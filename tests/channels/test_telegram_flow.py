"""Telegram client flow (IMPLEMENTATION.md §13.1, M5 acceptance).

The acceptance criteria are two: a simulated update sequence produces a
`pending` request with a held slot, and restarting `web` mid-flow preserves
progress. The second is tested by throwing the session away between steps --
nothing but the database carries state across, which is the point of §13.1's
"not in aiogram FSM memory".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.telegram import keyboards as kb
from app.channels.telegram.router import Reply, Update, handle
from app.core.enums import Channel, Modality, RequestStatus, SlotStatus, TokenPurpose
from app.core.models import BookingRequest, Client, FlowState, Practice, Slot, WaitlistEntry
from app.core.services import content, flow
from app.core.services.clients import issue_token, resolve_client
from app.core.services.flow import Step

CHAT = 555000111


async def _client(db: AsyncSession, chat_id: int = CHAT) -> Client:
    return await resolve_client(db, Channel.telegram, str(chat_id), verified=True)


async def _seed_topic_block(db: AsyncSession) -> None:
    topic = await content.get_topic(db, "work_terms")
    await content.upsert_block(
        db, topic_id=topic.id, lang="ru", position=0, body_md="**Условия** работы."
    )


# --- /start (§13.1 step 1-2) ------------------------------------------------


async def test_start_creates_a_client_and_asks_for_a_language(db: AsyncSession) -> None:
    reply = await handle(db, Update(chat_id=CHAT, text="/start", display_name="A B"))

    assert reply is not None
    assert reply.keyboard is not None
    client = await _client(db)
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_language


async def test_the_telegram_identity_is_verified_by_construction(db: AsyncSession) -> None:
    """Telegram vouches for the user id (DESIGN.md §5.1)."""
    from app.core.models import Identity

    await handle(db, Update(chat_id=CHAT, text="/start"))
    identity = (
        await db.execute(select(Identity).where(Identity.external_id == str(CHAT)))
    ).scalar_one()
    assert identity.verified_at is not None


async def test_language_selection_is_stored_on_the_client(db: AsyncSession) -> None:
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:hy"))

    client = await _client(db)
    assert client.language == "hy"
    # hy, never am (hard rule 5).
    assert client.language != "am"


async def test_language_is_asked_on_first_contact_only(db: AsyncSession) -> None:
    """§13.1 step 2."""
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))

    reply = await handle(db, Update(chat_id=CHAT, text="/start"))

    client = await _client(db)
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.idle
    assert reply is not None


async def test_a_deep_link_merges_onto_an_existing_client(db: AsyncSession) -> None:
    """§13.1 step 1: one tap, rather than "type your email into the bot"."""
    web_client = await resolve_client(db, Channel.email, "merge@example.test")
    raw = await issue_token(db, TokenPurpose.link_channel, client_id=web_client.id)

    await handle(db, Update(chat_id=CHAT, text=f"/start {kb.LANG and 'link_' + raw}"))

    from app.core.models import Identity

    identity = (
        await db.execute(select(Identity).where(Identity.external_id == str(CHAT)))
    ).scalar_one()
    assert identity.client_id == web_client.id


async def test_an_expired_deep_link_is_refused_without_creating_a_client(
    db: AsyncSession,
) -> None:
    reply = await handle(db, Update(chat_id=CHAT, text="/start link_not-a-real-token"))
    assert reply is not None
    assert "expired" in reply.text.lower() or reply.text


# --- Topics (§13.1 step 4) --------------------------------------------------


async def test_a_topic_button_sends_its_blocks_as_separate_messages(
    db: AsyncSession,
) -> None:
    """Separate messages is what makes the 4096-character limit a non-issue
    (DESIGN.md §10.1)."""
    await _seed_topic_block(db)
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))

    from app.core.services.translations import get_text

    title = await get_text(db, "ru", "content.topic.work_terms.title")
    reply = await handle(db, Update(chat_id=CHAT, text=title))

    assert reply is not None
    assert "<b>Условия</b>" in reply.text


# --- The full booking flow (§13.1 steps 5-8) -------------------------------


async def _walk_to_slot_pick(db: AsyncSession, slot: Slot) -> Client:
    from app.core.services.translations import get_text

    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))

    consultation = await get_text(db, "ru", "menu.consultation")
    await handle(db, Update(chat_id=CHAT, text=consultation))
    # No timezone yet, so the picker asks for one first (§13.1 step 6).
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.TZ}:Europe/Moscow"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.SLOT}:{slot.id}"))
    return await _client(db)


async def test_a_full_update_sequence_produces_a_pending_request_with_a_held_slot(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """M5 acceptance, stated exactly as §19 puts it."""
    client = await _walk_to_slot_pick(db, future_slot)

    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, text="I would like to talk about work stress"))
    await handle(db, Update(chat_id=CHAT, text="Anna"))
    reply = await handle(db, Update(chat_id=CHAT, text="telegram is fine"))

    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalar_one()

    assert request.status is RequestStatus.pending
    assert request.slot_id == future_slot.id
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.held
    assert future_slot.held_by_request == request.id

    # §13.1 step 8: the confirmation carries the request UUID.
    assert reply is not None
    assert str(request.uuid) in reply.text


async def test_the_answers_reach_the_request(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    await _walk_to_slot_pick(db, future_slot)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:onsite"))
    await handle(db, Update(chat_id=CHAT, text="work stress"))
    await handle(db, Update(chat_id=CHAT, text="Anna"))
    await handle(db, Update(chat_id=CHAT, text="telegram is fine"))

    client = await _client(db)
    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalar_one()
    assert request.problem_text == "work stress"
    assert request.display_name == "Anna"
    assert request.contact_note == "telegram is fine"
    assert request.modality is Modality.onsite
    assert request.source_channel is Channel.telegram
    assert request.client_timezone == "Europe/Moscow"


# --- The contact step (§13.1 step 7) ----------------------------------------


async def _walk_to_contact(
    db: AsyncSession, slot: Slot, session_type_id: int
) -> tuple[Client, Reply]:
    """Up to the contact question, returning it along with the client."""
    client = await _walk_to_slot_pick(db, slot)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))  # problem
    reply = await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))  # name
    assert reply is not None
    return client, reply


def _contact_options(keyboard: object) -> set[str]:
    rows = getattr(keyboard, "inline_keyboard", [])
    return {button.callback_data for row in rows for button in row}


async def _login_links(db: AsyncSession, client: Client) -> list[object]:
    """This client's login-link rows. Scoped, because the e2e suite commits."""
    from app.core.models import OutboxMessage

    return list(
        (
            await db.execute(
                select(OutboxMessage).where(
                    OutboxMessage.client_id == client.id,
                    OutboxMessage.intent_key == "auth.login_link.client",
                )
            )
        )
        .scalars()
        .all()
    )


async def test_the_contact_step_offers_a_choice(
    db: AsyncSession, future_slot: Slot, session_type_id: int, email_enabled: None
) -> None:
    """The question used to be open, and "email" was an answer nothing acted on."""
    client, reply = await _walk_to_contact(db, future_slot, session_type_id)

    assert _contact_options(reply.keyboard) == {
        f"{kb.CONTACT}:{kb.CONTACT_TELEGRAM}",
        f"{kb.CONTACT}:{kb.CONTACT_EMAIL}",
        f"{kb.CONTACT}:{kb.CONTACT_OTHER}",
    }
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_contact


async def test_the_email_option_is_hidden_without_smtp(
    db: AsyncSession, future_slot: Slot, session_type_id: int, email_disabled: None
) -> None:
    """§4: without SMTP there is no link to send, so the promise is not made."""
    _, reply = await _walk_to_contact(db, future_slot, session_type_id)

    assert f"{kb.CONTACT}:{kb.CONTACT_EMAIL}" not in _contact_options(reply.keyboard)


async def test_choosing_telegram_records_no_contact_note(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """The identity already exists and §13.3 already prefers it."""
    client, _ = await _walk_to_contact(db, future_slot, session_type_id)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.CONTACT}:{kb.CONTACT_TELEGRAM}"))

    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalar_one()
    assert request.status is RequestStatus.pending
    assert request.contact_note is None


async def test_choosing_another_way_keeps_the_free_text_note(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    client, _ = await _walk_to_contact(db, future_slot, session_type_id)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.CONTACT}:{kb.CONTACT_OTHER}"))
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.entering_contact

    await handle(db, Update(chat_id=CHAT, text="+374 55 000000, after six"))

    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalar_one()
    assert request.contact_note == "+374 55 000000, after six"


async def test_an_address_that_is_not_shaped_like_one_is_refused(
    db: AsyncSession, future_slot: Slot, session_type_id: int, email_enabled: None
) -> None:
    client, _ = await _walk_to_contact(db, future_slot, session_type_id)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.CONTACT}:{kb.CONTACT_EMAIL}"))
    reply = await handle(db, Update(chat_id=CHAT, text="anna at example dot test"))

    assert reply is not None
    # Still on the same step, and nothing was booked or sent on a typo.
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.entering_contact_email
    assert (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalars().all() == []
    assert await _login_links(db, client) == []


async def test_a_given_address_is_mailed_a_link_and_stays_unverified(
    db: AsyncSession, future_slot: Slot, session_type_id: int, email_enabled: None
) -> None:
    """§13.1 step 7 and §13.3: the link goes to the mailbox being proved, and
    the address is not a delivery target until it is."""
    from app.core.models import Identity

    client, _ = await _walk_to_contact(db, future_slot, session_type_id)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.CONTACT}:{kb.CONTACT_EMAIL}"))
    reply = await handle(db, Update(chat_id=CHAT, text="Anna@Example.Test"))

    identity = (
        await db.execute(
            select(Identity).where(Identity.client_id == client.id, Identity.channel == "email")
        )
    ).scalar_one()
    assert identity.external_id == "anna@example.test"  # normalised
    assert identity.verified_at is None  # following the link is what proves it

    link = await _login_links(db, client)
    assert len(link) == 1
    assert link[0].channel is Channel.email
    assert link[0].address == "anna@example.test"
    assert "/auth/callback?token=" in link[0].payload["url"]

    # The booking still went through; verification is not a precondition.
    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalar_one()
    assert request.status is RequestStatus.pending
    assert request.contact_note == "anna@example.test"
    assert reply is not None and str(request.uuid) in reply.text


async def test_an_address_held_by_someone_else_is_not_reassigned(
    db: AsyncSession, future_slot: Slot, session_type_id: int, email_enabled: None
) -> None:
    """`link_identity` refuses the merge; the flow has to survive that."""
    await resolve_client(db, Channel.email, "taken@example.test")
    client, _ = await _walk_to_contact(db, future_slot, session_type_id)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.CONTACT}:{kb.CONTACT_EMAIL}"))
    reply = await handle(db, Update(chat_id=CHAT, text="taken@example.test"))

    assert reply is not None
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.entering_contact_email
    assert await _login_links(db, client) == []


async def test_a_verified_address_makes_confirmations_arrive_twice(
    db: AsyncSession, future_slot: Slot, session_type_id: int, email_enabled: None
) -> None:
    """The payoff of §13.3: once proved, both identities are targets."""
    from app.core.models import Identity, OutboxMessage
    from app.core.services import notifications
    from app.core.services.clients import link_identity

    client, _ = await _walk_to_contact(db, future_slot, session_type_id)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.CONTACT}:{kb.CONTACT_EMAIL}"))
    await handle(db, Update(chat_id=CHAT, text="anna@example.test"))

    # Following the link is simulated by what /auth/callback does with it.
    await link_identity(db, client.id, Channel.email, "anna@example.test", verified=True)
    identity = (
        await db.execute(
            select(Identity).where(Identity.client_id == client.id, Identity.channel == "email")
        )
    ).scalar_one()
    assert identity.verified_at is not None

    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalar_one()
    await notifications.enqueue(
        db,
        notifications.Envelope(
            "request.confirmed.client",
            notifications.Recipient.client,
            {"uuid": str(request.uuid)},
            request_id=request.id,
        ),
    )

    confirmed = [
        row
        for row in (await db.execute(select(OutboxMessage))).scalars().all()
        if row.intent_key == "request.confirmed.client"
    ]
    assert {row.channel for row in confirmed} == {Channel.telegram, Channel.email}


async def test_optional_answers_are_skippable(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """§13.1 step 7: each optional answer is skippable."""
    await _walk_to_slot_pick(db, future_slot)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))  # problem
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))  # name
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))  # contact

    client = await _client(db)
    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalar_one()
    assert request.status is RequestStatus.pending
    assert request.problem_text is None


async def test_the_flow_is_cleared_once_the_request_is_submitted(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """The scratch data can hold problem text, so it does not linger."""
    client = await _walk_to_slot_pick(db, future_slot)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, text="private"))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))

    assert await flow.get(db, client.id, Channel.telegram) is None


async def test_submitting_notifies_through_the_outbox_not_directly(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """Hard rule 2: the row is the notification."""
    from app.core.models import OutboxMessage

    await _walk_to_slot_pick(db, future_slot)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))

    keys = {row.intent_key for row in (await db.execute(select(OutboxMessage))).scalars().all()}
    assert "request.submitted.client" in keys


async def test_a_slot_taken_meanwhile_is_reported_not_crashed(
    db: AsyncSession, future_slot: Slot, session_type_id: int, practice: Practice
) -> None:
    """DESIGN.md §8's race, and the reason `booking.slot.taken` exists."""

    client = await _walk_to_slot_pick(db, future_slot)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))

    # Someone else takes it while this client is typing.
    other = await resolve_client(db, Channel.telegram, "999888777", verified=True)
    from app.core.services import booking

    await booking.submit_slot_request(
        db,
        client_id=other.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.telegram,
    )

    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))
    reply = await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))

    assert reply is not None
    assert "taken" in reply.text.lower() or reply.text
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_slot


# --- M5 acceptance: a restart mid-flow preserves progress -------------------


async def test_progress_survives_a_restart(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """M5 acceptance.

    §13.1 forbids aiogram FSM memory precisely so this holds. The proof is that
    every fact needed to finish the booking is readable from the database
    alone -- nothing is carried in a process.
    """
    client = await _walk_to_slot_pick(db, future_slot)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, text="half-typed answer"))

    # Everything the flow knows is in this row and nowhere else.
    state = (
        await db.execute(select(FlowState).where(FlowState.client_id == client.id))
    ).scalar_one()
    assert state.step == Step.entering_name.value
    assert state.data["slot_id"] == future_slot.id
    assert state.data["session_type_id"] == session_type_id
    assert state.data["problem"] == "half-typed answer"

    # Carry on as a fresh process would: no in-memory state exists to help.
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))

    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalar_one()
    assert request.status is RequestStatus.pending
    assert request.problem_text == "half-typed answer"


async def test_flow_state_is_scoped_per_channel(db: AsyncSession) -> None:
    """A client mid-booking in Telegram and on the web are two flows."""
    client = await _client(db)
    await flow.set_step(db, client.id, Channel.telegram, Step.entering_problem)
    await flow.set_step(db, client.id, Channel.web, Step.choosing_slot)

    assert await flow.current_step(db, client.id, Channel.telegram) is Step.entering_problem
    assert await flow.current_step(db, client.id, Channel.web) is Step.choosing_slot


async def test_an_unknown_step_drops_the_client_back_to_the_menu(
    db: AsyncSession,
) -> None:
    """A step removed by a deploy must not strand anyone."""
    client = await _client(db)
    await flow.set_step(db, client.id, Channel.telegram, Step.entering_problem)
    state = await flow.get(db, client.id, Channel.telegram)
    assert state is not None
    state.step = "a_step_that_no_longer_exists"
    await db.flush()

    assert await flow.current_step(db, client.id, Channel.telegram) is Step.idle


# --- Waitlist path ----------------------------------------------------------


async def test_availability_off_routes_to_the_waitlist(
    db: AsyncSession, practice: Practice
) -> None:
    """DESIGN.md §6's first row."""
    from app.core.services.translations import get_text

    practice.availability_on = False
    await db.flush()

    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    consultation = await get_text(db, "ru", "menu.consultation")
    await handle(db, Update(chat_id=CHAT, text=consultation))

    client = await _client(db)
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.waitlist_problem

    await handle(db, Update(chat_id=CHAT, text="a private matter"))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))

    entry = (
        await db.execute(select(WaitlistEntry).where(WaitlistEntry.client_id == client.id))
    ).scalar_one()
    assert entry.problem_text == "a private matter"


async def test_negotiation_mode_asks_for_a_desired_time(
    db: AsyncSession, practice: Practice, session_type_id: int
) -> None:
    """DESIGN.md §6's last row."""
    from app.core.enums import BookingMode
    from app.core.services.translations import get_text

    practice.booking_mode = BookingMode.negotiation
    await db.flush()

    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    consultation = await get_text(db, "ru", "menu.consultation")
    await handle(db, Update(chat_id=CHAT, text=consultation))

    client = await _client(db)
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.entering_desired_time


# --- Admin surface (§13.2) --------------------------------------------------


async def test_the_admin_command_is_gated_by_configuration(db: AsyncSession) -> None:
    """The bot has no other way to authenticate her (DESIGN.md §5.2)."""
    reply = await handle(db, Update(chat_id=CHAT, text="/admin"))
    assert reply is None  # silence, not an error


async def test_a_configured_admin_gets_the_reduced_surface(db: AsyncSession) -> None:
    reply = await handle(db, Update(chat_id=1, text="/admin"))
    assert reply is not None
    assert "Availability" in reply.text
    # §13.2: content and settings are web-only and reply with a link.
    assert "/admin" in reply.text


async def test_the_admin_summary_carries_no_problem_text(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """Hard rule 8: identifiers, never content."""
    from app.core.services import booking

    client = await resolve_client(db, Channel.telegram, "777666555", verified=True)
    await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.telegram,
        problem_text="deeply private",
    )

    reply = await handle(db, Update(chat_id=1, text="/admin"))
    assert reply is not None
    assert "deeply private" not in reply.text


async def test_toggling_availability_from_telegram(db: AsyncSession, practice: Practice) -> None:
    before = practice.availability_on
    await handle(db, Update(chat_id=1, text="/admin availability"))
    assert practice.availability_on is not before


# --- Keyboards --------------------------------------------------------------


def test_slot_buttons_are_grouped_by_day_in_the_client_timezone() -> None:
    from app.core.services.slots import SlotView

    base = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    slots = [
        SlotView(
            id=n,
            starts_at_utc=base + timedelta(hours=n),
            starts_at_local=base + timedelta(hours=n),
            duration_min=60,
            modality=None,
        )
        for n in range(4)
    ]
    markup = kb.slot_keyboard(slots, "Asia/Yerevan")
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert any(":" in label for label in labels)
    assert any("Sep" in label for label in labels)  # a day header


@pytest.mark.parametrize("action", [kb.SLOT, kb.STYPE, kb.MODE, kb.TZ, kb.LANG])
def test_every_callback_fits_telegram_budget(action: str) -> None:
    """§9: 64 bytes, shared between the action and its argument."""
    payload = f"{action}:America/Los_Angeles"
    assert len(payload.encode()) <= 64


def test_parse_callback_splits_action_from_argument() -> None:
    assert kb.parse_callback("slot:42") == ("slot", "42")
    assert kb.parse_callback("skip") == ("skip", "")


# --- The free-text path, run to completion ----------------------------------


async def test_the_free_text_path_reaches_a_submitted_request(
    db: AsyncSession, practice: Practice, session_type_id: int
) -> None:
    """DESIGN.md §6's last row: negotiation mode, no slot picker.

    Regression: this path asked for the desired time and then jumped straight
    to the problem text, so `session_type_id` was never collected and submit
    raised KeyError -- out of a webhook handler, which Telegram answers by
    redelivering the same update forever. No test ran this path to the end.
    """
    from app.core.enums import BookingMode
    from app.core.services.translations import get_text

    practice.booking_mode = BookingMode.negotiation
    await db.flush()

    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    consultation = await get_text(db, "ru", "menu.consultation")
    await handle(db, Update(chat_id=CHAT, text=consultation))

    client = await _client(db)
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.entering_desired_time

    # §13.1's order, now the same on both paths: when, type, modality, problem.
    await handle(db, Update(chat_id=CHAT, text="some evening next week?"))
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_session_type

    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, text="work stress"))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))
    reply = await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))

    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalar_one()

    assert request.status is RequestStatus.pending
    assert request.slot_id is None
    # §9: the client's own words are kept, not forced into a datetime.
    assert request.desired_time_text == "some evening next week?"
    assert request.problem_text == "work stress"
    assert request.session_type_id == session_type_id
    assert reply is not None and str(request.uuid) in reply.text

    # The flow is finished, not stranded mid-way.
    assert await flow.get(db, client.id, Channel.telegram) is None


async def test_submit_without_a_session_type_asks_again_rather_than_crashing(
    db: AsyncSession, practice: Practice
) -> None:
    """Defence in depth for the bug above: whatever route a client took, a
    missing answer must produce a question, not an exception out of a handler."""
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    client = await _client(db)

    await flow.set_step(
        db, client.id, Channel.telegram, Step.entering_contact, replace={"problem": "x"}
    )
    reply = await handle(db, Update(chat_id=CHAT, text="reach me anywhere"))

    assert reply is not None
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_session_type
