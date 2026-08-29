"""Telegram inbound router (IMPLEMENTATION.md §13.1, §13.2).

A thin adapter. Every scheduling decision here is a call into
app/core/services/; if a booking rule appears in this file, it is a bug (the
whole architecture exists to keep them out of it -- DESIGN.md §3).

State lives in `flow_state`, not in aiogram FSM memory, so a restart mid-booking
loses nothing (§13.1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.telegram import keyboards as kb
from app.config import get_settings
from app.core.enums import Channel, Modality, RequestStatus, SenderType, SlotStatus, TokenPurpose
from app.core.errors import DomainError, SlotUnavailable, TokenInvalid
from app.core.models import (
    BookingRequest,
    Client,
    Practice,
    SessionType,
    Slot,
    TimezoneOption,
)
from app.core.policies import BookingPath, now_utc, parse_client_time, resolve_booking_mode
from app.core.services import booking, content, flow, notifications, waitlist
from app.core.services.clients import (
    consume_token,
    identities_for,
    issue_token,
    link_identity,
    looks_like_email,
    magic_link_allowance_left,
    resolve_client,
    set_client_language,
    set_client_timezone,
)
from app.core.services.flow import Step
from app.core.services.notifications import Envelope, Recipient
from app.core.services.settings import get_practice
from app.core.services.slots import list_available_slots
from app.core.services.translations import get_text
from app.render.dates import day_label
from app.render.labels import session_type_name
from app.render.markdown import escape_telegram

logger = logging.getLogger(__name__)

#: How far ahead the slot picker looks.
SLOT_WINDOW = timedelta(days=30)

#: DESIGN.md §11: the admin surface is English, so §13.2 writes its day headings
#: in it whatever language the therapist's own client record says.
LOCALE = "en"

#: §13.1: the deep-link payload that merges an email-first client with Telegram.
LINK_PREFIX = "link_"


@dataclass
class Reply:
    """What the router wants sent back.

    Returned rather than sent, so handling an update stays a pure function of
    the database plus the update -- and so tests can assert on it without a
    network.
    """

    text: str
    keyboard: Any | None = None
    #: Extra messages, sent in order after `text`. Topic blocks arrive as
    #: separate messages, which is what makes conditional delivery possible
    #: (DESIGN.md §10.1).
    extra: list[str] = field(default_factory=list)
    #: §13.2: replace the message the button was on instead of sending a new
    #: one, so the admin panel does not bury the chat it lives in. Only ever
    #: true for a reply to a callback -- a typed answer has no message of its
    #: own to edit.
    edit: bool = False
    #: §13.2: the toast shown on the tap that produced this reply. Used to say
    #: why an action was refused without spending a screen on it.
    toast: str | None = None


@dataclass(frozen=True, slots=True)
class Update:
    """The parts of a Telegram update this router uses.

    Deliberately not an aiogram type: the router is testable without
    constructing a full `Update`, and the webhook does the parsing.
    """

    chat_id: int
    text: str | None = None
    callback_data: str | None = None
    display_name: str | None = None
    #: The message a pressed button belongs to, and the query to answer for it
    #: (§13.2). Both are None for an ordinary message.
    message_id: int | None = None
    callback_id: str | None = None


async def handle(session: AsyncSession, update: Update) -> Reply | None:
    """Route one update. Never raises a domain error at the caller."""
    settings = get_settings()

    if update.text and update.text.startswith("/start"):
        return await _start(session, update)

    if update.text and update.text.startswith("/admin"):
        return await _admin(session, update, settings.admin_telegram_ids)

    # Admin callbacks are answered before the fallback below: the therapist's
    # chat is not required to have a client record, and sending her through
    # /start because she pressed Approve would be absurd.
    if update.callback_data:
        action, argument = kb.parse_callback(update.callback_data)
        if action == kb.NOOP:
            # A button that is there to be read: a day heading, a weekday
            # letter, a padding cell in the month grid, an hour §13.2 marks as
            # taken. The webhook answers the query, so the tap resolves and
            # nothing is sent.
            #
            # This MUST come before the client lookup for the same reason the
            # admin block does. `NOOP` is not an admin action, so the
            # therapist -- who has no client record -- fell through to `_start`
            # and was asked to choose a language, mid-proposal, for tapping one
            # of the dead cells her own picker is full of.
            return None
        if action in kb.ADMIN_ACTIONS:
            return await _admin_action(session, update.chat_id, action, argument)

    client = await _known_client(session, update.chat_id)
    if client is None:
        # Anyone who talks to the bot before /start is sent through it.
        return await _start(session, update)

    try:
        if update.callback_data:
            return await _callback(session, client, update)
        if update.text:
            return await _text(session, client, update)
    except DomainError as exc:
        logger.info("refused telegram action for chat %s: %s", update.chat_id, type(exc).__name__)
        return Reply(await get_text(session, client.language, "common.error.generic"))

    return None


def _client_name(client: Client | None) -> str | None:
    return (client.display_name or None) if client else None


async def _client_display_name(session: AsyncSession, client_id: UUID) -> str | None:
    return (
        await session.execute(select(Client.display_name).where(Client.id == client_id))
    ).scalar_one_or_none()


def _identity_labels(identities: list[Any]) -> list[str]:
    """How to reach a client, one line per channel (§13.2).

    The email carries whether it is verified, because §13.3 delivers nothing to
    an unverified address -- worth knowing before waiting for a reply to it.
    """
    labels = []
    for identity in identities:
        line = f"{identity.channel.value}: {identity.external_id}"
        if identity.channel is Channel.email:
            line += " (verified)" if identity.verified_at else " (unverified)"
        labels.append(line)
    return labels


async def _known_client(session: AsyncSession, chat_id: int) -> Client | None:
    from app.core.models import Identity

    identity = (
        await session.execute(
            select(Identity).where(
                Identity.channel == Channel.telegram,
                Identity.external_id == str(chat_id),
            )
        )
    ).scalar_one_or_none()
    if identity is None:
        return None
    return (
        await session.execute(select(Client).where(Client.id == identity.client_id))
    ).scalar_one()


# --- /start -----------------------------------------------------------------


async def _start(session: AsyncSession, update: Update) -> Reply:
    """§13.1 step 1.

    A `link_<token>` payload attaches this Telegram identity to a client who
    already exists, rather than creating a second one. That is the merge path,
    and it costs the client one tap instead of a "type your email into the bot"
    flow (DESIGN.md §5.1).
    """
    payload = ""
    if update.text:
        _, _, payload = update.text.partition(" ")
        payload = payload.strip()

    existing = await _known_client(session, update.chat_id)

    if payload.startswith(LINK_PREFIX) and existing is None:
        raw = payload[len(LINK_PREFIX) :]
        try:
            result = await consume_token(session, raw, TokenPurpose.link_channel)
        except TokenInvalid:
            practice = await get_practice(session)
            return Reply(
                await get_text(session, practice.default_language, "common.error.expired_link")
            )
        if result.client_id is not None:
            await link_identity(
                session,
                result.client_id,
                Channel.telegram,
                str(update.chat_id),
                verified=True,
            )
            client = (
                await session.execute(select(Client).where(Client.id == result.client_id))
            ).scalar_one()
            await flow.clear(session, client.id, Channel.telegram)
            return await _menu(session, client, greeting_key="common.welcome_back")

    if existing is not None:
        await flow.clear(session, existing.id, Channel.telegram)
        return await _menu(session, existing, greeting_key="common.welcome_back")

    # Telegram vouches for the user id, so the identity is verified by
    # construction (DESIGN.md §5.1).
    client = await resolve_client(
        session,
        Channel.telegram,
        str(update.chat_id),
        display_name=update.display_name,
        verified=True,
    )
    await flow.set_step(session, client.id, Channel.telegram, Step.choosing_language)
    practice = await get_practice(session)
    return Reply(
        await get_text(session, practice.default_language, "lang.select"),
        keyboard=kb.language_keyboard(),
    )


async def _menu(
    session: AsyncSession, client: Client, *, greeting_key: str = "common.welcome"
) -> Reply:
    """§13.1 step 3: one button per menu topic, plus Consultation and My
    appointments."""
    await flow.set_step(session, client.id, Channel.telegram, Step.idle, replace={})
    titles = await _menu_labels(session, client)
    return Reply(
        await get_text(session, client.language, greeting_key),
        keyboard=kb.main_menu(
            titles["topics"],
            titles["consultation"],
            titles["appointments"],
        ),
    )


async def _menu_labels(session: AsyncSession, client: Client) -> dict[str, Any]:
    """Every label the main keyboard shows, in the client's language.

    One source for both drawing the keyboard and recognising a press: §13.1
    matches a button by its exact text, so the two must never drift.
    """
    topics = await content.list_menu_topics(session)
    return {
        "topics": [
            await get_text(session, client.language, f"content.topic.{topic.code}.title")
            for topic in topics
        ],
        "consultation": await get_text(session, client.language, "menu.consultation"),
        "appointments": await get_text(session, client.language, "menu.appointments"),
    }


# --- Text -------------------------------------------------------------------


async def _text(session: AsyncSession, client: Client, update: Update) -> Reply | None:
    step = await flow.current_step(session, client.id, Channel.telegram)
    text = (update.text or "").strip()

    # §13.1: a main-keyboard label is navigation, not an answer. The step checks
    # below would otherwise store "Условия работы" as the client's problem text
    # for anyone who tapped a topic while being asked to describe it.
    if step is not Step.idle and await _is_menu_label(session, client, text):
        await flow.clear(session, client.id, Channel.telegram)
        return await _menu_selection(session, client, text)

    if step is Step.entering_problem:
        await flow.remember(session, client.id, Channel.telegram, problem=text)
        return await _ask_name(session, client)

    if step is Step.entering_name:
        await flow.remember(session, client.id, Channel.telegram, name=text)
        return await _ask_contact(session, client)

    if step is Step.choosing_contact:
        # Typed instead of tapped: take the sentence as the note it plainly is,
        # rather than repeating the question.
        await flow.remember(session, client.id, Channel.telegram, contact=text)
        return await _submit_booking(session, client)

    if step is Step.entering_contact:
        await flow.remember(session, client.id, Channel.telegram, contact=text)
        return await _submit_booking(session, client)

    if step is Step.entering_contact_email:
        return await _contact_email(session, client, text)

    if step is Step.entering_desired_time:
        # §13.1 asks the slot path for the session type and modality *after* the
        # time, so the free-text path asks in the same order. Going straight to
        # the problem text here left `session_type_id` unset, and the submit
        # below needs it: booking_request.session_type_id is NOT NULL.
        await flow.remember(session, client.id, Channel.telegram, desired_time=text)
        return await _ask_session_type(session, client)

    if step is Step.entering_counter:
        return await _submit_counter(session, client, text)

    if step is Step.admin_entering_proposal:
        return await _submit_proposal(session, client, update.chat_id, text)

    if step is Step.admin_entering_cancel_reason:
        return await _submit_cancel_reason(session, client, update.chat_id, text)

    if step is Step.waitlist_problem:
        await flow.remember(session, client.id, Channel.telegram, problem=text)
        await flow.set_step(session, client.id, Channel.telegram, Step.waitlist_contact)
        return Reply(
            await get_text(session, client.language, "waitlist.ask_contact"),
            keyboard=kb.skip_keyboard(await get_text(session, client.language, "common.skip")),
        )

    if step is Step.waitlist_contact:
        await flow.remember(session, client.id, Channel.telegram, contact=text)
        return await _submit_waitlist(session, client)

    # Idle: a main-menu button, which arrives as ordinary text.
    return await _menu_selection(session, client, text)


async def _is_menu_label(session: AsyncSession, client: Client, text: str) -> bool:
    labels = await _menu_labels(session, client)
    return text in {labels["consultation"], labels["appointments"], *labels["topics"]}


async def _menu_selection(session: AsyncSession, client: Client, text: str) -> Reply | None:
    labels = await _menu_labels(session, client)
    if text == labels["consultation"]:
        return await _begin_consultation(session, client)

    if text == labels["appointments"]:
        return await _my_appointments(session, client)

    for topic in await content.list_menu_topics(session):
        title = await get_text(session, client.language, f"content.topic.{topic.code}.title")
        if text == title:
            return await _send_topic(session, client, topic.code)

    return None


#: §13.1 step 9. Three is enough to answer "what do I have booked" without
#: turning the reply into a history dump.
APPOINTMENTS_SHOWN = 3


async def _my_appointments(session: AsyncSession, client: Client) -> Reply:
    """§13.1 step 9: everything still live, newest first.

    Read-only by construction: no transition, no outbox row. The one thing it
    adds is the buttons of a proposal still waiting on this client, because the
    message those buttons came in may be a long way up the chat by now.
    """
    requests = await booking.active_for_client(session, client.id, limit=APPOINTMENTS_SHOWN)
    if not requests:
        return Reply(await get_text(session, client.language, "menu.appointments.none"))

    practice = await get_practice(session)
    lines: list[str] = []
    awaiting: list[BookingRequest] = []

    for request in requests:
        when = await booking.requested_start(session, request)
        session_type = await session_type_name(
            session,
            client.language,
            await _session_type_code(session, request.session_type_id),
        )
        lines.append(
            await get_text(
                session,
                client.language,
                "menu.appointments.item",
                status=await get_text(
                    session, client.language, f"request.status.{request.status.value}"
                ),
                # Hard rule 8: status and time, never the problem text.
                time=_local(when, client, practice) or (request.desired_time_text or ""),
                session_type=session_type,
                modality=await get_text(
                    session, client.language, f"booking.modality.{request.modality.value}"
                ),
            )
        )
        if request.status is RequestStatus.negotiating:
            if await booking.whose_turn(session, request.id) is SenderType.client:
                awaiting.append(request)

        join = await notifications.join_info(session, request)
        if request.status is RequestStatus.confirmed and join:
            lines.append(
                await get_text(
                    session,
                    client.language,
                    "intent.request.confirmed.client.join_online",
                    url=join,
                )
            )

    keyboard = None
    if len(awaiting) == 1:
        # With two proposals open the buttons could not say which they answer,
        # and each proposal message still carries its own.
        keyboard = kb.negotiation_keyboard(
            awaiting[0].id,
            {
                kb.ACCEPT: await get_text(
                    session, client.language, "intent.request.proposal.client.action.accept"
                ),
                kb.COUNTER: await get_text(
                    session, client.language, "intent.request.proposal.client.action.counter"
                ),
                kb.DECLINE: await get_text(
                    session, client.language, "intent.request.proposal.client.action.decline"
                ),
            },
        )

    return Reply("\n\n".join(lines), keyboard=keyboard)


async def _session_type_code(session: AsyncSession, session_type_id: int) -> str:
    return str(
        (
            await session.execute(select(SessionType.code).where(SessionType.id == session_type_id))
        ).scalar_one()
    )


async def _send_topic(session: AsyncSession, client: Client, topic_code: str) -> Reply:
    """§13.1 step 4: a topic's published blocks, in order, as separate messages.

    Separate messages is what makes the 4096-character limit a non-issue and
    conditional delivery possible (DESIGN.md §10.1).
    """
    from app.render.markdown import to_telegram

    blocks = await content.get_topic_blocks(session, topic_code, client.language)
    parts: list[str] = []
    for block in blocks:
        parts.extend(to_telegram(block.body_md))

    if not parts:
        return Reply(await get_text(session, client.language, "common.error.not_found"))
    return Reply(parts[0], extra=parts[1:])


# --- Consultation -----------------------------------------------------------


async def _begin_consultation(session: AsyncSession, client: Client) -> Reply:
    """§13.1 step 5: resolve the booking mode and follow it.

    The matrix lives in app/core/policies.py; this only obeys the answer.
    """
    practice = await get_practice(session)
    slots = await list_available_slots(
        session,
        window_from=now_utc(),
        window_to=now_utc() + SLOT_WINDOW,
        tz=client.timezone or practice.timezone,
    )
    resolved = resolve_booking_mode(practice, slots_exist=bool(slots))

    if resolved.path is BookingPath.waitlist:
        await flow.set_step(session, client.id, Channel.telegram, Step.waitlist_problem, replace={})
        return Reply(
            await get_text(session, client.language, "waitlist.intro"),
            extra=[await get_text(session, client.language, "waitlist.ask_problem")],
        )

    if resolved.path is BookingPath.negotiation:
        await flow.set_step(
            session, client.id, Channel.telegram, Step.entering_desired_time, replace={}
        )
        return Reply(await get_text(session, client.language, "booking.ask_desired_time"))

    if client.timezone is None:
        await flow.set_step(
            session, client.id, Channel.telegram, Step.choosing_timezone, replace={}
        )
        options = (
            (
                await session.execute(
                    select(TimezoneOption)
                    .where(TimezoneOption.is_active.is_(True))
                    .order_by(TimezoneOption.sort_order, TimezoneOption.id)
                )
            )
            .scalars()
            .all()
        )
        return Reply(
            await get_text(session, client.language, "booking.choose_timezone"),
            keyboard=kb.timezone_keyboard([(o.iana_name, o.display_name) for o in options]),
        )

    return await _show_slots(session, client)


async def _show_slots(session: AsyncSession, client: Client) -> Reply:
    """§13.1 step 6: grouped by day, in the client's timezone."""
    practice = await get_practice(session)
    tz = client.timezone or practice.timezone
    slots = await list_available_slots(
        session,
        window_from=now_utc(),
        window_to=now_utc() + SLOT_WINDOW,
        tz=tz,
    )
    if not slots:
        return Reply(await get_text(session, client.language, "booking.slot.none_available"))

    await flow.set_step(session, client.id, Channel.telegram, Step.choosing_slot)
    # The picker's day headings are written here, where there is a session and a
    # language to write them in (§15).
    zone = ZoneInfo(tz)
    labels = {}
    for slot in slots:
        day = slot.starts_at_utc.astimezone(zone).strftime("%Y-%m-%d")
        if day not in labels:
            labels[day] = await day_label(session, client.language, slot.starts_at_utc, tz)

    return Reply(
        await get_text(session, client.language, "booking.choose_slot", timezone=tz),
        keyboard=kb.slot_keyboard(slots, tz, labels),
    )


async def _ask_session_type(session: AsyncSession, client: Client) -> Reply:
    types = (
        (
            await session.execute(
                select(SessionType)
                .where(SessionType.is_active.is_(True))
                .order_by(SessionType.sort_order, SessionType.id)
            )
        )
        .scalars()
        .all()
    )
    options = []
    for session_type in types:
        name = await session_type_name(session, client.language, session_type.code)
        label = await get_text(
            session,
            client.language,
            "booking.type.without_price",
            name=name,
            duration=session_type.duration_min,
        )
        options.append((str(session_type.id), label))

    await flow.set_step(session, client.id, Channel.telegram, Step.choosing_session_type)
    return Reply(
        await get_text(session, client.language, "booking.choose_type"),
        keyboard=kb.choice_keyboard(kb.STYPE, options),
    )


async def _ask_modality(session: AsyncSession, client: Client) -> Reply:
    labels = [
        (
            Modality.online.value,
            await get_text(session, client.language, "booking.modality.online"),
        ),
        (
            Modality.onsite.value,
            await get_text(session, client.language, "booking.modality.onsite"),
        ),
    ]
    await flow.set_step(session, client.id, Channel.telegram, Step.choosing_modality)
    return Reply(
        await get_text(session, client.language, "booking.choose_modality"),
        keyboard=kb.choice_keyboard(kb.MODE, labels),
    )


async def _ask_problem(session: AsyncSession, client: Client) -> Reply:
    await flow.set_step(session, client.id, Channel.telegram, Step.entering_problem)
    return Reply(
        await get_text(session, client.language, "booking.ask_problem"),
        keyboard=kb.skip_keyboard(await get_text(session, client.language, "common.skip")),
    )


async def _ask_name(session: AsyncSession, client: Client) -> Reply:
    await flow.set_step(session, client.id, Channel.telegram, Step.entering_name)
    return Reply(
        await get_text(session, client.language, "booking.ask_name"),
        keyboard=kb.skip_keyboard(await get_text(session, client.language, "common.skip")),
    )


async def _ask_contact(session: AsyncSession, client: Client) -> Reply:
    """§13.1 step 7: a choice, not an open question.

    "Email" was always a reasonable answer to "how would you prefer to be
    contacted?", and nothing used to follow it -- the note went to the therapist
    and delivery kept going to Telegram. The buttons make the question mean what
    it says: picking Email asks for the address and starts verifying it.
    """
    await flow.set_step(session, client.id, Channel.telegram, Step.choosing_contact)

    options = [
        (kb.CONTACT_TELEGRAM, await get_text(session, client.language, "booking.contact.telegram")),
    ]
    # §4: with SMTP unset there is no way to send the link that would verify an
    # address, so the option that promises one is not offered.
    if get_settings().email_enabled:
        options.append(
            (kb.CONTACT_EMAIL, await get_text(session, client.language, "booking.contact.email"))
        )
    options.append(
        (kb.CONTACT_OTHER, await get_text(session, client.language, "booking.contact.other"))
    )

    return Reply(
        await get_text(session, client.language, "booking.ask_contact"),
        keyboard=kb.choice_keyboard(kb.CONTACT, options),
    )


async def _contact_choice(session: AsyncSession, client: Client, choice: str) -> Reply:
    """The branch behind each button of §13.1 step 7."""
    if choice == kb.CONTACT_EMAIL:
        await flow.set_step(session, client.id, Channel.telegram, Step.entering_contact_email)
        return Reply(
            await get_text(session, client.language, "booking.ask_email"),
            keyboard=kb.skip_keyboard(await get_text(session, client.language, "common.skip")),
        )

    if choice == kb.CONTACT_OTHER:
        await flow.set_step(session, client.id, Channel.telegram, Step.entering_contact)
        return Reply(
            await get_text(session, client.language, "booking.ask_contact_other"),
            keyboard=kb.skip_keyboard(await get_text(session, client.language, "common.skip")),
        )

    # Telegram: the identity already exists and §13.3 already prefers it, so
    # there is nothing to record.
    return await _submit_booking(session, client)


async def _contact_email(session: AsyncSession, client: Client, address: str) -> Reply:
    """Collect an address and send the link that proves it (§13.1 step 7).

    The address is stored as the contact note so the therapist can see it, but
    it becomes a delivery target only once `verified_at` is set -- which happens
    when the client follows the link, not here (DESIGN.md §5.1).
    """
    settings = get_settings()

    if not looks_like_email(address):
        return Reply(
            await get_text(session, client.language, "booking.contact.email_invalid"),
            keyboard=kb.skip_keyboard(await get_text(session, client.language, "common.skip")),
        )

    # Identities are stored lowercased, so the outbox row, the contact note and
    # the identity all say the same thing.
    address = address.strip().lower()

    try:
        await link_identity(session, client.id, Channel.email, address, verified=False)
    except TokenInvalid:
        # Someone else already holds it. Merging two people on the say-so of a
        # typed address is not a recoverable mistake, so the flow stops here.
        return Reply(
            await get_text(session, client.language, "booking.contact.email_taken"),
            keyboard=kb.skip_keyboard(await get_text(session, client.language, "common.skip")),
        )

    # §17: 3 per hour per address, counted from the tokens themselves.
    if await magic_link_allowance_left(session, address) <= 0:
        return Reply(
            await get_text(session, client.language, "booking.contact.email_throttled"),
            keyboard=kb.skip_keyboard(await get_text(session, client.language, "common.skip")),
        )

    raw = await issue_token(
        session, TokenPurpose.login, client_id=client.id, payload={"email": address}
    )
    await notifications.enqueue(
        session,
        Envelope(
            "auth.login_link.client",
            Recipient.client,
            {
                "url": f"{settings.base_url}/auth/callback?token={raw}",
                "minutes": 30,
            },
            # §13.3: addressed to the mailbox being proved, not routed by the
            # general policy -- which would send it straight back to Telegram.
            client_id=client.id,
            to=(Channel.email, address),
        ),
    )

    await flow.remember(session, client.id, Channel.telegram, contact=address)
    reply = await _submit_booking(session, client)
    reply.extra.insert(
        0, await get_text(session, client.language, "booking.contact.email_sent", email=address)
    )
    return reply


async def _submit_booking(session: AsyncSession, client: Client) -> Reply:
    """§13.1 step 8. The core decides everything; this reports the outcome."""
    scratch = await flow.data(session, client.id, Channel.telegram)
    practice = await get_practice(session)

    # A flow that reached here without a session type is a routing bug, not a
    # client error. Asking again is a survivable answer; a KeyError out of a
    # webhook handler is not -- Telegram would retry the same update forever.
    if not scratch.get("session_type_id"):
        logger.warning("submit reached without a session type; asking again")
        return await _ask_session_type(session, client)

    common: dict[str, Any] = {
        "client_id": client.id,
        "session_type_id": int(scratch["session_type_id"]),
        "modality": Modality(scratch.get("modality", Modality.online.value)),
        "source_channel": Channel.telegram,
        "problem_text": scratch.get("problem"),
        "contact_note": scratch.get("contact"),
        "display_name": scratch.get("name") or client.display_name,
        "client_timezone": client.timezone or practice.timezone,
    }

    try:
        if scratch.get("slot_id"):
            request = await booking.submit_slot_request(
                session, slot_id=int(scratch["slot_id"]), **common
            )
        else:
            request = await booking.submit_free_time_request(
                session, desired_time_text=scratch.get("desired_time") or "", **common
            )
    except SlotUnavailable:
        # DESIGN.md §8: the race this hold exists to lose gracefully.
        await flow.set_step(session, client.id, Channel.telegram, Step.choosing_slot)
        return Reply(await get_text(session, client.language, "booking.slot.taken"))

    await notifications.publish(session)
    await flow.clear(session, client.id, Channel.telegram)

    return Reply(
        await get_text(session, client.language, "booking.submitted", uuid=str(request.uuid))
    )


async def _submit_waitlist(session: AsyncSession, client: Client) -> Reply:
    scratch = await flow.data(session, client.id, Channel.telegram)
    await waitlist.join_waitlist(
        session,
        client_id=client.id,
        problem_text=scratch.get("problem"),
        contact_note=scratch.get("contact"),
    )
    await notifications.publish(session)
    await flow.clear(session, client.id, Channel.telegram)
    return Reply(await get_text(session, client.language, "waitlist.submitted"))


# --- Callbacks --------------------------------------------------------------


async def _callback(session: AsyncSession, client: Client, update: Update) -> Reply | None:
    action, argument = kb.parse_callback(update.callback_data or "")
    step = await flow.current_step(session, client.id, Channel.telegram)

    if action == kb.LANG:
        await set_client_language(session, client.id, argument)
        return await _menu(session, client)

    if action == kb.TZ:
        await set_client_timezone(session, client.id, argument)
        return await _show_slots(session, client)

    if action == kb.SLOT:
        await flow.remember(session, client.id, Channel.telegram, slot_id=int(argument))
        return await _ask_session_type(session, client)

    if action == kb.STYPE:
        await flow.remember(session, client.id, Channel.telegram, session_type_id=int(argument))
        return await _ask_modality(session, client)

    if action == kb.MODE:
        await flow.remember(session, client.id, Channel.telegram, modality=argument)
        if argument == Modality.onsite.value:
            practice = await get_practice(session)
            if practice.clinic_onsite_url:
                reply = await _ask_problem(session, client)
                reply.extra.insert(
                    0,
                    await get_text(
                        session,
                        client.language,
                        "booking.onsite_info",
                        url=practice.clinic_onsite_url,
                    ),
                )
                return reply
        return await _ask_problem(session, client)

    if action == kb.CONTACT:
        return await _contact_choice(session, client, argument)

    if action == kb.SKIP:
        return await _skip(session, client, step)

    if action in kb.CLIENT_ACTIONS:
        return await _negotiation_action(session, client, action, argument)

    if action in kb.ADMIN_ACTIONS:
        return await _admin_action(session, update.chat_id, action, argument)

    return None


async def _submit_counter(session: AsyncSession, client: Client, text: str) -> Reply:
    """The client's reply to a proposal.

    Free text stays free text: "some evening next week?" is a normal thing to
    say, and forcing it into a datetime picker would be worse than storing the
    sentence (DESIGN.md §9). A parseable time is also recorded structurally, so
    the therapist can approve it directly.
    """
    scratch = await flow.data(session, client.id, Channel.telegram)
    request_id = int(scratch.get("request_id", 0))
    request = await _request_for(session, client, request_id)
    if request is None:
        await flow.clear(session, client.id, Channel.telegram)
        return Reply(await get_text(session, client.language, "common.error.not_found"))

    practice = await get_practice(session)
    proposed = _parse_time(text, client.timezone or practice.timezone)

    try:
        await booking.client_counter(session, request.id, proposed_start=proposed, body_text=text)
    except DomainError:
        await flow.clear(session, client.id, Channel.telegram)
        return Reply(await get_text(session, client.language, "common.error.generic"))

    await notifications.publish(session)
    await flow.clear(session, client.id, Channel.telegram)
    return Reply(await get_text(session, client.language, "intent.request.counter.client.sent"))


async def _submit_proposal(session: AsyncSession, client: Client, chat_id: int, text: str) -> Reply:
    """The therapist's proposal, typed after pressing Propose (§13.2)."""
    settings = get_settings()
    if chat_id not in settings.admin_telegram_ids:
        return Reply(await get_text(session, client.language, "common.error.generic"))

    scratch = await flow.data(session, client.id, Channel.telegram)
    request_id = int(scratch.get("request_id", 0))
    request = (
        await session.execute(select(BookingRequest).where(BookingRequest.id == request_id))
    ).scalar_one_or_none()
    if request is None:
        await flow.clear(session, client.id, Channel.telegram)
        return Reply("That request no longer exists.")

    practice = await get_practice(session)
    proposed = _parse_time(text, practice.timezone)

    await flow.clear(session, client.id, Channel.telegram)
    try:
        await booking.admin_propose(session, request.id, proposed_start=proposed, body_text=text)
    except DomainError as exc:
        return await _admin_request(session, request, toast=f"Not possible: {type(exc).__name__}.")

    await notifications.publish(session)
    await session.refresh(request)
    # §13.2: the answer to a typed reply is a new message -- there is no button
    # press to edit -- but it is still the request screen, not a dead end.
    reply = await _admin_request(session, request, toast="Proposed.")
    reply.edit = False
    return reply


async def _submit_cancel_reason(
    session: AsyncSession, client: Client, chat_id: int, text: str
) -> Reply:
    """The reason typed after pressing Cancel (§13.2).

    Asked for rather than invented: it reaches the client verbatim in
    `request.cancelled.client`, and "cancelled from Telegram" is not something
    to say to someone whose session has just been called off.
    """
    settings = get_settings()
    if chat_id not in settings.admin_telegram_ids:
        return Reply(await get_text(session, client.language, "common.error.generic"))

    scratch = await flow.data(session, client.id, Channel.telegram)
    request = (
        await session.execute(
            select(BookingRequest).where(BookingRequest.id == int(scratch.get("request_id", 0)))
        )
    ).scalar_one_or_none()
    await flow.clear(session, client.id, Channel.telegram)
    if request is None:
        return _admin_gone()

    try:
        await booking.admin_cancel(session, request.id, reason=text.strip() or None)
    except DomainError as exc:
        return await _admin_request(session, request, toast=f"Not possible: {type(exc).__name__}.")

    await notifications.publish(session)
    await session.refresh(request)
    reply = await _admin_request(session, request, toast="Cancelled.")
    reply.edit = False
    return reply


def _parse_time(text: str, tz: str) -> datetime | None:
    """`YYYY-MM-DD HH:MM` in `tz` -> an aware UTC instant, or None.

    None is not a failure: §9 says a structured time is *preferred*, not
    required, and the words are kept either way.

    The formats live in the core, because the web asks the same question of the
    same people and must read the answer the same way.
    """
    return parse_client_time(text, tz)


# --- Negotiation, client side (§7.1) ----------------------------------------


async def _counter_options(
    session: AsyncSession, client: Client, request: BookingRequest
) -> Reply:
    """§12.1's counter form, as a keyboard.

    The same question the web asks and the same gate on it, because a client
    answering a proposal on a phone and one answering it in a browser are being
    asked the same thing -- and where the web shows a `datetime-local`, this
    offers the picker §13.2 gives the therapist. Typing survives beneath both,
    for `18:30` and for the words §9 keeps either way.
    """
    practice = await get_practice(session)
    tz = request.client_timezone or client.timezone or practice.timezone

    slots = await list_available_slots(
        session,
        window_from=now_utc(),
        window_to=now_utc() + SLOT_WINDOW,
        session_type_id=request.session_type_id,
        modality=request.modality,
        tz=tz,
    )

    zone = ZoneInfo(tz)
    labels: dict[str, str] = {}
    for slot in slots:
        day = slot.starts_at_utc.astimezone(zone).strftime("%Y-%m-%d")
        if day not in labels:
            labels[day] = await day_label(session, client.language, slot.starts_at_utc, tz)

    lines = [await get_text(session, client.language, "booking.choose_slot", timezone=tz)]
    if not slots:
        lines = [await get_text(session, client.language, "booking.slot.none_available")]

    extra: list[list[tuple[str, str]]] = []
    if practice.fallback_to_negotiation:
        # §9: words remain a legal answer, so park the step and say how a time
        # is best written -- the hint the web replaces with a picker.
        await flow.set_step(
            session,
            client.id,
            Channel.telegram,
            Step.entering_counter,
            replace={"request_id": request.id},
        )
        lines.append(await get_text(session, client.language, "intent.request.counter.client.ask"))
        lines.append(await get_text(session, client.language, "request.counter.time_hint"))
        extra.append(
            [
                (
                    await get_text(session, client.language, "request.counter.other_time"),
                    f"{kb.COUNTER_MONTHS}:{request.id}",
                )
            ]
        )
    else:
        await flow.clear(session, client.id, Channel.telegram)
        extra.append(
            [
                (
                    await get_text(session, client.language, "request.counter.join_waitlist"),
                    f"{kb.COUNTER_WAITLIST}:{request.id}",
                )
            ]
        )

    return Reply(
        "\n\n".join(lines),
        keyboard=kb.slot_keyboard(
            slots,
            tz,
            labels,
            action=kb.COUNTER_SLOT,
            prefix=f"{request.id}:",
            extra=extra,
        ),
    )


#: §12.1's picker screens, reached the same way as §13.2's and drawn by the same
#: builders.
_COUNTER_PICKER_SCREENS = frozenset(
    {kb.COUNTER_MONTHS, kb.COUNTER_DAYS, kb.COUNTER_HOURS, kb.COUNTER_AT}
)


async def _client_picker(session: AsyncSession, client: Client) -> kb.Picker:
    """§12.1's picker, written in the client's own language.

    Two months rather than the therapist's three: a time four months out is not
    a suggestion she can act on. `taken` is never passed to it -- marking her
    filled hours here would tell a client when other people have sessions, and
    omitting them would say the same thing by the gap.
    """
    lang = client.language
    return kb.Picker(
        days=kb.COUNTER_DAYS,
        hours=kb.COUNTER_HOURS,
        at=kb.COUNTER_AT,
        months=kb.COUNTER_MONTHS,
        cancel=kb.COUNTER,
        months_ahead=2,
        month_names={
            number: await get_text(session, lang, f"date.month.{number}")
            for number in range(1, 13)
        },
        weekdays=[
            await get_text(session, lang, f"date.weekday.{number}") for number in range(7)
        ],
        back_to_months=await get_text(session, lang, "request.counter.back_months"),
        back_to_days=await get_text(session, lang, "request.counter.back_days"),
        cancel_label=await get_text(session, lang, "common.cancel"),
    )


async def _counter_picker(
    session: AsyncSession, client: Client, request: BookingRequest, action: str, rest: str
) -> Reply:
    """§12.1's month -> day -> hour, in the client's own timezone."""
    practice = await get_practice(session)
    tz = request.client_timezone or client.timezone or practice.timezone
    today = now_utc().astimezone(ZoneInfo(tz)).date()
    picker = await _client_picker(session, client)

    if action == kb.COUNTER_MONTHS:
        return Reply(
            await get_text(session, client.language, "request.counter.pick_month"),
            keyboard=kb.months_keyboard(picker, request.id, today),
        )

    if action == kb.COUNTER_DAYS:
        try:
            year, month = (int(part) for part in rest.split("-", 1))
            date(year, month, 1)
        except ValueError:
            return Reply(await get_text(session, client.language, "common.error.generic"))
        return Reply(
            await get_text(session, client.language, "request.counter.pick_day"),
            keyboard=kb.days_keyboard(picker, request.id, year, month, today=today),
        )

    if action == kb.COUNTER_HOURS:
        try:
            day = date.fromisoformat(rest)
        except ValueError:
            return Reply(await get_text(session, client.language, "common.error.generic"))
        # No `taken`: see `_client_picker`.
        return Reply(
            await get_text(session, client.language, "request.counter.pick_hour"),
            keyboard=kb.hours_keyboard(picker, request.id, day),
        )

    # kb.COUNTER_AT
    when = _local_hour(rest, tz)
    if when is None:
        return Reply(await get_text(session, client.language, "common.error.generic"))

    await flow.clear(session, client.id, Channel.telegram)
    try:
        await booking.client_counter(session, request.id, proposed_start=when)
    except DomainError:
        return Reply(await get_text(session, client.language, "common.error.generic"))

    await notifications.publish(session)
    return Reply(await get_text(session, client.language, "intent.request.counter.client.sent"))


async def _counter_with_slot(
    session: AsyncSession, client: Client, request: BookingRequest, slot_id: int
) -> Reply:
    """A slot tapped in answer to a proposal (§12.1).

    Re-read rather than trusted: the keyboard may have been sitting in the chat
    for an hour, and a counter naming a time the practice no longer offers hands
    the therapist something she cannot honour. Nothing is held -- §7.1 keeps the
    request's original slot, and one request holding two is not a state worth
    inventing.
    """
    starts_at = (
        await session.execute(
            select(Slot.starts_at).where(Slot.id == slot_id, Slot.status == SlotStatus.available)
        )
    ).scalar_one_or_none()
    if starts_at is None:
        return Reply(await get_text(session, client.language, "booking.slot.taken"))

    await flow.clear(session, client.id, Channel.telegram)
    try:
        await booking.client_counter(session, request.id, proposed_start=starts_at)
    except DomainError:
        return Reply(await get_text(session, client.language, "common.error.generic"))

    await notifications.publish(session)
    return Reply(await get_text(session, client.language, "intent.request.counter.client.sent"))


async def _negotiation_action(
    session: AsyncSession, client: Client, action: str, argument: str
) -> Reply | None:
    """The buttons on a `request.proposal.client` message.

    Every rule lives in app/core/services/booking.py; this reports the outcome.
    A refusal is answered with an explanation rather than silence -- the client
    pressed a button and deserves to know it did nothing.
    """
    # §13.1: a slot tapped in answer to a proposal carries both ids, and §12.1's
    # picker carries its answer so far, so the button still works when the
    # parked flow has since been cleared.
    head, _, rest = argument.partition(":")
    slot_id: int | None = None
    if action == kb.COUNTER_SLOT:
        try:
            slot_id = int(rest)
        except ValueError:
            return None

    try:
        request_id = int(head)
    except ValueError:
        return None

    request = await _request_for(session, client, request_id)
    if request is None:
        return Reply(await get_text(session, client.language, "common.error.not_found"))

    if action == kb.COUNTER:
        return await _counter_options(session, client, request)

    if action == kb.COUNTER_SLOT and slot_id is not None:
        return await _counter_with_slot(session, client, request, slot_id)

    if action in _COUNTER_PICKER_SCREENS:
        return await _counter_picker(session, client, request, action, rest)

    if action == kb.COUNTER_WAITLIST:
        # §12.1: the way out where a counter may not be words.
        await flow.clear(session, client.id, Channel.telegram)
        try:
            await booking.client_decline_to_waitlist(session, request.id)
        except DomainError:
            return Reply(await get_text(session, client.language, "common.error.generic"))
        await notifications.publish(session)
        return Reply(await get_text(session, client.language, "waitlist.submitted"))

    try:
        if action == kb.ACCEPT:
            confirmed = await booking.client_accept(session, request.id)
            await notifications.publish(session)
            return Reply(
                await get_text(
                    session,
                    client.language,
                    "intent.request.confirmed.client.body",
                    uuid=str(confirmed.uuid),
                    time=_local(confirmed.scheduled_start, client, await get_practice(session)),
                )
            )
        await booking.client_decline(session, request.id)
        await notifications.publish(session)
        return Reply(
            await get_text(
                session,
                client.language,
                "intent.request.rejected.client.body",
                uuid=str(request.uuid),
            )
        )
    except DomainError:
        # Already answered, or answered from the other channel.
        return Reply(await get_text(session, client.language, "common.error.generic"))


async def _request_for(
    session: AsyncSession, client: Client, request_id: int
) -> BookingRequest | None:
    """A request, but only if it belongs to this client.

    Callback data is client-supplied: a request id from someone else's message
    must not be actionable.
    """
    request = (
        await session.execute(
            select(BookingRequest).where(
                BookingRequest.id == request_id, BookingRequest.client_id == client.id
            )
        )
    ).scalar_one_or_none()
    return request


def _local(value: datetime | None, client: Client, practice: Practice) -> str:
    """An instant in the client's own zone. Storage stays UTC (DESIGN.md §8)."""
    if value is None:
        return ""
    zone = ZoneInfo(client.timezone or practice.timezone)
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M")


# --- Negotiation, admin side (§13.2) ----------------------------------------


#: §13.2's propose screens, all reached the same way and all carrying the answer
#: so far in their callback.
_PROPOSE_SCREENS = frozenset(
    {
        kb.PROPOSE,
        kb.PROPOSE_SLOT,
        kb.PROPOSE_MONTHS,
        kb.PROPOSE_DAYS,
        kb.PROPOSE_HOURS,
        kb.PROPOSE_AT,
        kb.PROPOSE_TYPE,
    }
)


async def _propose_screen(
    session: AsyncSession,
    chat_id: int,
    request: BookingRequest,
    action: str,
    rest: str,
) -> Reply:
    """§13.2: propose a time without typing an ISO timestamp.

    The slots she has already published first, then month -> day -> hour for a
    time she has not, and typing kept for `18:30` and for the proposal of words
    §7.1 still allows. Nothing is parked until she asks to type: every screen
    carries its own answer, so there is no half-finished picker to abandon.
    """
    practice = await get_practice(session)
    zone = ZoneInfo(practice.timezone)
    today = now_utc().astimezone(zone).date()

    if action == kb.PROPOSE:
        await _clear_admin_input(session, chat_id)
        return await _propose_slots(session, request, practice)

    if action == kb.PROPOSE_SLOT:
        # Re-read: the panel may have been sitting in her chat for an hour, and
        # a slot taken since is not hers to offer.
        starts_at = (
            await session.execute(
                select(Slot.starts_at).where(
                    Slot.id == _int_or_none(rest), Slot.status == SlotStatus.available
                )
            )
        ).scalar_one_or_none()
        if starts_at is None:
            return await _admin_request(session, request, toast="That time has gone.")
        return await _propose_at(session, chat_id, request, starts_at, practice)

    if action == kb.PROPOSE_TYPE:
        await _park_admin_input(session, chat_id, Step.admin_entering_proposal, request.id)
        return Reply(
            f"Propose a time for {request.uuid}.\n\n"
            f"Send it as YYYY-MM-DD HH:MM in {practice.timezone}, or send words.",
            keyboard=kb.panel_keyboard([[("✕ Cancel", f"{kb.PANEL_OPEN}:{request.id}")]]),
            edit=True,
        )

    if action == kb.PROPOSE_MONTHS:
        return Reply(
            f"Which month? Times are in {practice.timezone}.",
            keyboard=kb.months_keyboard(kb.ADMIN_PICKER, request.id, today),
            edit=True,
        )

    if action == kb.PROPOSE_DAYS:
        try:
            year, month = (int(part) for part in rest.split("-", 1))
            date(year, month, 1)
        except ValueError:
            return await _admin_request(session, request, toast="That month is not a month.")
        return Reply(
            f"{date(year, month, 1):%B %Y}. Which day?",
            keyboard=kb.days_keyboard(kb.ADMIN_PICKER, request.id, year, month, today=today),
            edit=True,
        )

    if action == kb.PROPOSE_HOURS:
        try:
            day = date.fromisoformat(rest)
        except ValueError:
            return await _admin_request(session, request, toast="That day is not a day.")
        taken = await booking.taken_hours_on(session, day=day, tz=practice.timezone)
        return Reply(
            f"{day:%A %d %B}. Which hour? Marked ones already have something in them.",
            keyboard=kb.hours_keyboard(kb.ADMIN_PICKER, request.id, day, taken),
            edit=True,
        )

    # kb.PROPOSE_AT: she has picked an hour.
    when = _local_hour(rest, practice.timezone)
    if when is None:
        return await _admin_request(session, request, toast="That is not a time.")
    return await _propose_at(session, chat_id, request, when, practice)


async def _propose_at(
    session: AsyncSession,
    chat_id: int,
    request: BookingRequest,
    when: datetime,
    practice: Practice,
) -> Reply:
    """Send the proposal and answer with the request screen (§13.2).

    The toast names the time in her own clock, so a mis-tap is visible at once
    rather than after the client replies to something she did not mean.
    """
    await _clear_admin_input(session, chat_id)
    try:
        await booking.admin_propose(session, request.id, proposed_start=when)
    except DomainError as exc:
        return await _admin_request(session, request, toast=f"Not possible: {type(exc).__name__}.")

    await notifications.publish(session)
    await session.refresh(request)
    local = when.astimezone(ZoneInfo(practice.timezone))
    return await _admin_request(session, request, toast=f"Proposed {local:%a %d %b, %H:%M}.")


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


async def _propose_slots(
    session: AsyncSession, request: BookingRequest, practice: Practice
) -> Reply:
    """Screen one: the openings she has already published for this request.

    One tap, and if the client accepts it, §7.1's `_matching_slot` books that
    very slot -- so an offer she has already made stays an offer she has made.
    """
    slots = await list_available_slots(
        session,
        window_from=now_utc(),
        window_to=now_utc() + SLOT_WINDOW,
        session_type_id=request.session_type_id,
        modality=request.modality,
        tz=practice.timezone,
    )

    zone = ZoneInfo(practice.timezone)
    labels: dict[str, str] = {}
    for slot in slots:
        day = slot.starts_at_utc.astimezone(zone).strftime("%Y-%m-%d")
        if day not in labels:
            labels[day] = await day_label(session, LOCALE, slot.starts_at_utc, practice.timezone)

    lines = [f"Propose a time for {request.uuid}.", f"Times are in {practice.timezone}."]
    if slots:
        lines.insert(1, "Tap one of your free times, or pick another.")
    else:
        lines.insert(1, "You have no free times published. Pick one below.")

    return Reply(
        "\n\n".join(lines),
        keyboard=kb.slot_keyboard(
            slots,
            practice.timezone,
            labels,
            action=kb.PROPOSE_SLOT,
            prefix=f"{request.id}:",
            extra=[
                [
                    ("Another time", f"{kb.PROPOSE_MONTHS}:{request.id}"),
                    ("Type it", f"{kb.PROPOSE_TYPE}:{request.id}"),
                ],
                [("✕ Cancel", f"{kb.PANEL_OPEN}:{request.id}")],
            ],
        ),
        edit=True,
    )


def _local_hour(value: str, tz: str) -> datetime | None:
    """`YYYY-MM-DDTHH` in the practice's clock -> an aware UTC instant.

    Her own clock, never the client's: §13.2 is her surface, and DESIGN.md §8
    converts only at the edges.
    """
    try:
        naive = datetime.strptime(value, "%Y-%m-%dT%H")  # noqa: DTZ007 - zone applied next
    except ValueError:
        return None
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


async def _admin_action(
    session: AsyncSession, chat_id: int, action: str, argument: str
) -> Reply | None:
    """§13.2: every tap on the panel, and on an admin notification.

    Each one answers with a *screen*, never with bare text: an outcome the
    therapist cannot navigate away from is where the previous version left her.
    """
    settings = get_settings()
    if chat_id not in settings.admin_telegram_ids:
        return None  # Silence: an unknown chat learns nothing.

    if action == kb.PANEL:
        return await _panel(session, edit=True)

    if action == kb.PANEL_AVAILABILITY:
        practice = await get_practice(session)
        practice.availability_on = not practice.availability_on
        await session.flush()
        state = "on" if practice.availability_on else "off"
        return await _panel(session, edit=True, toast=f"Availability is now {state}.")

    if action == kb.PANEL_REQUESTS:
        return await _admin_requests(session, _page(argument))

    if action == kb.PANEL_WAITLIST:
        return await _admin_waitlist(session, _page(argument))

    if action == kb.PANEL_SESSIONS:
        return await _admin_sessions(session, 7 if argument == "7" else 2)

    # §13.2's picker holds no state, so its screens carry the answer so far
    # after the request id. Everything else has a bare id.
    head, _, rest = argument.partition(":")
    try:
        request_id = int(head)
    except ValueError:
        return None

    request = (
        await session.execute(select(BookingRequest).where(BookingRequest.id == request_id))
    ).scalar_one_or_none()
    if request is None:
        return _admin_gone()

    if action == kb.PANEL_OPEN:
        # Opening a request abandons anything half-typed for another one.
        await _clear_admin_input(session, chat_id)
        return await _admin_request(session, request)

    if action in _PROPOSE_SCREENS:
        return await _propose_screen(session, chat_id, request, action, rest)

    if action == kb.CANCEL_REQUEST:
        await _park_admin_input(session, chat_id, Step.admin_entering_cancel_reason, request.id)
        return Reply(
            f"Cancelling {request.uuid}.\n\n"
            "Why? The client is told the reason. Send a line, or skip it.",
            keyboard=kb.panel_keyboard(
                [
                    [
                        ("Skip", f"{kb.PANEL_SKIP}:{request.id}"),
                        ("✕ Cancel", f"{kb.PANEL_OPEN}:{request.id}"),
                    ]
                ]
            ),
            edit=True,
        )

    try:
        if action == kb.APPROVE:
            # §13.2: the practice's default meeting link. A per-request one is
            # web-only, because it means typing a URL on a phone.
            await booking.admin_approve(session, request.id)
            toast = "Confirmed."
        elif action == kb.REJECT:
            await booking.admin_reject(session, request.id)
            toast = "Rejected."
        elif action == kb.PANEL_SKIP:
            await _clear_admin_input(session, chat_id)
            await booking.admin_cancel(session, request.id, reason=None)
            toast = "Cancelled."
        else:
            return None
    except DomainError as exc:
        # The status moved under her -- another channel answered first. Show
        # what it is now rather than an error she cannot act on.
        await session.refresh(request)
        return await _admin_request(
            session, request, toast=f"Not possible: {type(exc).__name__}."
        )

    await notifications.publish(session)
    await session.refresh(request)
    return await _admin_request(session, request, toast=toast)


def _page(argument: str) -> int:
    try:
        return max(0, int(argument))
    except ValueError:
        return 0


def _admin_gone(toast: str | None = None) -> Reply:
    """A request that has been erased under her. Still a screen, still navigable."""
    return Reply(
        "That request no longer exists.",
        keyboard=kb.panel_keyboard([_admin_nav()]),
        edit=True,
        toast=toast,
    )


def _admin_nav() -> list[tuple[str, str]]:
    return [("← Requests", f"{kb.PANEL_REQUESTS}:0"), ("⌂ Panel", kb.PANEL)]


async def _park_admin_input(
    session: AsyncSession, chat_id: int, step: Step, request_id: int
) -> None:
    """§13.2: a typed answer needs somewhere to remember what it is about.

    `flow_state` on the therapist's own client row -- the same store §13.1 uses,
    and for the same reason: a restart mid-sentence loses nothing.
    """
    admin_client = await resolve_client(session, Channel.telegram, str(chat_id), verified=True)
    await flow.set_step(
        session, admin_client.id, Channel.telegram, step, replace={"request_id": request_id}
    )


async def _clear_admin_input(session: AsyncSession, chat_id: int) -> None:
    admin_client = await resolve_client(session, Channel.telegram, str(chat_id), verified=True)
    await flow.clear(session, admin_client.id, Channel.telegram)


async def _skip(session: AsyncSession, client: Client, step: Step) -> Reply | None:
    """§13.1 step 7: each optional answer is skippable."""
    if step is Step.entering_problem:
        return await _ask_name(session, client)
    if step is Step.entering_name:
        return await _ask_contact(session, client)
    if step in (Step.choosing_contact, Step.entering_contact, Step.entering_contact_email):
        return await _submit_booking(session, client)
    if step is Step.waitlist_problem:
        await flow.set_step(session, client.id, Channel.telegram, Step.waitlist_contact)
        return Reply(
            await get_text(session, client.language, "waitlist.ask_contact"),
            keyboard=kb.skip_keyboard(await get_text(session, client.language, "common.skip")),
        )
    if step is Step.waitlist_contact:
        return await _submit_waitlist(session, client)
    return None


# --- Admin (§13.2) ----------------------------------------------------------


#: §13.2: rows a page, and how much of a thread is worth reading on a phone.
ADMIN_PAGE = 5
ADMIN_SESSIONS_SHOWN = 10
ADMIN_THREAD_SHOWN = 3

#: The status glyphs the queue is scanned by.
ADMIN_GLYPHS = {
    RequestStatus.pending: "⏳",
    RequestStatus.negotiating: "💬",
    RequestStatus.confirmed: "✅",
}


async def _panel(
    session: AsyncSession, *, edit: bool = False, toast: str | None = None
) -> Reply:
    """§13.2's root screen: what needs answering, and the way to each of them."""
    settings = get_settings()
    practice = await get_practice(session)

    pending = await booking.count_with_status(session, RequestStatus.pending)
    negotiating = await booking.count_with_status(session, RequestStatus.negotiating)
    waiting = await waitlist.count_open(session)
    soonest = await booking.upcoming_sessions(session, within=timedelta(days=365), limit=1)

    lines = [
        f"Availability: {'on' if practice.availability_on else 'off'}",
        f"Pending: {pending}   Negotiating: {negotiating}",
    ]
    if soonest:
        lines.append(f"Next session: {await _admin_line(session, soonest[0], practice)}")
    else:
        lines.append("Next session: none booked")
    lines.append(f"Waitlist: {waiting}")
    lines.append(f"Admin UI: {settings.base_url}/admin")

    return Reply(
        "\n".join(lines),
        keyboard=kb.panel_keyboard(
            [
                [(f"Requests ({pending + negotiating})", f"{kb.PANEL_REQUESTS}:0")],
                [
                    ("Sessions", f"{kb.PANEL_SESSIONS}:2"),
                    (f"Waitlist ({waiting})", f"{kb.PANEL_WAITLIST}:0"),
                ],
                [
                    (
                        f"Availability: turn {'off' if practice.availability_on else 'on'}",
                        kb.PANEL_AVAILABILITY,
                    ),
                    ("Refresh", kb.PANEL),
                ],
            ]
        ),
        edit=edit,
        toast=toast,
    )


async def _admin_line(
    session: AsyncSession, request: BookingRequest, practice: Practice
) -> str:
    """One request, as a line to scan: when, and who."""
    when = await booking.requested_start(session, request)
    zone = ZoneInfo(practice.timezone)
    stamp = (
        when.astimezone(zone).strftime("%a %d %b %H:%M")
        if when
        else (request.desired_time_text or "no time yet")
    )
    name = request.display_name or await _client_display_name(session, request.client_id)
    return f"{stamp} · {escape_telegram(name or 'no name')}"


async def _admin_requests(
    session: AsyncSession, page: int, *, toast: str | None = None
) -> Reply:
    """§13.2: the queue, five a page, every row a way into the request."""
    practice = await get_practice(session)
    total = await booking.count_with_status(
        session, RequestStatus.pending, RequestStatus.negotiating
    )
    rows = await booking.queue_for_admin(session, limit=ADMIN_PAGE, offset=page * ADMIN_PAGE)

    if not rows:
        return Reply(
            "No open requests." if page == 0 else "Nothing further.",
            keyboard=kb.panel_keyboard([[("⌂ Panel", kb.PANEL)]]),
            edit=True,
            toast=toast,
        )

    pages = max(1, -(-total // ADMIN_PAGE))
    buttons = [
        [
            (
                f"{ADMIN_GLYPHS.get(request.status, '·')} "
                f"{await _admin_line(session, request, practice)}",
                f"{kb.PANEL_OPEN}:{request.id}",
            )
        ]
        for request in rows
    ]

    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("← Prev", f"{kb.PANEL_REQUESTS}:{page - 1}"))
    if (page + 1) * ADMIN_PAGE < total:
        nav.append(("Next →", f"{kb.PANEL_REQUESTS}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([("⌂ Panel", kb.PANEL)])

    return Reply(
        f"Open requests — page {page + 1} of {pages}",
        keyboard=kb.panel_keyboard(buttons),
        edit=True,
        toast=toast,
    )


async def _admin_request(
    session: AsyncSession, request: BookingRequest, *, toast: str | None = None
) -> Reply:
    """§13.2: one request in full, with only the actions §7.1 permits.

    `problem_text` belongs here: this is an admin surface, and DESIGN.md §16
    names the therapist's own Telegram account as one of the two places it is
    visible. It is never logged (hard rule 8).
    """
    practice = await get_practice(session)
    client = (
        await session.execute(select(Client).where(Client.id == request.client_id))
    ).scalar_one_or_none()

    when = await booking.requested_start(session, request)
    zone = ZoneInfo(practice.timezone)
    lines = [
        f"{ADMIN_GLYPHS.get(request.status, '·')} {request.status.value}",
        str(request.uuid),
        # §12.2: the request carries whatever name it was submitted under, which
        # for a web booking is usually nothing. The client may still have one.
        f"Client: {escape_telegram(request.display_name or _client_name(client) or 'no name')}",
    ]
    # §13.2: how to answer somebody who left no name and no contact note. This
    # is the surface for triaging away from a desk, and a request nobody can be
    # reached about is not triageable.
    for label in _identity_labels(await identities_for(session, request.client_id)):
        lines.append(escape_telegram(label))
    if when is not None:
        lines.append(f"When: {when.astimezone(zone).strftime('%a %d %b %H:%M')} ({zone.key})")
        client_zone = request.client_timezone or (client.timezone if client else None)
        if client_zone and client_zone != practice.timezone:
            local = when.astimezone(ZoneInfo(client_zone)).strftime("%H:%M")
            lines.append(f"Client's time: {local} ({client_zone})")
    elif request.desired_time_text:
        lines.append(f"Asked for: {escape_telegram(request.desired_time_text)}")

    session_type = (
        await session.execute(
            select(SessionType.code).where(SessionType.id == request.session_type_id)
        )
    ).scalar_one_or_none()
    lines.append(f"{session_type or 'session'} · {request.modality.value}")
    if request.contact_note:
        lines.append(f"Contact: {escape_telegram(request.contact_note)}")
    if request.problem_text:
        lines.append(f"\n{escape_telegram(request.problem_text)}")

    thread = await _admin_thread(session, request.id)
    if thread:
        lines.append("")
        lines.extend(thread)

    # §7.1 decides what may be offered, so the panel can never propose what the
    # core would refuse.
    allowed = booking.ALLOWED[request.status]
    actions = [
        (label, f"{action}:{request.id}")
        for event, label, action in (
            ("admin_approve", "Approve", kb.APPROVE),
            ("admin_propose", "Propose", kb.PROPOSE),
            ("admin_reject", "Reject", kb.REJECT),
            ("admin_cancel", "Cancel", kb.CANCEL_REQUEST),
        )
        if event in allowed
    ]

    buttons = [actions] if actions else []
    buttons.append(_admin_nav())
    return Reply(
        "\n".join(lines), keyboard=kb.panel_keyboard(buttons), edit=True, toast=toast
    )


async def _admin_thread(session: AsyncSession, request_id: int) -> list[str]:
    """The last few turns, oldest of them first."""
    from app.core.models import NegotiationMessage

    rows = (
        (
            await session.execute(
                select(NegotiationMessage)
                .where(NegotiationMessage.request_id == request_id)
                .order_by(NegotiationMessage.created_at.desc(), NegotiationMessage.id.desc())
                .limit(ADMIN_THREAD_SHOWN)
            )
        )
        .scalars()
        .all()
    )
    return [
        f"{message.sender.value}: {escape_telegram(message.body_text or message.kind.value)}"
        for message in reversed(rows)
    ]


async def _admin_sessions(session: AsyncSession, days: int) -> Reply:
    """§13.2: what is actually booked, soonest first."""
    practice = await get_practice(session)
    rows = await booking.upcoming_sessions(
        session, within=timedelta(days=days), limit=ADMIN_SESSIONS_SHOWN
    )
    window = "today and tomorrow" if days == 2 else f"the next {days} days"

    switch = (
        ("Next 7 days", f"{kb.PANEL_SESSIONS}:7")
        if days == 2
        else ("Today & tomorrow", f"{kb.PANEL_SESSIONS}:2")
    )
    if not rows:
        return Reply(
            f"Nothing confirmed in {window}.",
            keyboard=kb.panel_keyboard([[switch], [("⌂ Panel", kb.PANEL)]]),
            edit=True,
        )

    lines = [f"Confirmed in {window}:"]
    buttons: list[list[tuple[str, str]]] = []
    for request in rows:
        line = await _admin_line(session, request, practice)
        join = await notifications.join_info(session, request)
        lines.append(f"{line} · {request.modality.value}" + (f"\n{join}" if join else ""))
        buttons.append([(line, f"{kb.PANEL_OPEN}:{request.id}")])

    buttons.append([switch])
    buttons.append([("⌂ Panel", kb.PANEL)])
    return Reply("\n".join(lines), keyboard=kb.panel_keyboard(buttons), edit=True)


async def _admin_waitlist(session: AsyncSession, page: int) -> Reply:
    """§13.2: read-only. Working an entry means writing to someone, which is a
    keyboard job rather than a phone job."""
    settings = get_settings()
    rows = await waitlist.recent(session, limit=ADMIN_PAGE, offset=page * ADMIN_PAGE)
    if not rows:
        return Reply(
            "The waitlist is empty." if page == 0 else "Nothing further.",
            keyboard=kb.panel_keyboard([[("⌂ Panel", kb.PANEL)]]),
            edit=True,
        )

    practice = await get_practice(session)
    lines = [f"Waitlist — page {page + 1}"]
    for entry in rows:
        # The entry itself carries no name; the person it belongs to does.
        name = (
            await session.execute(
                select(Client.display_name).where(Client.id == entry.client_id)
            )
        ).scalar_one_or_none()
        joined = entry.created_at.astimezone(ZoneInfo(practice.timezone))
        parts = [escape_telegram(name or "no name"), entry.status.value]
        if entry.contact_note:
            parts.append(escape_telegram(entry.contact_note))
        lines.append(f"{joined.strftime('%d %b')} · " + " · ".join(parts))
    lines.append(f"\n{settings.base_url}/admin/waitlist")

    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("← Prev", f"{kb.PANEL_WAITLIST}:{page - 1}"))
    if len(rows) == ADMIN_PAGE:
        nav.append(("Next →", f"{kb.PANEL_WAITLIST}:{page + 1}"))

    buttons = [nav] if nav else []
    buttons.append([("⌂ Panel", kb.PANEL)])
    return Reply("\n".join(lines), keyboard=kb.panel_keyboard(buttons), edit=True)


async def _admin(session: AsyncSession, update: Update, admin_ids: frozenset[int]) -> Reply | None:
    """§13.2: a reduced surface, gated by TELEGRAM_ADMIN_IDS.

    The bot has no other way to authenticate the therapist (DESIGN.md §5.2).
    Content, translations, settings, and slot creation are web-only and reply
    with a link rather than trying to be a phone-sized admin UI.
    """
    if update.chat_id not in admin_ids:
        return None  # Silence, not an error: an unknown chat learns nothing.

    argument = (update.text or "").partition(" ")[2].strip()
    if argument == "availability":
        # The command still works, for muscle memory and for scripts.
        practice = await get_practice(session)
        practice.availability_on = not practice.availability_on
        await session.flush()

    # §13.2: /admin abandons a half-typed answer, the way a menu label does in
    # §13.1. Reaching for the panel is not an answer to the last question.
    await _clear_admin_input(session, update.chat_id)
    return await _panel(session)


async def _client_by_uuid(session: AsyncSession, request_uuid: UUID) -> Client:
    request = await booking.get_by_uuid(session, request_uuid)
    return (
        await session.execute(select(Client).where(Client.id == request.client_id))
    ).scalar_one()
