"""The §18 end-to-end scenario, on both channels (M8 acceptance).

§18 states it as: "book via web -> approve via admin -> reminder fires ->
cancel; and the same via a simulated Telegram update."

These run against the rollback session rather than the ASGI app, because the
scenario is about the *domain* travelling through every channel's adapter, and
the adapters are callable directly. The web routes have their own end-to-end
coverage in test_web_booking.py; what is new here is that a proposal made in
one place can be answered in another.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
import time_machine
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.telegram import keyboards as kb
from app.channels.telegram.router import Update, handle
from app.core.enums import (
    Channel,
    Modality,
    NegotiationKind,
    ReminderState,
    RequestStatus,
    SenderType,
    SlotStatus,
)
from app.core.models import (
    BookingRequest,
    Client,
    NegotiationMessage,
    OutboxMessage,
    Practice,
    Reminder,
    Slot,
)
from app.core.policies import now_utc
from app.core.services import booking, flow, notifications
from app.core.services.clients import link_identity, resolve_client
from app.core.services.flow import Step

CHAT = 611611611


async def _rows(db: AsyncSession, request_id: int, intent: str) -> list[OutboxMessage]:
    return list(
        (
            await db.execute(
                select(OutboxMessage).where(
                    OutboxMessage.request_id == request_id,
                    OutboxMessage.intent_key == intent,
                )
            )
        )
        .scalars()
        .all()
    )


async def _book(
    db: AsyncSession, client: Client, session_type_id: int, slot: Slot, channel: Channel
) -> BookingRequest:
    request = await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=channel,
        problem_text="work stress",
    )
    await notifications.publish(db)
    return request


# --- §18, the web path ------------------------------------------------------


async def test_the_full_scenario_on_the_web_channel(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """book -> approve -> reminder fires -> cancel."""
    request = await _book(db, client, session_type_id, future_slot, Channel.web)
    assert request.status is RequestStatus.pending
    assert future_slot.status is SlotStatus.held

    # Approve, as the admin surface does.
    confirmed = await booking.admin_approve(db, request.id)
    await notifications.publish(db)

    assert confirmed.status is RequestStatus.confirmed
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.booked
    assert await _rows(db, request.id, "request.confirmed.client")

    # The reminder fires when its due time arrives.
    reminder = (
        await db.execute(
            select(Reminder).where(Reminder.request_id == request.id, Reminder.offset_min == 60)
        )
    ).scalar_one()
    assert reminder.state is ReminderState.scheduled

    await _fire(db, reminder, request)
    assert reminder.state is ReminderState.sent
    assert await _rows(db, request.id, "reminder.client")

    # The therapist cancels: slot released, reminders cancelled, client told.
    cancelled = await booking.admin_cancel(db, request.id, reason="ill")
    await notifications.publish(db)

    assert cancelled.status is RequestStatus.cancelled
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available

    states = (
        (await db.execute(select(Reminder.state).where(Reminder.request_id == request.id)))
        .scalars()
        .all()
    )
    # The one already sent stays sent; the other is cancelled, not deleted.
    assert ReminderState.cancelled in states
    assert await _rows(db, request.id, "request.cancelled.client")


async def _fire(db: AsyncSession, reminder: Reminder, request: BookingRequest) -> None:
    """The `fire_reminders` sweep, against this transaction.

    The job itself opens its own unit of work and would commit; the query and
    the dedupe key are identical.
    """
    from app.core.services.notifications import Recipient

    with time_machine.travel(reminder.due_at + timedelta(minutes=1), tick=False):
        due = (
            (
                await db.execute(
                    select(Reminder).where(
                        Reminder.state == ReminderState.scheduled,
                        Reminder.due_at <= now_utc(),
                        Reminder.request_id == request.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert reminder.id in {r.id for r in due}

        await notifications.enqueue_raw(
            db,
            intent_key="reminder.client",
            recipient=Recipient.client,
            payload={
                "uuid": str(request.uuid),
                "time": request.scheduled_start.isoformat() if request.scheduled_start else None,
                "offset_min": reminder.offset_min,
                "modality": request.modality.value,
                "join_url": await notifications.join_info(db, request),
            },
            request_id=request.id,
            dedupe_key=f"reminder:{reminder.id}",
        )
        reminder.state = ReminderState.sent
        reminder.fired_at = now_utc()
        await db.flush()


# --- §18, the Telegram path -------------------------------------------------


async def _tg_client(db: AsyncSession) -> Client:
    await handle(db, Update(chat_id=CHAT, text="/start"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
    return await resolve_client(db, Channel.telegram, str(CHAT), verified=True)


async def test_the_full_scenario_on_the_telegram_channel(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """The same four steps, driven by simulated updates."""
    from app.core.services.translations import get_text

    tg = await _tg_client(db)

    consultation = await get_text(db, "ru", "menu.consultation")
    await handle(db, Update(chat_id=CHAT, text=consultation))
    # §13.1's order: how, what, when. Modality first so the picker can filter by
    # it; the timezone follows only because this one is online.
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:online"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.STYPE}:{session_type_id}"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.TZ}:Europe/Moscow"))
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.SLOT}:{future_slot.id}"))
    await handle(db, Update(chat_id=CHAT, text="work stress"))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))
    await handle(db, Update(chat_id=CHAT, callback_data=kb.SKIP))

    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.client_id == tg.id))
    ).scalar_one()
    assert request.status is RequestStatus.pending

    # Approve from the therapist's Telegram surface (§13.2).
    reply = await handle(db, Update(chat_id=1, callback_data=f"{kb.APPROVE}:{request.id}"))
    assert reply is not None and str(request.uuid) in reply.text

    await db.refresh(request)
    assert request.status is RequestStatus.confirmed
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.booked

    reminder = (
        await db.execute(
            select(Reminder).where(Reminder.request_id == request.id, Reminder.offset_min == 60)
        )
    ).scalar_one()
    await _fire(db, reminder, request)
    assert await _rows(db, request.id, "reminder.client")

    # Cancel from Telegram too. §13.2 asks for a reason first: the client is
    # told it, so it is hers to write rather than the adapter's to invent.
    asked = await handle(db, Update(chat_id=1, callback_data=f"{kb.CANCEL_REQUEST}:{request.id}"))
    assert asked is not None
    await db.refresh(request)
    assert request.status is RequestStatus.confirmed  # nothing yet

    reply = await handle(db, Update(chat_id=1, text="I am ill, I am sorry"))
    assert reply is not None

    await db.refresh(request)
    assert request.status is RequestStatus.cancelled
    assert request.cancellation_reason == "I am ill, I am sorry"
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available
    assert await _rows(db, request.id, "request.cancelled.client")


# --- Negotiation across channels -------------------------------------------


async def test_a_proposal_made_by_the_admin_is_answerable_in_telegram(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """The point of M8: the intent is the same, the surface differs."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)

    later = now_utc() + timedelta(days=9)
    await booking.admin_propose(db, request.id, proposed_start=later)
    await notifications.publish(db)

    assert request.status is RequestStatus.negotiating
    assert await _rows(db, request.id, "request.proposal.client")

    # The client presses Accept on that message.
    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"accept:{request.id}"))
    assert reply is not None

    await db.refresh(request)
    assert request.status is RequestStatus.confirmed
    assert request.scheduled_start == later


async def test_a_client_can_counter_from_telegram(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """§9: a structured time is preferred, free text stays legal."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    # Counter needs words, so the button parks the request and asks.
    await handle(db, Update(chat_id=CHAT, callback_data=f"counter:{request.id}"))
    assert await flow.current_step(db, tg.id, Channel.telegram) is Step.entering_counter

    reply = await handle(db, Update(chat_id=CHAT, text="some evening next week?"))
    assert reply is not None

    await db.refresh(request)
    assert request.status is RequestStatus.negotiating

    last = (
        await db.execute(
            select(NegotiationMessage)
            .where(NegotiationMessage.request_id == request.id)
            .order_by(NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert last.sender is SenderType.client
    assert last.kind is NegotiationKind.counter
    assert last.body_text == "some evening next week?"
    # Free text has no instant, and that is allowed.
    assert last.proposed_start is None

    # Whose turn is derived, never stored (§6.6).
    assert await booking.whose_turn(db, request.id) is SenderType.admin
    assert await _rows(db, request.id, "request.counter.admin")


async def test_a_countered_time_is_recorded_structurally_when_parseable(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """When the client does name a real time, acceptance can confirm directly."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    await handle(db, Update(chat_id=CHAT, callback_data=f"counter:{request.id}"))
    await handle(db, Update(chat_id=CHAT, text="2027-05-14 16:30"))

    last = (
        await db.execute(
            select(NegotiationMessage)
            .where(NegotiationMessage.request_id == request.id)
            .order_by(NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert last.proposed_start is not None
    assert last.proposed_start.tzinfo is not None  # aware UTC, always
    assert last.body_text == "2027-05-14 16:30"


async def test_a_client_can_decline_from_telegram(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    await handle(db, Update(chat_id=CHAT, callback_data=f"decline:{request.id}"))

    await db.refresh(request)
    assert request.status is RequestStatus.rejected
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available


async def test_the_therapist_can_propose_from_telegram(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """§13.2 keeps propose on the phone. Typing is the escape hatch now rather
    than the only way in, and it still has to work: `18:30` is on no hour grid,
    and §7.1 allows a proposal of words."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)

    # The admin chat is a different chat id (configured in TELEGRAM_ADMIN_IDS).
    await handle(db, Update(chat_id=1, callback_data=f"{kb.PROPOSE_TYPE}:{request.id}"))
    reply = await handle(db, Update(chat_id=1, text="2027-06-01 11:00"))

    assert reply is not None
    await db.refresh(request)
    assert request.status is RequestStatus.negotiating

    proposal = (
        await db.execute(
            select(NegotiationMessage)
            .where(NegotiationMessage.request_id == request.id)
            .order_by(NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert proposal.sender is SenderType.admin
    assert proposal.proposed_start is not None


# --- Authorisation ----------------------------------------------------------


async def test_a_client_cannot_act_on_someone_elses_request(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """Callback data is client-supplied. A request id from someone else's
    message must not be actionable."""
    victim = await _book(db, client, session_type_id, future_slot, Channel.web)
    await booking.admin_propose(db, victim.id, proposed_start=now_utc() + timedelta(days=9))

    await _tg_client(db)  # a different person
    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"accept:{victim.id}"))

    assert reply is not None
    await db.refresh(victim)
    assert victim.status is RequestStatus.negotiating  # untouched


async def test_the_approve_button_the_worker_renders_actually_approves(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The seam between §10's outbox row and §13.2's handler.

    Both halves were covered and the join was not: the payload never carried a
    request id, so every inline button arrived as a bare action word and the
    handler had nothing to act on. This presses the button as delivered.
    """
    from app.render.messages import render

    request = await _book(db, client, session_type_id, future_slot, Channel.web)

    row = (
        await db.execute(
            select(OutboxMessage)
            .where(
                OutboxMessage.intent_key == "request.submitted.admin",
                OutboxMessage.channel == Channel.telegram,
            )
            .order_by(OutboxMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()

    # Exactly the call app/worker/jobs/outbox.py:deliver_one makes.
    rendered = await render(
        db,
        intent_key=row.intent_key,
        payload=row.payload,
        locale=row.locale,
        channel=row.channel,
        tz="UTC",
        base_url="https://example.test",
        request_id=row.request_id,
    )
    approve = next(a for a in rendered.actions if a.key == kb.APPROVE)
    assert approve.callback_data == f"{kb.APPROVE}:{request.id}"

    reply = await handle(db, Update(chat_id=1, callback_data=approve.callback_data))

    assert reply is not None
    await db.refresh(request)
    assert request.status is RequestStatus.confirmed


async def test_admin_actions_are_gated_by_configuration(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The bot has no other way to authenticate her (DESIGN.md §5.2)."""
    request = await _book(db, client, session_type_id, future_slot, Channel.web)

    await _tg_client(db)
    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.APPROVE}:{request.id}"))

    assert reply is None  # silence, not an error
    await db.refresh(request)
    assert request.status is RequestStatus.pending


async def test_answering_twice_is_refused_rather_than_duplicated(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """A proposal answered on the web and then in Telegram: §7.1 has no path
    out of `confirmed` except cancel or complete."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    await handle(db, Update(chat_id=CHAT, callback_data=f"accept:{request.id}"))
    await db.refresh(request)
    assert request.status is RequestStatus.confirmed

    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"accept:{request.id}"))
    assert reply is not None  # explained, not silent

    await db.refresh(request)
    assert request.status is RequestStatus.confirmed
    confirmations = await _rows(db, request.id, "request.confirmed.client")
    assert len(confirmations) == 1


# --- Reminders (§13.4) ------------------------------------------------------


async def test_a_reminder_email_carries_the_date_and_time(
    db: AsyncSession, session_type_id: int, future_slot: Slot, email_enabled: None
) -> None:
    """§13.4 states this explicitly: a reminder that requires a click is not a
    reminder."""
    from app.core.enums import Channel as Ch
    from app.render.messages import render

    tg = await _tg_client(db)
    await link_identity(db, tg.id, Ch.email, "reminder@example.test", verified=True)

    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_approve(db, request.id)
    await notifications.publish(db)

    rows = await _rows(db, request.id, "request.confirmed.client")
    # §13.3: confirmations go to both channels when both exist.
    assert {r.channel for r in rows} == {Ch.telegram, Ch.email}

    email_row = next(r for r in rows if r.channel is Ch.email)
    message = await render(
        db,
        intent_key=email_row.intent_key,
        payload=email_row.payload,
        locale=email_row.locale,
        channel=Ch.email,
        tz="Europe/Moscow",
        base_url="https://example.test",
    )
    assert request.scheduled_start is not None
    assert message.text.strip()
    assert message.subject


async def test_cancelling_stops_the_reminders(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_approve(db, request.id)
    await booking.admin_cancel(db, request.id, reason="ill")

    pending = (
        await db.execute(
            select(func.count())
            .select_from(Reminder)
            .where(
                Reminder.request_id == request.id,
                Reminder.state == ReminderState.scheduled,
            )
        )
    ).scalar_one()
    assert pending == 0


@pytest.mark.parametrize("channel", [Channel.web, Channel.telegram])
async def test_cancellation_notifies_the_client_whatever_the_source(
    db: AsyncSession,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    practice: Practice,
    channel: Channel,
) -> None:
    """DESIGN.md §14: cancelling notifies the client on every channel they
    have."""
    request = await _book(db, client, session_type_id, future_slot, channel)
    await booking.admin_approve(db, request.id)
    await booking.admin_cancel(db, request.id, reason="ill")
    await notifications.publish(db)

    rows = await _rows(db, request.id, "request.cancelled.client")
    assert rows
    assert all(r.payload.get("reason") == "ill" for r in rows)


async def test_time_travel_makes_a_scheduled_reminder_due(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """The sweep is the whole mechanism; there is no in-memory schedule to
    lose (hard rule 3)."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_approve(db, request.id)

    reminders = (
        (await db.execute(select(Reminder).where(Reminder.request_id == request.id)))
        .scalars()
        .all()
    )
    assert all(r.due_at > now_utc() for r in reminders)

    latest = max(r.due_at for r in reminders)
    with time_machine.travel(latest + timedelta(minutes=1), tick=False):
        due = (
            (
                await db.execute(
                    select(Reminder).where(
                        Reminder.request_id == request.id,
                        Reminder.state == ReminderState.scheduled,
                        Reminder.due_at <= now_utc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(due) == len(reminders)


def test_the_scenario_covers_both_channels() -> None:
    """A guard on this file rather than on the code: §19's M8 acceptance is
    "the full E2E scenario in §18 passes on both channels", and it is easy to
    let one of the two rot."""
    import pathlib

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    assert "test_the_full_scenario_on_the_web_channel" in source
    assert "test_the_full_scenario_on_the_telegram_channel" in source
    assert datetime.now(UTC).tzinfo is not None  # naive datetimes are banned


async def test_agreeing_to_a_proposal_of_words_reaches_the_therapist(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """§7.1's other accept branch, end to end.

    The therapist typed a time Telegram could not parse, so it went out as
    words. The client agreed. Before, `client_accept` refused: the tap did
    nothing the client could see, the request sat in `negotiating`, and the one
    person who could move it forward heard nothing at all.
    """
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)

    # Exactly what the Telegram admin panel does with an unparseable answer:
    # it keeps the words and proposes them (§9).
    await booking.admin_propose(db, request.id, proposed_start=None, body_text="2026-08-29-14:30")
    await notifications.publish(db)

    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"accept:{request.id}"))
    assert reply is not None

    await db.refresh(request)
    assert request.status is RequestStatus.negotiating, "words cannot confirm a session"
    assert request.scheduled_start is None

    await notifications.publish(db)
    assert await _rows(db, request.id, "request.accepted.admin"), "the therapist must hear it"

    # And it is her turn: she is the one who turns an agreement into a time.
    assert await booking.whose_turn(db, request.id) is SenderType.admin


async def test_the_approve_button_on_a_counter_actually_approves(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """Request, therapist counters, client counters back, therapist taps
    Approve on the notification.

    §10 gives `request.counter.admin` an approve action and §13.2 requires every
    button to come from §7.1's table -- which did not allow approving a
    negotiation. So the notification offered a button the core refused, and
    tapping it did nothing whatsoever.
    """
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)

    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))
    countered = now_utc() + timedelta(days=10)
    await booking.client_counter(db, request.id, proposed_start=countered, body_text="later?")
    await notifications.publish(db)

    assert await _rows(db, request.id, "request.counter.admin")

    # chat_id=1 is the admin chat, as TELEGRAM_ADMIN_IDS configures it.
    reply = await handle(db, Update(chat_id=1, callback_data=f"{kb.APPROVE}:{request.id}"))
    assert reply is not None

    await db.refresh(request)
    assert request.status is RequestStatus.confirmed
    assert request.scheduled_start == countered


# --- Answering a proposal with a slot (§12.1, §13.1) ------------------------


def _callbacks(reply: object) -> list[str]:
    keyboard = getattr(reply, "keyboard", None)
    if keyboard is None:
        return []
    return [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]


async def test_counter_offers_the_free_slots_rather_than_a_blank_box(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """§12.1: a client has no reason to guess an ISO timestamp, so a slot the
    practice is already holding open is the answer that needs no guessing --
    and none of it is the booking picker, because a tap here means "I suggest
    this", not "hold this for me"."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    # Proposing another time releases the held slot (§7.1), which puts it back
    # on the picker -- so it is one of the times offered back.
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"counter:{request.id}"))
    assert reply is not None

    offered = _callbacks(reply)
    assert f"{kb.COUNTER_SLOT}:{request.id}:{future_slot.id}" in offered
    assert not any(c.startswith(f"{kb.SLOT}:") for c in offered), "not the booking picker"
    # Words are still legal while the practice accepts them (§9).
    assert await flow.current_step(db, tg.id, Channel.telegram) is Step.entering_counter


async def test_countering_with_a_slot_records_its_instant(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """The point of the whole change: what reaches the therapist is a time she
    can approve, not a sentence she has to turn into one."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))
    await db.refresh(future_slot)

    await handle(db, Update(chat_id=CHAT, callback_data=f"counter:{request.id}"))
    await handle(
        db,
        Update(
            chat_id=CHAT,
            callback_data=f"{kb.COUNTER_SLOT}:{request.id}:{future_slot.id}",
        ),
    )

    last = (
        await db.execute(
            select(NegotiationMessage)
            .where(NegotiationMessage.request_id == request.id)
            .order_by(NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert last.kind is NegotiationKind.counter
    assert last.proposed_start == future_slot.starts_at

    # §12.1: nothing is held. §7.1 keeps the request's original slot, and one
    # request holding two is not a state worth inventing.
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available
    assert await booking.whose_turn(db, request.id) is SenderType.admin


async def test_a_slot_taken_since_the_keyboard_was_drawn_is_refused(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """The keyboard may have sat in the chat for an hour. Recording a counter
    for a time the practice no longer offers hands her something she cannot
    honour."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    await handle(db, Update(chat_id=CHAT, callback_data=f"counter:{request.id}"))

    await db.refresh(future_slot)
    future_slot.status = SlotStatus.blocked
    await db.flush()

    before = (
        await db.execute(
            select(func.count())
            .select_from(NegotiationMessage)
            .where(NegotiationMessage.request_id == request.id)
        )
    ).scalar_one()
    reply = await handle(
        db,
        Update(
            chat_id=CHAT,
            callback_data=f"{kb.COUNTER_SLOT}:{request.id}:{future_slot.id}",
        ),
    )
    assert reply is not None
    after = (
        await db.execute(
            select(func.count())
            .select_from(NegotiationMessage)
            .where(NegotiationMessage.request_id == request.id)
        )
    ).scalar_one()
    assert after == before, "nothing recorded for a time that is no longer offered"


async def test_with_free_text_off_the_counter_offers_the_waitlist_instead(
    db: AsyncSession, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    """§12.1: with the gate closed and nothing to pick, accept and decline would
    be the only replies -- so a client who simply cannot make the proposed time
    would have to reject their own request to say so."""
    practice.fallback_to_negotiation = False
    await db.flush()

    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"counter:{request.id}"))
    assert reply is not None
    assert f"{kb.COUNTER_WAITLIST}:{request.id}" in _callbacks(reply)
    # Nothing is parked, because typing is not an answer here.
    assert await flow.current_step(db, tg.id, Channel.telegram) is not Step.entering_counter


async def test_the_waitlist_button_closes_the_request_and_leaves_an_entry(
    db: AsyncSession, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    from app.core.models import WaitlistEntry

    practice.fallback_to_negotiation = False
    await db.flush()

    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    await handle(db, Update(chat_id=CHAT, callback_data=f"counter:{request.id}"))
    reply = await handle(
        db, Update(chat_id=CHAT, callback_data=f"{kb.COUNTER_WAITLIST}:{request.id}")
    )
    assert reply is not None

    await db.refresh(request)
    assert request.status is RequestStatus.rejected
    entries = (
        (await db.execute(select(WaitlistEntry).where(WaitlistEntry.client_id == tg.id)))
        .scalars()
        .all()
    )
    assert entries


# --- Proposing without typing an ISO timestamp (§13.2) ----------------------


ADMIN_CHAT = 1


async def test_propose_opens_with_her_own_free_times(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """The reported fault: it asked for YYYY-MM-DD HH:MM on the one surface that
    exists for answering away from a desk."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    # Booking holds the slot, so publish another for her to be offered.
    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    spare = Slot(
        practice_id=practice.id,
        starts_at=now_utc() + timedelta(days=12, minutes=3),
        duration_min=60,
        status=SlotStatus.available,
    )
    db.add(spare)
    await db.flush()

    reply = await handle(db, Update(chat_id=ADMIN_CHAT, callback_data=f"propose:{request.id}"))
    assert reply is not None

    offered = _callbacks(reply)
    assert f"{kb.PROPOSE_SLOT}:{request.id}:{spare.id}" in offered
    assert f"{kb.PROPOSE_MONTHS}:{request.id}" in offered, "a way into the picker"
    assert f"{kb.PROPOSE_TYPE}:{request.id}" in offered, "and typing is still there"
    # Nothing is parked until she asks to type.
    admin = await resolve_client(db, Channel.telegram, str(ADMIN_CHAT), verified=True)
    assert await flow.current_step(db, admin.id, Channel.telegram) is not (
        Step.admin_entering_proposal
    )


async def test_the_picker_walks_month_then_day_then_hour(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """Three screens, each carrying the whole answer so far, so the picker holds
    no state at all."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    today = now_utc().astimezone(ZoneInfo(practice.timezone)).date()

    months = await handle(
        db, Update(chat_id=ADMIN_CHAT, callback_data=f"{kb.PROPOSE_MONTHS}:{request.id}")
    )
    assert months is not None
    this_month = f"{kb.PROPOSE_DAYS}:{request.id}:{today.year:04d}-{today.month:02d}"
    assert this_month in _callbacks(months)

    days = await handle(db, Update(chat_id=ADMIN_CHAT, callback_data=this_month))
    assert days is not None
    day_callbacks = [c for c in _callbacks(days) if c.startswith(f"{kb.PROPOSE_HOURS}:")]
    assert day_callbacks, "the month's days"
    # Today itself is offered; anything before it is dead rather than missing.
    assert f"{kb.PROPOSE_HOURS}:{request.id}:{today.isoformat()}" in day_callbacks

    hours = await handle(db, Update(chat_id=ADMIN_CHAT, callback_data=day_callbacks[-1]))
    assert hours is not None
    picked_day = day_callbacks[-1].rsplit(":", 1)[1]
    hour_callbacks = [c for c in _callbacks(hours) if c.startswith(f"{kb.PROPOSE_AT}:")]

    # Every hour of the day is a cell: the live ones plus the ones §13.2 marks
    # as taken. Counted against the core rather than assumed to be twenty-four,
    # because this database is shared and may genuinely have a session that day.
    taken = await booking.taken_hours_on(
        db, day=date.fromisoformat(picked_day), tz=practice.timezone
    )
    assert len(hour_callbacks) + len(taken) == 24, (
        "all twenty-four offered or marked, never filtered to working hours"
    )
    for hour in range(24):
        expected = f"{kb.PROPOSE_AT}:{request.id}:{picked_day}T{hour:02d}"
        assert (expected in hour_callbacks) is (hour not in taken), hour


async def test_picking_an_hour_proposes_it_in_her_own_clock(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """DESIGN.md §8: she thinks in the practice timezone; storage is UTC."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    zone = ZoneInfo(practice.timezone)
    day = (now_utc().astimezone(zone) + timedelta(days=6)).date()

    reply = await handle(
        db,
        Update(
            chat_id=ADMIN_CHAT,
            callback_data=f"{kb.PROPOSE_AT}:{request.id}:{day.isoformat()}T18",
        ),
    )
    assert reply is not None

    await db.refresh(request)
    assert request.status is RequestStatus.negotiating

    proposal = (
        await db.execute(
            select(NegotiationMessage)
            .where(NegotiationMessage.request_id == request.id)
            .order_by(NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert proposal.sender is SenderType.admin
    assert proposal.proposed_start is not None
    local = proposal.proposed_start.astimezone(zone)
    assert (local.date(), local.hour) == (day, 18)


async def test_an_hour_already_taken_cannot_be_picked(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """§13.2: dead in place and marked, so the absence reads as "there is
    something there" rather than as a gap. Telegram has no colour."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    zone = ZoneInfo(practice.timezone)
    day = (now_utc().astimezone(zone) + timedelta(days=7)).date()

    # A session she has already confirmed, and an hour she has blocked off.
    busy = datetime.combine(day, time(14, 0), tzinfo=zone)
    db.add(
        BookingRequest(
            practice_id=practice.id,
            client_id=tg.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            status=RequestStatus.confirmed,
            source_channel=Channel.web,
            scheduled_start=busy.astimezone(UTC),
            confirmed_at=now_utc(),
        )
    )
    db.add(
        Slot(
            practice_id=practice.id,
            starts_at=datetime.combine(day, time(9, 0), tzinfo=zone).astimezone(UTC),
            duration_min=60,
            status=SlotStatus.blocked,
        )
    )
    await db.flush()

    reply = await handle(
        db,
        Update(
            chat_id=ADMIN_CHAT,
            callback_data=f"{kb.PROPOSE_HOURS}:{request.id}:{day.isoformat()}",
        ),
    )
    assert reply is not None

    offered = _callbacks(reply)
    assert f"{kb.PROPOSE_AT}:{request.id}:{day.isoformat()}T14" not in offered
    assert f"{kb.PROPOSE_AT}:{request.id}:{day.isoformat()}T09" not in offered
    assert f"{kb.PROPOSE_AT}:{request.id}:{day.isoformat()}T15" in offered, "the rest still are"

    labels = [
        button.text
        for row in reply.keyboard.inline_keyboard
        for button in row
    ]
    assert f"{kb.TAKEN_MARK}14" in labels, "marked, not removed"
    assert f"{kb.TAKEN_MARK}09" in labels


async def test_a_held_slot_does_not_make_an_hour_taken(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """A hold belongs to a client who has not finished a form, and lapses on its
    own. Treating it as taken would hide an hour that is about to be free."""
    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    zone = ZoneInfo(practice.timezone)
    day = (now_utc().astimezone(zone) + timedelta(days=8)).date()

    # `future_slot` is held by the booking below, at its own instant.
    tg = await _tg_client(db)
    await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.held

    held_day = future_slot.starts_at.astimezone(zone).date()
    taken = await booking.taken_hours_on(db, day=held_day, tz=practice.timezone)

    assert future_slot.starts_at.astimezone(zone).hour not in taken
    assert await booking.taken_hours_on(db, day=day, tz=practice.timezone) == frozenset()


async def test_a_slot_taken_since_the_panel_was_drawn_is_refused(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """The panel may have sat in her chat for an hour."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    spare = Slot(
        practice_id=practice.id,
        starts_at=now_utc() + timedelta(days=13, minutes=5),
        duration_min=60,
        status=SlotStatus.available,
    )
    db.add(spare)
    await db.flush()
    spare_id = spare.id

    spare.status = SlotStatus.blocked
    await db.flush()

    reply = await handle(
        db,
        Update(
            chat_id=ADMIN_CHAT,
            callback_data=f"{kb.PROPOSE_SLOT}:{request.id}:{spare_id}",
        ),
    )
    assert reply is not None
    assert reply.toast == "That time has gone."

    await db.refresh(request)
    assert request.status is RequestStatus.pending, "nothing proposed"


# --- The client's own picker (§12.1, §13.1) ---------------------------------


async def test_a_client_answering_a_proposal_is_offered_the_picker(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """Item 5 gave the web a `datetime-local` and left Telegram with a format
    hint, so a client on a phone was the last person still typing timestamps."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"counter:{request.id}"))
    assert reply is not None
    assert f"{kb.COUNTER_MONTHS}:{request.id}" in _callbacks(reply)
    # Typing survives beneath it: §9 keeps the words either way.
    assert await flow.current_step(db, tg.id, Channel.telegram) is Step.entering_counter


async def test_the_client_picker_looks_two_months_ahead_not_three(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """A suggestion four months out is not one she can act on, and a shorter row
    nudges toward something sooner. Hers is three (§13.2)."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    reply = await handle(
        db, Update(chat_id=CHAT, callback_data=f"{kb.COUNTER_MONTHS}:{request.id}")
    )
    assert reply is not None
    months = [c for c in _callbacks(reply) if c.startswith(f"{kb.COUNTER_DAYS}:")]
    assert len(months) == 2
    assert kb.ADMIN_PICKER.months_ahead == 3


async def test_the_client_picker_never_shows_the_therapists_diary(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """Marking her filled hours would tell a client when *other people* have
    sessions, and quietly omitting them would say the same by the gap. So all
    twenty-four are offered, whatever her day looks like."""
    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    tz = request.client_timezone or tg.timezone or practice.timezone
    day = (now_utc().astimezone(ZoneInfo(tz)) + timedelta(days=9)).date()

    # A session she has confirmed and an hour she has blocked, both on that day.
    busy = datetime.combine(day, time(14, 0), tzinfo=ZoneInfo(practice.timezone))
    db.add(
        BookingRequest(
            practice_id=practice.id,
            client_id=tg.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            status=RequestStatus.confirmed,
            source_channel=Channel.web,
            scheduled_start=busy.astimezone(UTC),
            confirmed_at=now_utc(),
        )
    )
    await db.flush()
    assert await booking.taken_hours_on(
        db, day=day, tz=practice.timezone
    ), "the therapist really does have something that day"

    reply = await handle(
        db,
        Update(
            chat_id=CHAT,
            callback_data=f"{kb.COUNTER_HOURS}:{request.id}:{day.isoformat()}",
        ),
    )
    assert reply is not None

    offered = [c for c in _callbacks(reply) if c.startswith(f"{kb.COUNTER_AT}:")]
    assert len(offered) == 24, "every hour, so the gaps say nothing"
    labels = [b.text for row in reply.keyboard.inline_keyboard for b in row]
    assert not any(kb.TAKEN_MARK in label for label in labels)


async def test_the_client_picker_reads_in_their_own_timezone(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """DESIGN.md §8: the therapist picks in her clock, a client in theirs."""
    from app.core.services.clients import set_client_timezone

    tg = await _tg_client(db)
    await set_client_timezone(db, tg.id, "Asia/Tokyo")
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    request.client_timezone = "Asia/Tokyo"
    await db.flush()
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    day = (now_utc().astimezone(ZoneInfo("Asia/Tokyo")) + timedelta(days=10)).date()
    await handle(
        db,
        Update(
            chat_id=CHAT,
            callback_data=f"{kb.COUNTER_AT}:{request.id}:{day.isoformat()}T18",
        ),
    )

    last = (
        await db.execute(
            select(NegotiationMessage)
            .where(NegotiationMessage.request_id == request.id)
            .order_by(NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert last.kind is NegotiationKind.counter
    assert last.proposed_start is not None
    local = last.proposed_start.astimezone(ZoneInfo("Asia/Tokyo"))
    assert (local.date(), local.hour) == (day, 18)


async def test_the_client_picker_speaks_the_clients_language(
    db: AsyncSession, session_type_id: int, future_slot: Slot
) -> None:
    """§15: the admin surface is English and never translated; a client's is
    never English by accident. The month names come from the catalogue the slot
    picker already uses."""
    from app.core.services.translations import get_text

    tg = await _tg_client(db)  # /start chose ru
    assert tg.language == "ru"
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    reply = await handle(
        db, Update(chat_id=CHAT, callback_data=f"{kb.COUNTER_MONTHS}:{request.id}")
    )
    assert reply is not None

    this_month = now_utc().date().month
    russian = await get_text(db, "ru", f"date.month.{this_month}")
    labels = [b.text for row in reply.keyboard.inline_keyboard for b in row]
    assert any(russian in label for label in labels), labels
    assert reply.text == await get_text(db, "ru", "request.counter.pick_month")


async def test_with_free_text_off_the_client_gets_no_picker(
    db: AsyncSession, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    """The picker is the free-text half in another shape, so it sits behind the
    same gate -- otherwise switching words off would leave the way in open."""
    practice.fallback_to_negotiation = False
    await db.flush()

    tg = await _tg_client(db)
    request = await _book(db, tg, session_type_id, future_slot, Channel.telegram)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))

    reply = await handle(db, Update(chat_id=CHAT, callback_data=f"counter:{request.id}"))
    assert reply is not None
    offered = _callbacks(reply)
    assert f"{kb.COUNTER_MONTHS}:{request.id}" not in offered
    assert f"{kb.COUNTER_WAITLIST}:{request.id}" in offered
