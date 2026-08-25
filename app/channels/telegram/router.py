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
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.telegram import keyboards as kb
from app.config import get_settings
from app.core.enums import Channel, Modality, RequestStatus, SenderType, TokenPurpose
from app.core.errors import DomainError, SlotUnavailable, TokenInvalid
from app.core.models import BookingRequest, Client, Practice, SessionType, TimezoneOption
from app.core.policies import BookingPath, now_utc, resolve_booking_mode
from app.core.services import booking, content, flow, notifications, waitlist
from app.core.services.clients import (
    consume_token,
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

logger = logging.getLogger(__name__)

#: How far ahead the slot picker looks.
SLOT_WINDOW = timedelta(days=30)

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
        session_type = await get_text(
            session,
            client.language,
            f"booking.type.{await _session_type_code(session, request.session_type_id)}",
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
    return Reply(
        await get_text(session, client.language, "booking.choose_slot", timezone=tz),
        keyboard=kb.slot_keyboard(slots, tz),
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
        name = await get_text(session, client.language, f"booking.type.{session_type.code}")
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
                session, desired_time_text=scratch.get("desired_time", ""), **common
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

    try:
        await booking.admin_propose(session, request.id, proposed_start=proposed, body_text=text)
    except DomainError as exc:
        await flow.clear(session, client.id, Channel.telegram)
        return Reply(f"Not possible: {type(exc).__name__}.")

    await notifications.publish(session)
    await flow.clear(session, client.id, Channel.telegram)
    return Reply(f"Proposed to {request.uuid}.")


def _parse_time(text: str, tz: str) -> datetime | None:
    """`YYYY-MM-DD HH:MM` in `tz` -> an aware UTC instant, or None.

    None is not a failure: §9 says a structured time is *preferred*, not
    required, and the words are kept either way.
    """
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M"):
        try:
            naive = datetime.strptime(text.strip(), fmt)  # noqa: DTZ007 - zone applied next
        except ValueError:
            continue
        return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)
    return None


# --- Negotiation, client side (§7.1) ----------------------------------------


async def _negotiation_action(
    session: AsyncSession, client: Client, action: str, argument: str
) -> Reply | None:
    """The buttons on a `request.proposal.client` message.

    Every rule lives in app/core/services/booking.py; this reports the outcome.
    A refusal is answered with an explanation rather than silence -- the client
    pressed a button and deserves to know it did nothing.
    """
    try:
        request_id = int(argument)
    except ValueError:
        return None

    request = await _request_for(session, client, request_id)
    if request is None:
        return Reply(await get_text(session, client.language, "common.error.not_found"))

    if action == kb.COUNTER:
        # §9: a counter needs words, and a callback carries none. Park the
        # request id and ask.
        await flow.set_step(
            session,
            client.id,
            Channel.telegram,
            Step.entering_counter,
            replace={"request_id": request.id},
        )
        return Reply(await get_text(session, client.language, "intent.request.counter.client.ask"))

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


async def _admin_action(
    session: AsyncSession, chat_id: int, action: str, argument: str
) -> Reply | None:
    """The buttons on an admin notification.

    §13.2 keeps approve, propose, reject and cancel on the phone; content,
    translations, settings and slot creation stay web-only.
    """
    settings = get_settings()
    if chat_id not in settings.admin_telegram_ids:
        return None  # Silence: an unknown chat learns nothing.

    try:
        request_id = int(argument)
    except ValueError:
        return None

    request = (
        await session.execute(select(BookingRequest).where(BookingRequest.id == request_id))
    ).scalar_one_or_none()
    if request is None:
        return Reply("That request no longer exists.")

    if action == kb.PROPOSE:
        # A time needs typing, so park the request and ask for it.
        admin_client = await resolve_client(session, Channel.telegram, str(chat_id), verified=True)
        await flow.set_step(
            session,
            admin_client.id,
            Channel.telegram,
            Step.admin_entering_proposal,
            replace={"request_id": request.id},
        )
        return Reply(
            f"Propose a time for {request.uuid}. "
            "Send it as YYYY-MM-DD HH:MM in the practice timezone, or send words."
        )

    try:
        if action == kb.APPROVE:
            confirmed = await booking.admin_approve(session, request.id)
            await notifications.publish(session)
            return Reply(f"Confirmed {confirmed.uuid}.")
        if action == kb.REJECT:
            await booking.admin_reject(session, request.id)
            await notifications.publish(session)
            return Reply(f"Rejected {request.uuid}.")
        await booking.admin_cancel(session, request.id, reason="cancelled from Telegram")
        await notifications.publish(session)
        return Reply(f"Cancelled {request.uuid}.")
    except DomainError as exc:
        return Reply(f"Not possible: {type(exc).__name__}.")


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


async def _admin(session: AsyncSession, update: Update, admin_ids: frozenset[int]) -> Reply | None:
    """§13.2: a reduced surface, gated by TELEGRAM_ADMIN_IDS.

    The bot has no other way to authenticate the therapist (DESIGN.md §5.2).
    Content, translations, settings, and slot creation are web-only and reply
    with a link rather than trying to be a phone-sized admin UI.
    """
    if update.chat_id not in admin_ids:
        return None  # Silence, not an error: an unknown chat learns nothing.

    settings = get_settings()
    practice = await get_practice(session)
    _, _, argument = (update.text or "").partition(" ")

    if argument.strip() == "availability":
        practice.availability_on = not practice.availability_on
        await session.flush()
        state = "on" if practice.availability_on else "off"
        return Reply(f"Availability is now {state}.")

    from app.core.enums import RequestStatus
    from app.core.models import BookingRequest

    open_requests = (
        (
            await session.execute(
                select(BookingRequest)
                .where(
                    BookingRequest.status.in_((RequestStatus.pending, RequestStatus.negotiating))
                )
                .order_by(BookingRequest.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )

    lines = [
        f"Availability: {'on' if practice.availability_on else 'off'}",
        f"Open requests: {len(open_requests)}",
    ]
    # `uuid` MUST appear in every admin notification (§6.5). problem_text MUST
    # NOT (hard rule 8).
    lines += [f"  {r.uuid} — {r.status.value}" for r in open_requests]
    lines.append(f"Admin UI: {settings.base_url}/admin")
    return Reply("\n".join(lines))


async def _client_by_uuid(session: AsyncSession, request_uuid: UUID) -> Client:
    request = await booking.get_by_uuid(session, request_uuid)
    return (
        await session.execute(select(Client).where(Client.id == request.client_id))
    ).scalar_one()
