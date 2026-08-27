"""Booking request state machine (IMPLEMENTATION.md §7.1).

M2 acceptance requires every transition **and every rejection** in §7 to be
covered. The rejection matrix at the bottom is the part that actually protects
the invariant: it asserts that each of the 11 events is refused from every
status the table does not permit it from.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import time_machine
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    ActorType,
    Channel,
    Modality,
    NegotiationKind,
    ReminderState,
    RequestStatus,
    SenderType,
    SlotStatus,
)
from app.core.errors import BookingClosed, InvalidTransition, NegotiationDisabled
from app.core.events import (
    RequestAccepted,
    RequestConfirmed,
    RequestCounter,
    RequestSubmitted,
    drain,
    pending,
)
from app.core.models import (
    AuditLog,
    BookingRequest,
    Client,
    NegotiationMessage,
    Practice,
    Reminder,
    Slot,
)
from app.core.services import booking
from app.core.services import slots as slot_service

LATER = datetime.now(UTC) + timedelta(days=10)


async def _submit(
    db: AsyncSession, client: Client, session_type_id: int, slot: Slot
) -> BookingRequest:
    return await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        problem_text="not logged, not emailed",
    )


async def _negotiating(
    db: AsyncSession,
    client: Client,
    session_type_id: int,
    slot: Slot,
    *,
    proposed: datetime | None = LATER,
) -> BookingRequest:
    request = await _submit(db, client, session_type_id, slot)
    return await booking.admin_propose(db, request.id, proposed_start=proposed)


async def _confirmed(
    db: AsyncSession, client: Client, session_type_id: int, slot: Slot
) -> BookingRequest:
    request = await _submit(db, client, session_type_id, slot)
    return await booking.admin_approve(db, request.id)


# --- submit -----------------------------------------------------------------


async def test_submit_creates_a_pending_request_and_holds_the_slot(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _submit(db, client, session_type_id, future_slot)

    assert request.status is RequestStatus.pending
    assert request.expires_at is not None
    assert future_slot.status is SlotStatus.held
    assert future_slot.held_by_request == request.id


async def test_submit_emits_request_submitted(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    await _submit(db, client, session_type_id, future_slot)
    assert any(isinstance(e, RequestSubmitted) for e in pending(db))


async def test_submit_is_refused_when_availability_is_off(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    practice.availability_on = False
    await db.flush()

    with pytest.raises(BookingClosed):
        await _submit(db, client, session_type_id, future_slot)


async def test_free_time_submit_is_refused_when_negotiation_is_off(
    db: AsyncSession, client: Client, session_type_id: int, practice: Practice
) -> None:
    practice.negotiation_enabled = False
    await db.flush()

    with pytest.raises(NegotiationDisabled):
        await booking.submit_free_time_request(
            db,
            client_id=client.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            desired_time_text="some evening next week?",
            source_channel=Channel.telegram,
        )


async def test_auto_confirm_skips_the_pending_state(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    """One line of policy, off by default (DESIGN.md §6)."""
    practice.auto_confirm_slots = True
    await db.flush()

    request = await _submit(db, client, session_type_id, future_slot)
    assert request.status is RequestStatus.confirmed
    assert future_slot.status is SlotStatus.booked


# --- pending transitions ----------------------------------------------------


async def test_admin_approve_confirms_books_the_slot_and_creates_reminders(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _submit(db, client, session_type_id, future_slot)
    drain(db)

    confirmed = await booking.admin_approve(db, request.id)

    assert confirmed.status is RequestStatus.confirmed
    assert confirmed.scheduled_start == future_slot.starts_at
    assert confirmed.confirmed_at is not None
    assert confirmed.scheduled_duration_min == 60
    assert future_slot.status is SlotStatus.booked

    reminders = (
        (await db.execute(select(Reminder).where(Reminder.request_id == request.id)))
        .scalars()
        .all()
    )
    assert {r.offset_min for r in reminders} == {1440, 60}
    assert any(isinstance(e, RequestConfirmed) for e in pending(db))


async def test_approving_a_free_text_request_without_a_time_is_refused(
    db: AsyncSession, client: Client, session_type_id: int
) -> None:
    """There is nothing to derive a scheduled_start from."""
    request = await booking.submit_free_time_request(
        db,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        desired_time_text="whenever",
        source_channel=Channel.web,
    )
    with pytest.raises(InvalidTransition):
        await booking.admin_approve(db, request.id)


async def test_admin_propose_moves_to_negotiating_and_records_the_message(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _submit(db, client, session_type_id, future_slot)
    negotiating = await booking.admin_propose(
        db, request.id, proposed_start=LATER, body_text="how about this?"
    )

    assert negotiating.status is RequestStatus.negotiating
    message = (
        await db.execute(
            select(NegotiationMessage).where(NegotiationMessage.request_id == request.id)
        )
    ).scalar_one()
    assert message.sender is SenderType.admin
    assert message.kind is NegotiationKind.proposal


async def test_proposing_a_different_time_releases_the_held_slot(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§7.1: release the slot if the proposal names a different time."""
    request = await _submit(db, client, session_type_id, future_slot)
    assert future_slot.status is SlotStatus.held

    await booking.admin_propose(db, request.id, proposed_start=LATER)

    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available
    assert request.slot_id is None


async def test_proposing_the_held_time_keeps_the_slot_held(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The other half of §7.1, and the reason the comparison precedes the
    release: a therapist who proposes the time the client already picked -- to
    attach a note to it, say -- must not thereby put that slot back on the
    picker for someone else to take mid-negotiation.
    """
    request = await _submit(db, client, session_type_id, future_slot)
    assert future_slot.status is SlotStatus.held

    await booking.admin_propose(
        db, request.id, proposed_start=future_slot.starts_at, body_text="does this still suit?"
    )

    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.held
    assert future_slot.held_by_request == request.id
    assert request.slot_id == future_slot.id


async def test_a_submitted_request_holds_its_slot_for_as_long_as_it_can_live(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    """§7.2. `slot_hold_minutes` is the window a client has to finish a form.
    Once the form is in, the window that matters is the therapist's: the request
    stays pending for `pending_expiry_hours`, and the slot behind it used to go
    back on the picker after fifteen minutes while the request went on pointing
    at it.
    """
    request = await _submit(db, client, session_type_id, future_slot)

    assert future_slot.status is SlotStatus.held
    assert future_slot.hold_expires_at == request.expires_at
    assert future_slot.hold_expires_at > datetime.now(UTC) + timedelta(
        minutes=practice.slot_hold_minutes
    )


async def test_a_proposal_re_stamps_the_hold_on_the_slot_it_keeps(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """A negotiation has no expiry of its own -- §7.1 expires only `pending`. So
    a hold inherited from submission would lapse mid-conversation and put the
    time under discussion back on the picker. It follows the conversation.
    """
    request = await _submit(db, client, session_type_id, future_slot)
    at_submission = future_slot.hold_expires_at

    with time_machine.travel(datetime.now(UTC) + timedelta(hours=24), tick=False):
        await booking.admin_propose(
            db, request.id, proposed_start=future_slot.starts_at, body_text="does this still suit?"
        )

    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.held
    assert future_slot.hold_expires_at > at_submission


async def test_admin_reject_releases_the_slot(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _submit(db, client, session_type_id, future_slot)
    rejected = await booking.admin_reject(db, request.id, reason="not taking new clients")

    assert rejected.status is RequestStatus.rejected
    assert rejected.rejected_reason == "not taking new clients"
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available


async def test_expire_releases_the_slot(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _submit(db, client, session_type_id, future_slot)
    expired = await booking.expire_request(db, request.id)

    assert expired.status is RequestStatus.expired
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available


# --- negotiating transitions ------------------------------------------------


async def test_client_accept_confirms_at_the_last_admin_proposal(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _negotiating(db, client, session_type_id, future_slot)
    confirmed = await booking.client_accept(db, request.id)

    assert confirmed.status is RequestStatus.confirmed
    assert confirmed.scheduled_start == LATER
    assert confirmed.confirmed_at is not None


async def test_client_accept_uses_the_most_recent_proposal(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _negotiating(db, client, session_type_id, future_slot)
    newer = LATER + timedelta(days=1)
    await booking.admin_propose(db, request.id, proposed_start=newer)

    confirmed = await booking.client_accept(db, request.id)
    assert confirmed.scheduled_start == newer


async def test_accepting_a_free_text_proposal_tells_the_therapist(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§7.1: "some evening next week" has no instant to confirm against, so
    accepting it cannot confirm anything -- no `scheduled_start`, no reminder,
    nothing for the schedule to draw.

    It used to be refused outright, which was worse than useless: the client's
    tap did nothing visible and the one person who could move it forward was
    told nothing. The agreement is recorded and she is asked for a time.
    """
    request = await _negotiating(db, client, session_type_id, future_slot, proposed=None)
    drain(db)

    same = await booking.client_accept(db, request.id)

    assert same.status is RequestStatus.negotiating
    assert same.scheduled_start is None
    assert same.confirmed_at is None

    accept = (
        await db.execute(
            select(NegotiationMessage).where(
                NegotiationMessage.request_id == request.id,
                NegotiationMessage.kind == NegotiationKind.accept,
            )
        )
    ).scalar_one()
    assert accept.sender is SenderType.client

    assert any(isinstance(e, RequestAccepted) for e in pending(db))
    # Not a confirmation: nothing may be scheduled from words.
    assert not any(isinstance(e, RequestConfirmed) for e in pending(db))

    # And the turn goes back to the therapist, who is the one who sets a time.
    assert await booking.whose_turn(db, request.id) is SenderType.admin


async def test_accepting_a_proposal_that_names_a_time_still_confirms(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The other branch, unchanged."""
    request = await _negotiating(db, client, session_type_id, future_slot, proposed=LATER)

    confirmed = await booking.client_accept(db, request.id)

    assert confirmed.status is RequestStatus.confirmed
    assert confirmed.scheduled_start == LATER


async def test_client_counter_stays_negotiating(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _negotiating(db, client, session_type_id, future_slot)
    countered = await booking.client_counter(db, request.id, body_text="earlier would help")

    assert countered.status is RequestStatus.negotiating


async def test_whose_turn_is_derived_from_the_last_sender(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§6.6: derived, never stored."""
    request = await _negotiating(db, client, session_type_id, future_slot)
    assert await booking.whose_turn(db, request.id) is SenderType.client

    await booking.client_counter(db, request.id, body_text="or Friday?")
    assert await booking.whose_turn(db, request.id) is SenderType.admin


async def test_a_system_message_does_not_take_anybody_s_turn(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§6.6 is an admin/client either-or. Nothing writes a `system` row today,
    so this is a guard on the day something does: read as "not admin", one
    system row would hand the turn to the therapist and leave the client with
    no way to answer.
    """
    request = await _negotiating(db, client, session_type_id, future_slot)
    assert await booking.whose_turn(db, request.id) is SenderType.client

    db.add(
        NegotiationMessage(
            request_id=request.id,
            sender=SenderType.system,
            kind=NegotiationKind.note,
            body_text="reminder sent",
        )
    )
    await db.flush()

    assert await booking.whose_turn(db, request.id) is SenderType.client


# --- Client notes (§7.1, status-neutral) ------------------------------------


async def test_a_note_records_the_message_without_moving_the_status(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _submit(db, client, session_type_id, future_slot)
    noted = await booking.client_note(db, request.id, body_text="  I may be five minutes late  ")

    assert noted.status is RequestStatus.pending
    message = (
        (
            await db.execute(
                select(NegotiationMessage).where(NegotiationMessage.request_id == request.id)
            )
        )
        .scalars()
        .one()
    )
    assert message.sender is SenderType.client
    assert message.kind is NegotiationKind.note
    assert message.body_text == "I may be five minutes late"


async def test_a_note_is_accepted_while_the_therapist_can_still_act(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _negotiating(db, client, session_type_id, future_slot)
    assert (await booking.client_note(db, request.id, body_text="a thought")).status is (
        RequestStatus.negotiating
    )

    confirmed = await booking.client_accept(db, request.id)
    assert (await booking.client_note(db, confirmed.id, body_text="another")).status is (
        RequestStatus.confirmed
    )


async def test_a_note_on_a_finished_request_is_refused(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """Nobody is going to read it."""
    request = await _submit(db, client, session_type_id, future_slot)
    await booking.admin_reject(db, request.id)

    with pytest.raises(InvalidTransition):
        await booking.client_note(db, request.id, body_text="please reconsider")


async def test_an_empty_note_is_refused(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _submit(db, client, session_type_id, future_slot)
    with pytest.raises(InvalidTransition):
        await booking.client_note(db, request.id, body_text="   ")


async def test_a_note_is_audited_by_identifier_only(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """Hard rule 8: the audit log names the request, never the content."""
    request = await _submit(db, client, session_type_id, future_slot)
    await booking.client_note(db, request.id, body_text="something private")

    entry = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == "request.note")
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .one()
    )
    assert entry.entity_id == str(request.uuid)
    assert entry.actor_type is ActorType.client
    assert "something private" not in str(entry.meta)


async def test_client_decline_rejects_and_releases(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _negotiating(db, client, session_type_id, future_slot, proposed=None)
    declined = await booking.client_decline(db, request.id)
    assert declined.status is RequestStatus.rejected


# --- confirmed transitions --------------------------------------------------


async def test_admin_cancel_releases_the_slot_and_cancels_reminders(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _confirmed(db, client, session_type_id, future_slot)
    cancelled = await booking.admin_cancel(db, request.id, reason="ill")

    assert cancelled.status is RequestStatus.cancelled
    assert cancelled.cancelled_by is ActorType.admin
    assert cancelled.cancellation_reason == "ill"

    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available

    states = (
        (await db.execute(select(Reminder.state).where(Reminder.request_id == request.id)))
        .scalars()
        .all()
    )
    assert all(state is ReminderState.cancelled for state in states)


async def test_complete_releases_the_slot_and_emits_nothing(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§7.1: set by the worker, no notification."""
    request = await _confirmed(db, client, session_type_id, future_slot)
    drain(db)

    completed = await booking.complete_request(db, request.id)

    assert completed.status is RequestStatus.completed
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available
    assert pending(db) == []


async def test_a_reminder_already_due_at_confirmation_is_skipped_not_fired(
    db: AsyncSession, client: Client, session_type_id: int, practice: Practice
) -> None:
    """DESIGN.md §13."""
    soon = datetime.now(UTC) + timedelta(minutes=30)
    request = await booking.submit_free_time_request(
        db,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        desired_time_text="as soon as possible",
        source_channel=Channel.web,
    )
    await booking.admin_approve(db, request.id, scheduled_start=soon)

    reminders = {
        r.offset_min: r.state
        for r in (
            await db.execute(select(Reminder).where(Reminder.request_id == request.id))
        ).scalars()
    }
    assert reminders[1440] is ReminderState.skipped
    assert reminders[60] is ReminderState.skipped


# --- audit ------------------------------------------------------------------


async def test_state_changes_are_audited_without_leaking_problem_text(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """Hard rule 8: identifiers, never content."""
    request = await _submit(db, client, session_type_id, future_slot)
    await booking.admin_approve(db, request.id)

    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.entity_id == str(request.uuid))))
        .scalars()
        .all()
    )
    assert {row.action for row in rows} == {"request.submit", "request.confirm"}
    for row in rows:
        assert "not logged, not emailed" not in str(row.meta)


# --- the rejection matrix ---------------------------------------------------

#: §7.1, restated independently of the implementation. If these two ever agree
#: only because one was copied from the other, the test is worthless -- so this
#: is written from the specification table, not from booking.ALLOWED.
LEGAL: dict[RequestStatus, set[str]] = {
    RequestStatus.pending: {"admin_approve", "admin_propose", "admin_reject", "expire"},
    RequestStatus.negotiating: {
        "client_accept",
        "client_counter",
        "admin_approve",
        "admin_propose",
        "client_decline",
        "admin_reject",
    },
    RequestStatus.confirmed: {"admin_cancel", "complete"},
    RequestStatus.rejected: set(),
    RequestStatus.expired: set(),
    RequestStatus.cancelled: set(),
    RequestStatus.completed: set(),
}

ALL_EVENTS = sorted({event for events in LEGAL.values() for event in events})


async def _invoke(db: AsyncSession, event: str, request_id: int) -> None:
    calls = {
        "admin_approve": lambda: booking.admin_approve(db, request_id, scheduled_start=LATER),
        "admin_propose": lambda: booking.admin_propose(db, request_id, proposed_start=LATER),
        "admin_reject": lambda: booking.admin_reject(db, request_id),
        "expire": lambda: booking.expire_request(db, request_id),
        "client_accept": lambda: booking.client_accept(db, request_id),
        "client_counter": lambda: booking.client_counter(db, request_id, body_text="x"),
        "client_decline": lambda: booking.client_decline(db, request_id),
        "admin_cancel": lambda: booking.admin_cancel(db, request_id, reason="x"),
        "complete": lambda: booking.complete_request(db, request_id),
    }
    await calls[event]()


async def _request_in(
    db: AsyncSession, status: RequestStatus, client: Client, session_type_id: int, slot: Slot
) -> BookingRequest:
    request = await _submit(db, client, session_type_id, slot)
    if status is RequestStatus.pending:
        return request
    if status is RequestStatus.negotiating:
        return await booking.admin_propose(db, request.id, proposed_start=LATER)
    if status is RequestStatus.confirmed:
        return await booking.admin_approve(db, request.id)
    if status is RequestStatus.rejected:
        return await booking.admin_reject(db, request.id)
    if status is RequestStatus.expired:
        return await booking.expire_request(db, request.id)
    if status is RequestStatus.cancelled:
        confirmed = await booking.admin_approve(db, request.id)
        return await booking.admin_cancel(db, confirmed.id, reason="x")
    if status is RequestStatus.completed:
        confirmed = await booking.admin_approve(db, request.id)
        return await booking.complete_request(db, confirmed.id)
    raise AssertionError(status)


@pytest.mark.parametrize("status", list(LEGAL))
@pytest.mark.parametrize("event", ALL_EVENTS)
async def test_illegal_transitions_are_refused_and_change_nothing(
    db: AsyncSession,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    status: RequestStatus,
    event: str,
) -> None:
    if event in LEGAL[status]:
        pytest.skip(f"{event} is legal from {status.value}")

    request = await _request_in(db, status, client, session_type_id, future_slot)
    before = request.status
    audit_before = (await db.execute(select(func.count()).select_from(AuditLog))).scalar_one()

    with pytest.raises(InvalidTransition):
        await _invoke(db, event, request.id)

    assert request.status is before, "a refused transition must change nothing"
    audit_after = (await db.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert audit_after == audit_before


async def test_there_is_no_path_from_confirmed_back_to_negotiating(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """DESIGN.md §7. A change of time after confirmation is a cancellation plus
    a new request; anything else makes reminders and slot bookkeeping lie."""
    request = await _confirmed(db, client, session_type_id, future_slot)

    with pytest.raises(InvalidTransition):
        await booking.admin_propose(db, request.id, proposed_start=LATER)
    with pytest.raises(InvalidTransition):
        await booking.client_counter(db, request.id, body_text="actually...")

    assert request.status is RequestStatus.confirmed


@pytest.mark.parametrize(
    "terminal",
    [
        RequestStatus.rejected,
        RequestStatus.expired,
        RequestStatus.cancelled,
        RequestStatus.completed,
    ],
)
async def test_terminal_states_accept_nothing(
    db: AsyncSession,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    terminal: RequestStatus,
) -> None:
    request = await _request_in(db, terminal, client, session_type_id, future_slot)
    for event in ALL_EVENTS:
        with pytest.raises(InvalidTransition):
            await _invoke(db, event, request.id)
    assert request.status is terminal


async def test_slot_service_is_reachable_without_any_channel_import() -> None:
    """A canary for the architecture rule: the core is importable on its own."""
    assert slot_service.hold_slot is not None


# --- the week schedule (§12.2) ----------------------------------------------


async def _at(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    status: RequestStatus,
    *,
    scheduled: datetime | None = None,
    slot: Slot | None = None,
    desired: str | None = None,
) -> BookingRequest:
    """A request placed directly in a status, bypassing the transitions.

    The schedule is a read, so what matters is the shape of the row, not the
    path that produced it -- and driving four statuses through §7.1 here would
    test the state machine a third time instead of the query.
    """
    request = BookingRequest(
        practice_id=practice.id,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        status=status,
        source_channel=Channel.web,
        slot_id=slot.id if slot is not None else None,
        scheduled_start=scheduled,
        # The `confirmed_requires_schedule` check constraint (§6.5).
        confirmed_at=datetime.now(UTC) if status is RequestStatus.confirmed else None,
        desired_time_text=desired,
    )
    db.add(request)
    await db.flush()
    return request


async def test_schedule_places_confirmed_by_its_scheduled_start(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> None:
    request = await _at(
        db, practice, client, session_type_id, RequestStatus.confirmed, scheduled=LATER
    )

    entries = await booking.scheduled_in_window(
        db, window_from=LATER - timedelta(days=1), window_to=LATER + timedelta(days=1)
    )

    assert [(e.uuid, e.starts_at) for e in entries] == [(request.uuid, LATER)]


async def test_schedule_places_a_pending_request_by_its_held_slot(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
) -> None:
    """§7.1 sets `scheduled_start` only at approval, so before that the instant
    the therapist needs to see is the one the client is holding."""
    request = await _at(
        db, practice, client, session_type_id, RequestStatus.pending, slot=future_slot
    )

    entries = await booking.scheduled_in_window(
        db,
        window_from=future_slot.starts_at - timedelta(hours=1),
        window_to=future_slot.starts_at + timedelta(hours=1),
    )

    assert [(e.uuid, e.starts_at) for e in entries] == [(request.uuid, future_slot.starts_at)]


async def test_schedule_shows_completed_so_a_past_week_is_not_empty(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> None:
    """The worker sweeps confirmed to completed once the end passes (§14).
    Without this the previous week renders blank, which is a lie."""
    last_week = datetime.now(UTC) - timedelta(days=5)
    request = await _at(
        db, practice, client, session_type_id, RequestStatus.completed, scheduled=last_week
    )

    entries = await booking.scheduled_in_window(
        db, window_from=last_week - timedelta(days=1), window_to=last_week + timedelta(days=1)
    )

    assert [e.uuid for e in entries] == [request.uuid]


@pytest.mark.parametrize(
    "finished",
    [RequestStatus.rejected, RequestStatus.expired, RequestStatus.cancelled],
)
async def test_schedule_omits_requests_that_will_not_happen(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    finished: RequestStatus,
) -> None:
    await _at(db, practice, client, session_type_id, finished, scheduled=LATER)

    entries = await booking.scheduled_in_window(
        db, window_from=LATER - timedelta(days=1), window_to=LATER + timedelta(days=1)
    )

    assert entries == []


async def test_schedule_window_includes_its_start_and_excludes_its_end(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> None:
    """Half-open, so consecutive weeks neither drop a session nor show it twice."""
    opening = await _at(
        db, practice, client, session_type_id, RequestStatus.confirmed, scheduled=LATER
    )
    closing = await _at(
        db,
        practice,
        client,
        session_type_id,
        RequestStatus.confirmed,
        scheduled=LATER + timedelta(days=7),
    )

    entries = await booking.scheduled_in_window(
        db, window_from=LATER, window_to=LATER + timedelta(days=7)
    )

    # Asserted about this test's own two requests rather than as an exact list:
    # the query is unfiltered and the suite shares its database with a running
    # install, so any real booking landing in the window would fail it.
    found = {e.uuid for e in entries}
    assert opening.uuid in found, "the instant the window opens on belongs to it"
    assert closing.uuid not in found, "the instant it closes on belongs to the next window"


async def test_schedule_orders_a_day_the_way_it_is_lived(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> None:
    evening = await _at(
        db,
        practice,
        client,
        session_type_id,
        RequestStatus.confirmed,
        scheduled=LATER + timedelta(hours=8),
    )
    morning = await _at(
        db, practice, client, session_type_id, RequestStatus.confirmed, scheduled=LATER
    )

    entries = await booking.scheduled_in_window(
        db, window_from=LATER - timedelta(days=1), window_to=LATER + timedelta(days=1)
    )

    assert [e.uuid for e in entries] == [morning.uuid, evening.uuid]


async def test_unscheduled_carries_the_wording_and_no_instant(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> None:
    request = await _at(
        db,
        practice,
        client,
        session_type_id,
        RequestStatus.negotiating,
        desired="some evening next week?",
    )

    entries = await booking.unscheduled_for_admin(db)

    # This test's own row, not an exact list: the query is unfiltered and the
    # suite shares its database with a running install, so any real request
    # waiting on a time would fail it.
    mine = [e for e in entries if e.uuid == request.uuid]
    assert [(e.desired_time_text, e.starts_at) for e in mine] == [
        ("some evening next week?", None)
    ]


async def test_a_request_with_a_time_is_never_in_both_places(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
) -> None:
    """The two lists partition the work; an entry appearing in both would be
    counted twice by the only person reading them."""
    scheduled = await _at(
        db, practice, client, session_type_id, RequestStatus.confirmed, scheduled=LATER
    )
    holding = await _at(
        db, practice, client, session_type_id, RequestStatus.pending, slot=future_slot
    )
    timeless = await _at(
        db, practice, client, session_type_id, RequestStatus.pending, desired="mornings"
    )

    placed = {
        e.uuid
        for e in await booking.scheduled_in_window(
            db,
            window_from=datetime.now(UTC) - timedelta(days=1),
            window_to=LATER + timedelta(days=30),
        )
    }
    beside = {e.uuid for e in await booking.unscheduled_for_admin(db, limit=200)}

    # Scoped to the rows this test made: the window is deliberately wide, and
    # the database it runs against is not empty.
    mine = {scheduled.uuid, holding.uuid, timeless.uuid}
    assert placed & mine == {scheduled.uuid, holding.uuid}
    assert beside & mine == {timeless.uuid}
    # Disjoint everywhere, not just here: one query wants an instant and the
    # other wants none, so no request can be counted twice.
    assert placed & beside == set()


# --- The practice learns who a client is (§12.1) ----------------------------


async def test_a_submitted_name_is_remembered_on_the_client(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """`client.display_name` was only ever written when the row was created,
    which Telegram does from the profile and the web cannot do at all -- the
    address arrives before the name. So the web asked "what should I call you?"
    on every booking and threw the answer onto the request.
    """
    assert not client.display_name

    await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        display_name="  Anna  ",
    )

    await db.refresh(client)
    assert client.display_name == "Anna"


async def test_a_later_name_does_not_rename_the_client(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    """§12.1: filled once, never overwritten. A typo on one booking must not
    rename the person on every other, and correcting a client is the
    therapist's to do."""
    client.display_name = "Anna"
    await db.flush()

    await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        display_name="Annna",
    )

    await db.refresh(client)
    assert client.display_name == "Anna"


async def test_a_blank_name_leaves_the_client_alone(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The name is optional on both channels, and whitespace is not a name."""
    await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        display_name="   ",
    )

    await db.refresh(client)
    assert not client.display_name


async def test_the_last_contact_note_is_the_one_offered_back(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    """§12.1 prefills step 3 with it. Unlike the name it stays per-request, so
    the prefill reads the newest one rather than a field on the client."""
    assert await booking.last_contact_note(db, client.id) is None

    await booking.submit_free_time_request(
        db,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        desired_time_text="any evening",
        source_channel=Channel.web,
        contact_note="phone after six",
    )
    assert await booking.last_contact_note(db, client.id) == "phone after six"

    # A later request without one must not erase the answer they did give.
    await booking.submit_free_time_request(
        db,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        desired_time_text="or a morning",
        source_channel=Channel.web,
    )
    assert await booking.last_contact_note(db, client.id) == "phone after six"


# --- A counter reaches the therapist with something in it (§10) -------------


async def test_a_countered_time_and_the_words_both_reach_the_therapist(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§10 puts `proposed_start` and `note` in the counter payload. The note was
    pinned to None, so a counter arrived carrying neither the client's time nor
    their words -- the reply she has to answer, with nothing in it.
    """
    request = await _negotiating(db, client, session_type_id, future_slot)
    drain(db)

    when = LATER + timedelta(days=1)
    await booking.client_counter(
        db, request.id, proposed_start=when, body_text="2026-08-29 13:30"
    )

    event = next(e for e in pending(db) if isinstance(e, RequestCounter))
    assert event.proposed_start == when
    assert event.note == "2026-08-29 13:30"


# --- Approving a negotiation (§7.1) -----------------------------------------


async def test_approving_a_negotiation_takes_the_time_last_put_forward(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """Client requests, therapist counters, client counters back, therapist
    approves. Her agreeing to the client's time is an approval; making her
    propose that same time back and be accepted again says nothing.

    Both surfaces offered the Approve button already -- §10 gives
    `request.counter.admin` the action -- and §7.1 did not allow it, so the
    button did nothing at all.
    """
    request = await _negotiating(db, client, session_type_id, future_slot)
    countered = LATER + timedelta(hours=2)
    await booking.client_counter(db, request.id, proposed_start=countered, body_text="later?")
    drain(db)

    confirmed = await booking.admin_approve(db, request.id)

    assert confirmed.status is RequestStatus.confirmed
    assert confirmed.scheduled_start == countered, "the client's time, not the therapist's"
    assert confirmed.confirmed_at is not None
    assert any(isinstance(e, RequestConfirmed) for e in pending(db))


async def test_approving_a_negotiation_with_an_explicit_time_still_wins(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The fallback is for the button, which carries no time. A time she names
    is the one she meant."""
    request = await _negotiating(db, client, session_type_id, future_slot)
    await booking.client_counter(db, request.id, proposed_start=LATER + timedelta(hours=2))

    named = LATER + timedelta(hours=5)
    confirmed = await booking.admin_approve(db, request.id, scheduled_start=named)

    assert confirmed.scheduled_start == named


async def test_a_words_only_negotiation_still_confirms_at_the_slot_it_holds(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """A proposal that names no time does not release the slot (§7.1), so the
    request is still holding the one the client picked. That is the instant."""
    request = await _negotiating(db, client, session_type_id, future_slot, proposed=None)
    await booking.client_counter(db, request.id, body_text="that works")

    confirmed = await booking.admin_approve(db, request.id)

    assert confirmed.scheduled_start == future_slot.starts_at


async def test_approving_a_negotiation_with_no_time_and_no_slot_is_refused(
    db: AsyncSession, client: Client, session_type_id: int
) -> None:
    """Nothing to confirm at all: free text throughout, and a session needs an
    instant (§7.1)."""
    request = await booking.submit_free_time_request(
        db,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        desired_time_text="some evening next week?",
        source_channel=Channel.web,
    )
    await booking.admin_propose(db, request.id, body_text="which evening suits?")
    await booking.client_counter(db, request.id, body_text="thursday maybe")

    with pytest.raises(InvalidTransition):
        await booking.admin_approve(db, request.id)


async def test_a_words_only_proposal_also_re_stamps_the_hold(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§7.1: a proposal that names no instant keeps the slot -- it has not moved
    away from it -- so it has to keep the hold alive too.

    Only the "proposed the very same time" branch re-stamped, which is the
    rarest case. A words-only proposal, the one a mistyped time produces, left
    the hold running on the clock started at submission: the slot went back on
    the picker mid-negotiation with the request still pointing at it.
    """
    request = await _submit(db, client, session_type_id, future_slot)
    at_submission = future_slot.hold_expires_at

    with time_machine.travel(datetime.now(UTC) + timedelta(hours=24), tick=False):
        await booking.admin_propose(db, request.id, body_text="which evening suits you?")

    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.held
    assert request.slot_id == future_slot.id
    assert future_slot.hold_expires_at > at_submission


async def test_a_client_counter_re_stamps_the_hold_too(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The other half of the conversation. A client still talking is a client
    still interested in the slot they picked."""
    request = await _submit(db, client, session_type_id, future_slot)
    await booking.admin_propose(db, request.id, body_text="which evening suits you?")
    before = future_slot.hold_expires_at

    with time_machine.travel(datetime.now(UTC) + timedelta(hours=24), tick=False):
        await booking.client_counter(db, request.id, body_text="thursday would be better")

    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.held
    assert future_slot.hold_expires_at > before
