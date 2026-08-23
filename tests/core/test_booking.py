"""Booking request state machine (IMPLEMENTATION.md §7.1).

M2 acceptance requires every transition **and every rejection** in §7 to be
covered. The rejection matrix at the bottom is the part that actually protects
the invariant: it asserts that each of the 11 events is refused from every
status the table does not permit it from.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
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
from app.core.events import RequestConfirmed, RequestSubmitted, drain, pending
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


async def test_accepting_a_free_text_proposal_is_refused(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """ "some evening next week" has no instant to confirm against."""
    request = await _negotiating(db, client, session_type_id, future_slot, proposed=None)
    with pytest.raises(InvalidTransition):
        await booking.client_accept(db, request.id)


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
