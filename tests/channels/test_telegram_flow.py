"""Telegram client flow (IMPLEMENTATION.md §13.1, M5 acceptance).

The acceptance criteria are two: a simulated update sequence produces a
`pending` request with a held slot, and restarting `web` mid-flow preserves
progress. The second is tested by throwing the session away between steps --
nothing but the database carries state across, which is the point of §13.1's
"not in aiogram FSM memory".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.telegram import keyboards as kb
from app.channels.telegram.router import Reply, Update, handle
from app.core.enums import Channel, Modality, RequestStatus, SlotStatus, TokenPurpose
from app.core.models import (
    BookingRequest,
    Client,
    FlowState,
    Identity,
    OutboxMessage,
    Practice,
    SessionType,
    Slot,
    WaitlistEntry,
)
from app.core.services import booking, clients, content, flow
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


async def _walk_to_slot_pick(
    db: AsyncSession, slot: Slot, *, modality: str = "online"
) -> Client:
    """§13.1's order: how, then what, then when.

    Modality first, so the picker can filter by an answer it actually has --
    choosing a time and learning afterwards whether it was ever an online time
    is the reported fault. The timezone question follows only for a session the
    client attends from elsewhere; somebody coming to the room is in the room's
    clock already.
    """
    from app.core.services.translations import get_text

    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))

    consultation = await get_text(db, "ru", "menu.consultation")
    await handle(db, Update(chat_id=CHAT, text=consultation))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:{modality}"))

    session_type_id = (
        await db.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))

    if modality == "online":
        await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.TZ}:Europe/Moscow"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.SLOT}:{slot.id}"))
    return await _client(db)


async def test_a_full_update_sequence_produces_a_pending_request_with_a_held_slot(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """M5 acceptance, stated exactly as §19 puts it."""
    client = await _walk_to_slot_pick(db, future_slot)

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
    await _walk_to_slot_pick(db, future_slot, modality="onsite")
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
    # On-site, so the timezone question was never asked: they are coming to the
    # room, and the room's clock is theirs (§13.1). The request records the
    # practice zone rather than a guess about where they live.
    assert request.client_timezone == "Asia/Yerevan"
    client = await _client(db)
    assert client.timezone is None, "nothing was stored that a later booking would inherit"


# --- My appointments (§13.1 step 9) -----------------------------------------


async def _appointments_label(db: AsyncSession) -> str:
    from app.core.services.translations import get_text

    return await get_text(db, "ru", "menu.appointments")


async def _book_and_return_to_menu(
    db: AsyncSession, slot: Slot, session_type_id: int
) -> BookingRequest:
    client = await _walk_to_slot_pick(db, slot)
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))  # problem
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))  # name
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.CONTACT}:{kb.CONTACT_TELEGRAM}"))
    return (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id))
    ).scalar_one()


async def test_the_main_keyboard_offers_my_appointments(db: AsyncSession) -> None:
    """§13.1 step 3."""
    await handle(db, Update(chat_id=CHAT, text="/start"))
    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))

    assert reply is not None
    labels = {button.text for row in reply.keyboard.keyboard for button in row}
    assert await _appointments_label(db) in labels


async def test_a_client_with_nothing_booked_is_told_so(db: AsyncSession) -> None:
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))

    reply = await handle(db, Update(chat_id=CHAT, text=await _appointments_label(db)))

    from app.core.services.translations import get_text

    assert reply is not None
    assert reply.text == await get_text(db, "ru", "menu.appointments.none")


async def test_an_active_request_is_listed_without_its_problem_text(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """Hard rule 8: the status and the time, never what they wrote."""
    client = await _walk_to_slot_pick(db, future_slot)
    await handle(db, Update(chat_id=CHAT, text="something I would not want repeated"))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))  # name
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.CONTACT}:{kb.CONTACT_TELEGRAM}"))

    reply = await handle(db, Update(chat_id=CHAT, text=await _appointments_label(db)))

    assert reply is not None
    assert "something I would not want repeated" not in reply.text
    # The slot's time, in the client's zone, even though nothing is approved yet.
    local = future_slot.starts_at.astimezone(ZoneInfo("Europe/Moscow"))
    assert local.strftime("%Y-%m-%d %H:%M") in reply.text
    assert client.id


async def test_finished_requests_are_not_listed(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """A client asking what they have booked is not asking about a rejection."""
    request = await _book_and_return_to_menu(db, future_slot, session_type_id)
    await booking.admin_reject(db, request.id)

    reply = await handle(db, Update(chat_id=CHAT, text=await _appointments_label(db)))

    from app.core.services.translations import get_text

    assert reply is not None
    assert reply.text == await get_text(db, "ru", "menu.appointments.none")


async def test_a_proposal_awaiting_the_client_is_answerable_from_the_list(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """§13.1 step 9: the proposal's own message may be far up the chat by now."""
    request = await _book_and_return_to_menu(db, future_slot, session_type_id)
    await booking.admin_propose(
        db, request.id, proposed_start=datetime.now(UTC) + timedelta(days=8)
    )

    reply = await handle(db, Update(chat_id=CHAT, text=await _appointments_label(db)))

    assert reply is not None
    data = {b.callback_data for row in reply.keyboard.inline_keyboard for b in row}
    assert data == {
        f"{kb.ACCEPT}:{request.id}",
        f"{kb.COUNTER}:{request.id}",
        f"{kb.DECLINE}:{request.id}",
    }

    # And the button works, through the handler that already existed.
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.ACCEPT}:{request.id}"))
    await db.refresh(request)
    assert request.status is RequestStatus.confirmed


async def test_looking_at_appointments_changes_nothing(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    request = await _book_and_return_to_menu(db, future_slot, session_type_id)
    before = request.status

    await handle(db, Update(chat_id=CHAT, text=await _appointments_label(db)))

    await db.refresh(request)
    assert request.status is before
    assert await flow.current_step(db, request.client_id, Channel.telegram) is Step.idle


async def test_a_menu_button_mid_flow_is_navigation_not_an_answer(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """§13.1: without this the label lands in `problem_text`."""
    client = await _walk_to_slot_pick(db, future_slot)
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.entering_problem

    reply = await handle(db, Update(chat_id=CHAT, text=await _appointments_label(db)))

    assert reply is not None
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.idle
    booked = (
        (await db.execute(select(BookingRequest).where(BookingRequest.client_id == client.id)))
        .scalars()
        .all()
    )
    assert booked == []  # the half-finished booking was abandoned, not submitted


# --- The contact step (§13.1 step 7) ----------------------------------------


async def _walk_to_contact(
    db: AsyncSession, slot: Slot, session_type_id: int
) -> tuple[Client, Reply]:
    """Up to the contact question, returning it along with the client."""
    client = await _walk_to_slot_pick(db, slot)
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
    # §13.1 asks how and what before when on both paths, so the free-text
    # question comes two answers later rather than first.
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_modality

    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))

    # No picker on this path, so the timezone is never asked for either -- the
    # client is about to describe a time in their own words (§9).
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.entering_desired_time
    assert reply is not None
    assert reply.text == await get_text(db, "ru", "booking.ask_desired_time")


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


# --- The admin panel (§13.2) ------------------------------------------------


def _panel_data(reply: Reply) -> set[str]:
    return {b.callback_data for row in reply.keyboard.inline_keyboard for b in row}


async def _admin_request(db: AsyncSession, slot: Slot, session_type_id: int) -> BookingRequest:
    client = await resolve_client(db, Channel.telegram, "777666555", verified=True)
    return await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.telegram,
        problem_text="deeply private",
        display_name="Anna",
    )


async def test_the_panel_reaches_every_screen(db: AsyncSession) -> None:
    """§13.2: the root offers all of them."""
    reply = await handle(db, Update(chat_id=1, text="/admin"))

    assert reply is not None
    data = _panel_data(reply)
    assert f"{kb.PANEL_REQUESTS}:0" in data
    assert f"{kb.PANEL_SESSIONS}:2" in data
    assert f"{kb.PANEL_WAITLIST}:0" in data
    assert kb.PANEL_AVAILABILITY in data
    assert kb.PANEL in data


@pytest.mark.parametrize(
    "callback",
    [
        f"{kb.PANEL_REQUESTS}:0",
        f"{kb.PANEL_SESSIONS}:2",
        f"{kb.PANEL_SESSIONS}:7",
        f"{kb.PANEL_WAITLIST}:0",
    ],
)
async def test_no_screen_is_a_dead_end_even_when_empty(db: AsyncSession, callback: str) -> None:
    """§13.2: every reply is navigable, including the empty ones."""
    reply = await handle(db, Update(chat_id=1, callback_data=callback))

    assert reply is not None
    assert kb.PANEL in _panel_data(reply)
    assert reply.edit is True


async def test_a_request_opens_from_the_queue_with_its_permitted_actions(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """§13.2: the buttons come from §7.1, so the panel cannot offer what the
    core would refuse."""
    request = await _admin_request(db, future_slot, session_type_id)

    listed = await handle(db, Update(chat_id=1, callback_data=f"{kb.PANEL_REQUESTS}:0"))
    assert listed is not None
    assert f"{kb.PANEL_OPEN}:{request.id}" in _panel_data(listed)

    opened = await handle(db, Update(chat_id=1, callback_data=f"{kb.PANEL_OPEN}:{request.id}"))
    assert opened is not None
    data = _panel_data(opened)
    assert f"{kb.APPROVE}:{request.id}" in data  # pending
    assert f"{kb.CANCEL_REQUEST}:{request.id}" not in data  # only from confirmed
    assert kb.PANEL in data
    # This is an admin surface: the problem text belongs here (DESIGN.md §16).
    assert "deeply private" in opened.text
    assert "Anna" in opened.text


async def test_a_confirmed_request_offers_cancel_and_nothing_else(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    request = await _admin_request(db, future_slot, session_type_id)
    await booking.admin_approve(db, request.id)

    opened = await handle(db, Update(chat_id=1, callback_data=f"{kb.PANEL_OPEN}:{request.id}"))

    assert opened is not None
    data = _panel_data(opened)
    assert f"{kb.CANCEL_REQUEST}:{request.id}" in data
    assert f"{kb.APPROVE}:{request.id}" not in data


async def test_an_action_answers_with_the_request_not_with_bare_text(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """The old surface said "Confirmed <uuid>." and left her there."""
    request = await _admin_request(db, future_slot, session_type_id)

    reply = await handle(db, Update(chat_id=1, callback_data=f"{kb.APPROVE}:{request.id}"))

    assert reply is not None
    assert reply.toast == "Confirmed."
    assert kb.PANEL in _panel_data(reply)
    await db.refresh(request)
    assert request.status is RequestStatus.confirmed


async def test_an_action_the_state_machine_refuses_shows_the_current_state(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """Another channel answered first; she gets the request as it now is."""
    request = await _admin_request(db, future_slot, session_type_id)
    await booking.admin_reject(db, request.id)

    reply = await handle(db, Update(chat_id=1, callback_data=f"{kb.APPROVE}:{request.id}"))

    assert reply is not None
    assert reply.toast is not None and "Not possible" in reply.toast
    assert kb.PANEL in _panel_data(reply)


async def test_cancelling_asks_for_a_reason_and_uses_it(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """§13.2: the client is told the reason, so it is asked for."""
    request = await _admin_request(db, future_slot, session_type_id)
    await booking.admin_approve(db, request.id)

    asked = await handle(db, Update(chat_id=1, callback_data=f"{kb.CANCEL_REQUEST}:{request.id}"))
    assert asked is not None
    assert f"{kb.PANEL_SKIP}:{request.id}" in _panel_data(asked)
    await db.refresh(request)
    assert request.status is RequestStatus.confirmed  # nothing cancelled yet

    reply = await handle(db, Update(chat_id=1, text="I am ill"))

    assert reply is not None
    await db.refresh(request)
    assert request.status is RequestStatus.cancelled
    assert request.cancellation_reason == "I am ill"
    # A typed answer has no message of its own to edit.
    assert reply.edit is False


async def test_the_reason_can_be_skipped(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    request = await _admin_request(db, future_slot, session_type_id)
    await booking.admin_approve(db, request.id)
    await handle(db, Update(chat_id=1, callback_data=f"{kb.CANCEL_REQUEST}:{request.id}"))

    reply = await handle(db, Update(chat_id=1, callback_data=f"{kb.PANEL_SKIP}:{request.id}"))

    assert reply is not None
    await db.refresh(request)
    assert request.status is RequestStatus.cancelled
    assert request.cancellation_reason is None


async def test_admin_navigation_abandons_a_half_typed_answer(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """§13.2, the same rule §13.1 gives the client's menu."""
    request = await _admin_request(db, future_slot, session_type_id)
    # Propose now opens the picker, and only "Type it" parks an answer -- which
    # is the state this rule is about.
    await handle(db, Update(chat_id=1, callback_data=f"{kb.PROPOSE_TYPE}:{request.id}"))
    admin_client = await resolve_client(db, Channel.telegram, "1", verified=True)
    assert (
        await flow.current_step(db, admin_client.id, Channel.telegram)
        is Step.admin_entering_proposal
    )

    await handle(db, Update(chat_id=1, text="/admin"))

    assert await flow.current_step(db, admin_client.id, Channel.telegram) is Step.idle


async def test_a_request_erased_under_her_still_navigates(db: AsyncSession) -> None:
    reply = await handle(db, Update(chat_id=1, callback_data=f"{kb.PANEL_OPEN}:99999999"))

    assert reply is not None
    assert "no longer exists" in reply.text
    assert kb.PANEL in _panel_data(reply)


async def test_the_panel_is_gated_like_the_command(db: AsyncSession) -> None:
    """A stranger pressing a panel button learns nothing."""
    assert await handle(db, Update(chat_id=CHAT, callback_data=kb.PANEL)) is None


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
    markup = kb.slot_keyboard(slots, "Asia/Yerevan", {"2026-09-15": "вт 15 сен"})
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert any(":" in label for label in labels)
    # The heading is whatever the router wrote in the client's language; this
    # module no longer has an opinion about the wording (§15).
    assert "вт 15 сен" in labels


def test_a_day_offering_one_time_is_a_single_button() -> None:
    """Reported in use: on a one-slot day the client taps the heading.

    It is the wider, upper half of what looks like one control, and it is a
    `NOOP`, so the tap does nothing and reads as a broken bot. The day and the
    time therefore arrive as one button that books the slot.
    """
    from app.core.services.slots import SlotView

    only = SlotView(
        id=7,
        starts_at_utc=datetime(2026, 9, 15, 6, 0, tzinfo=UTC),
        starts_at_local=datetime(2026, 9, 15, 6, 0, tzinfo=UTC),
        duration_min=60,
        modality=None,
    )
    markup = kb.slot_keyboard([only], "Asia/Yerevan", {"2026-09-15": "вт 15 сен"})
    buttons = [b for row in markup.inline_keyboard for b in row]

    assert len(buttons) == 1, "no heading of its own to mis-tap"
    assert buttons[0].callback_data == f"{kb.SLOT}:7"
    assert "вт 15 сен" in buttons[0].text
    assert "10:00" in buttons[0].text  # 06:00 UTC in Asia/Yerevan
    assert all(b.callback_data != kb.NOOP for b in buttons)


async def test_a_dead_button_never_sends_the_therapist_through_start(
    db: AsyncSession,
) -> None:
    """`NOOP` is not an admin action, and she has no client record.

    Her propose picker is full of dead cells -- weekday letters, padding, the
    hours §13.2 marks taken -- and one mis-tap used to reach `_start`, which
    asks for a language and clears the flow she was in the middle of.
    """
    assert await handle(db, Update(chat_id=1, callback_data=kb.NOOP)) is None

    # `_start` would have created one for her chat on the way to asking.
    identities = (
        await db.execute(select(Identity).where(Identity.external_id == "1"))
    ).scalars().all()
    assert not identities


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
    # §13.1's order, the same on both paths: how, what, when, problem. The
    # free-text path has no picker to filter, but a client is asked how they
    # want to meet before describing when either way.
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_modality

    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_session_type

    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.entering_desired_time

    await handle(db, Update(chat_id=CHAT, text="some evening next week?"))
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


async def test_the_picker_offers_the_waitlist_beside_the_times(
    db: AsyncSession, future_slot: Slot
) -> None:
    """§13.1: times can all be wrong for a client without being absent.

    `resolve_booking_mode` only sends somebody to the waitlist when there is
    nothing to choose from, so a client looking at four times that do not suit
    them had no move left but closing the app.
    """
    from app.core.services.translations import get_text

    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    consultation = await get_text(db, "ru", "menu.consultation")
    await handle(db, Update(chat_id=CHAT, text=consultation))
    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.TZ}:Europe/Moscow"))

    assert reply is not None and reply.keyboard is not None
    data = [b.callback_data for row in reply.keyboard.inline_keyboard for b in row]
    assert kb.WAITLIST in data, "the way out is on the picker"
    # The slot is still bookable: this is an addition, not a replacement.
    assert f"{kb.SLOT}:{future_slot.id}" in data


async def test_the_waitlist_button_reaches_a_waitlist_entry(
    db: AsyncSession, future_slot: Slot
) -> None:
    from app.core.services.translations import get_text

    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    consultation = await get_text(db, "ru", "menu.consultation")
    await handle(db, Update(chat_id=CHAT, text=consultation))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.TZ}:Europe/Moscow"))

    reply = await handle(db, Update(chat_id=CHAT, callback_data=kb.WAITLIST))
    client = await _client(db)
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.waitlist_problem

    # Not "there are no free places" -- they are looking at some (§12.1).
    assert reply is not None
    assert reply.text == await get_text(db, "ru", "waitlist.intro_by_choice")
    assert reply.text != await get_text(db, "ru", "waitlist.intro")

    await handle(db, Update(chat_id=CHAT, text="evenings would suit me better"))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))

    entry = (
        await db.execute(select(WaitlistEntry).where(WaitlistEntry.client_id == client.id))
    ).scalar_one()
    assert entry.problem_text == "evenings would suit me better"

    # The slot they walked past is untouched: they made no request.
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available


# --- Declining with a note (§13.2) ------------------------------------------


async def test_reject_asks_for_a_note_before_it_rejects(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> None:
    """Reported in use: from the phone she could only decline silently.

    `admin_reject` has taken a reason since it was written and the web form has
    always offered the field; this button sent `None`.
    """
    request = BookingRequest(
        practice_id=practice.id,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        status=RequestStatus.pending,
        source_channel=Channel.web,
    )
    db.add(request)
    await db.flush()

    reply = await handle(db, Update(chat_id=1, callback_data=f"{kb.REJECT}:{request.id}"))

    assert reply is not None
    # Nothing has happened yet: the question comes first.
    await db.refresh(request)
    assert request.status is RequestStatus.pending

    admin = await resolve_client(db, Channel.telegram, "1", verified=True)
    assert (
        await flow.current_step(db, admin.id, Channel.telegram)
        is Step.admin_entering_reject_note
    )

    await handle(db, Update(chat_id=1, text="Not my area — Anna at the centre takes these."))

    await db.refresh(request)
    assert request.status is RequestStatus.rejected
    assert request.rejected_reason == "Not my area — Anna at the centre takes these."


async def test_the_note_reaches_the_client_without_a_reason_label(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> None:
    """A referral read as the justification for a refusal is worse than none."""
    request = BookingRequest(
        practice_id=practice.id,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        status=RequestStatus.pending,
        source_channel=Channel.web,
    )
    db.add(request)
    await db.flush()

    await handle(db, Update(chat_id=1, callback_data=f"{kb.REJECT}:{request.id}"))
    await handle(db, Update(chat_id=1, text="Anna at the centre works with exactly this."))

    row = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.request_id == request.id,
                OutboxMessage.intent_key == "request.rejected.client",
            )
        )
    ).scalar_one()
    assert row.payload["reason"] == "Anna at the centre works with exactly this."


async def test_skip_still_declines_and_does_not_cancel(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> None:
    """Two prompts end in Skip now, so it has to know which it is answering.

    Answering the decline prompt with the cancellation's action would refuse:
    §7.1 allows `admin_cancel` only from `confirmed`.
    """
    request = BookingRequest(
        practice_id=practice.id,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        status=RequestStatus.pending,
        source_channel=Channel.web,
    )
    db.add(request)
    await db.flush()

    await handle(db, Update(chat_id=1, callback_data=f"{kb.REJECT}:{request.id}"))
    await handle(db, Update(chat_id=1, callback_data=f"{kb.PANEL_SKIP}:{request.id}"))

    await db.refresh(request)
    assert request.status is RequestStatus.rejected
    assert request.rejected_reason is None


async def test_skip_after_the_cancel_prompt_still_cancels(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> None:
    """The other half of the same dispatch."""
    request = BookingRequest(
        practice_id=practice.id,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        status=RequestStatus.confirmed,
        source_channel=Channel.web,
        scheduled_start=datetime.now(UTC) + timedelta(days=3),
        confirmed_at=datetime.now(UTC),
    )
    db.add(request)
    await db.flush()

    await handle(db, Update(chat_id=1, callback_data=f"{kb.CANCEL_REQUEST}:{request.id}"))
    await handle(db, Update(chat_id=1, callback_data=f"{kb.PANEL_SKIP}:{request.id}"))

    await db.refresh(request)
    assert request.status is RequestStatus.cancelled


# --- Joining two records that are one person (§13.1 step 1) ----------------


async def _web_client_with_token(db: AsyncSession) -> tuple[Client, str]:
    web = await resolve_client(db, Channel.email, "joined@example.test")
    raw = await issue_token(db, TokenPurpose.link_channel, client_id=web.id)
    return web, raw


async def test_the_link_asks_before_joining_a_chat_the_bot_already_knows(
    db: AsyncSession,
) -> None:
    """Reported in use: the bot opened and nothing happened.

    `_start` honoured the payload only when the chat had no client behind it,
    so anyone who pressed /start before booking by email watched the bot open
    on its ordinary menu with the two records still separate.
    """
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    web, raw = await _web_client_with_token(db)

    reply = await handle(db, Update(chat_id=CHAT, text=f"/start link_{raw}"))

    assert reply is not None and reply.keyboard is not None
    data = [b.callback_data for row in reply.keyboard.inline_keyboard for b in row]
    assert f"{kb.MERGE}:{raw}" in data
    assert kb.MERGE_NO in data

    # Asked, not done -- and the token is unspent, so the answer still works.
    assert (
        await db.execute(select(Client).where(Client.id == web.id))
    ).scalar_one_or_none() is not None
    assert await clients.token_target(db, raw, TokenPurpose.link_channel) is not None


async def test_confirming_joins_the_two_records(db: AsyncSession) -> None:
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    telegram_client = await _client(db)
    web, raw = await _web_client_with_token(db)

    await handle(db, Update(chat_id=CHAT, text=f"/start link_{raw}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MERGE}:{raw}"))

    # §5.1: the token's row survives, and this chat now reaches it.
    identity = (
        await db.execute(select(Identity).where(Identity.external_id == str(CHAT)))
    ).scalar_one()
    assert identity.client_id == web.id
    assert (
        await db.execute(select(Client).where(Client.id == telegram_client.id))
    ).scalar_one_or_none() is None

    # Spent now, and only now.
    assert await clients.token_target(db, raw, TokenPurpose.link_channel) is None


async def test_not_me_joins_nothing_and_keeps_the_link_alive(db: AsyncSession) -> None:
    """A shared phone or a forwarded email is enough for a wrong tap, and the
    person it was really sent to must still be able to follow it."""
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    telegram_client = await _client(db)
    _, raw = await _web_client_with_token(db)

    await handle(db, Update(chat_id=CHAT, text=f"/start link_{raw}"))
    reply = await handle(db, Update(chat_id=CHAT, callback_data=kb.MERGE_NO))

    assert reply is not None
    identity = (
        await db.execute(select(Identity).where(Identity.external_id == str(CHAT)))
    ).scalar_one()
    assert identity.client_id == telegram_client.id, "nothing moved"
    assert await clients.token_target(db, raw, TokenPurpose.link_channel) is not None


async def test_a_half_finished_booking_postpones_the_merge(
    db: AsyncSession, future_slot: Slot
) -> None:
    """The client is asked to finish rather than have it dropped underneath
    them, and the link survives so they can come back to it."""
    from app.core.services.translations import get_text

    await _walk_to_slot_pick(db, future_slot)  # leaves a live flow mid-booking
    telegram_client = await _client(db)
    _, raw = await _web_client_with_token(db)

    await handle(db, Update(chat_id=CHAT, text=f"/start link_{raw}"))
    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MERGE}:{raw}"))

    assert reply is not None
    assert reply.text == await get_text(db, "ru", "merge.busy")

    identity = (
        await db.execute(select(Identity).where(Identity.external_id == str(CHAT)))
    ).scalar_one()
    assert identity.client_id == telegram_client.id
    assert await clients.token_target(db, raw, TokenPurpose.link_channel) is not None


async def test_a_link_for_the_client_already_here_just_opens_the_menu(
    db: AsyncSession,
) -> None:
    """Following it twice is not an error -- they are already one person."""
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    client = await _client(db)
    raw = await issue_token(db, TokenPurpose.link_channel, client_id=client.id)

    reply = await handle(db, Update(chat_id=CHAT, text=f"/start link_{raw}"))

    # The main menu, which carries the persistent reply keyboard rather than an
    # inline one -- so no confirmation was offered.
    assert reply is not None
    assert not hasattr(reply.keyboard, "inline_keyboard")
    # Spent, because it did what it was for: there was nothing to join.
    assert await clients.token_target(db, raw, TokenPurpose.link_channel) is None


async def test_an_expired_link_says_so_rather_than_offering_a_merge(
    db: AsyncSession,
) -> None:
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))

    reply = await handle(db, Update(chat_id=CHAT, text="/start link_not-a-real-token"))

    assert reply is not None
    assert reply.keyboard is None


# --- How before when (§13.1) ------------------------------------------------


async def _only_the_slots_this_test_makes(db: AsyncSession) -> None:
    """Take every other slot off the picker for the length of this test.

    The suite shares a database with whatever else has run against it, so
    "nothing free" and "only these times free" cannot be assumed -- they have to
    be arranged. Rolled back with the rest of the test.
    """
    await db.execute(
        update(Slot)
        .where(Slot.status == SlotStatus.available)
        .values(status=SlotStatus.blocked)
    )
    await db.flush()


async def _to_the_modality_question(db: AsyncSession) -> Client:
    from app.core.services.translations import get_text

    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    consultation = await get_text(db, "ru", "menu.consultation")
    await handle(db, Update(chat_id=CHAT, text=consultation))
    return await _client(db)


async def test_the_first_question_is_how_not_when(
    db: AsyncSession, future_slot: Slot
) -> None:
    """Reported in use: the client picked a time, then found out whether it was
    ever an online time. The picker cannot filter by an answer it does not
    have."""
    client = await _to_the_modality_question(db)
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_modality

    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_session_type


async def test_an_on_site_client_is_never_asked_for_a_timezone(
    db: AsyncSession, practice: Practice, session_type_id: int, future_slot: Slot
) -> None:
    """They are coming to the room, so the room's clock is theirs."""
    client = await _to_the_modality_question(db)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:onsite"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))

    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_slot
    await db.refresh(client)
    assert client.timezone is None, "a stored zone is never asked for again"


async def test_an_online_client_is_asked_once_and_not_again(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    client = await _to_the_modality_question(db)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_timezone

    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.TZ}:Europe/Moscow"))
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_slot

    # Second booking: the zone is known, so the question does not come back.
    await handle(db, Update(chat_id=CHAT, text="/start"))
    client = await _to_the_modality_question(db)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_slot


async def test_the_picker_shows_only_times_that_match_both_answers(
    db: AsyncSession, practice: Practice, session_type_id: int
) -> None:
    """`slot_session_type` and `slot.modality` were both dead while the picker
    came first: it could not filter by answers it had not collected."""
    await _only_the_slots_this_test_makes(db)
    when = datetime.now(UTC) + timedelta(days=11)
    online = Slot(
        practice_id=practice.id,
        starts_at=when,
        duration_min=60,
        modality=Modality.online,
        status=SlotStatus.available,
    )
    onsite = Slot(
        practice_id=practice.id,
        starts_at=when + timedelta(hours=1),
        duration_min=60,
        modality=Modality.onsite,
        status=SlotStatus.available,
    )
    db.add_all([online, onsite])
    await db.flush()

    await _to_the_modality_question(db)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.TZ}:Europe/Moscow"))

    assert reply is not None and reply.keyboard is not None
    data = [b.callback_data for row in reply.keyboard.inline_keyboard for b in row]
    assert f"{kb.SLOT}:{online.id}" in data
    assert f"{kb.SLOT}:{onsite.id}" not in data, "the other modality is not quietly included"


async def test_nothing_online_offers_a_switch_rather_than_the_other_list(
    db: AsyncSession, practice: Practice, session_type_id: int
) -> None:
    """A client shown in-person times after asking for online has to work out
    what happened; a button saying "switch to in person" says it."""
    await _only_the_slots_this_test_makes(db)
    onsite = Slot(
        practice_id=practice.id,
        starts_at=datetime.now(UTC) + timedelta(days=12),
        duration_min=60,
        modality=Modality.onsite,
        status=SlotStatus.available,
    )
    db.add(onsite)
    await db.flush()

    await _to_the_modality_question(db)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.TZ}:Europe/Moscow"))

    assert reply is not None and reply.keyboard is not None
    data = [b.callback_data for row in reply.keyboard.inline_keyboard for b in row]
    assert f"{kb.MODE}:onsite" in data, "offered as a switch"
    assert kb.WAITLIST in data
    assert not any(str(d).startswith(f"{kb.SLOT}:") for d in data), "no times of the other kind"


async def test_the_switch_goes_straight_back_to_the_picker(
    db: AsyncSession, practice: Practice, session_type_id: int
) -> None:
    """The type is already chosen, so switching does not re-ask it."""
    await _only_the_slots_this_test_makes(db)
    onsite = Slot(
        practice_id=practice.id,
        starts_at=datetime.now(UTC) + timedelta(days=13),
        duration_min=60,
        modality=Modality.onsite,
        status=SlotStatus.available,
    )
    db.add(onsite)
    await db.flush()

    client = await _to_the_modality_question(db)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.TZ}:Europe/Moscow"))

    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:onsite"))

    assert await flow.current_step(db, client.id, Channel.telegram) is Step.choosing_slot
    assert reply is not None and reply.keyboard is not None
    data = [b.callback_data for row in reply.keyboard.inline_keyboard for b in row]
    assert f"{kb.SLOT}:{onsite.id}" in data


async def test_nothing_either_way_offers_the_waitlist(
    db: AsyncSession, session_type_id: int
) -> None:
    """No times of either kind is what the waitlist is for, and it is offered
    rather than left to be found."""
    await _only_the_slots_this_test_makes(db)
    await _to_the_modality_question(db)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.TZ}:Europe/Moscow"))

    assert reply is not None and reply.keyboard is not None
    data = [b.callback_data for row in reply.keyboard.inline_keyboard for b in row]
    assert data == [kb.WAITLIST]
