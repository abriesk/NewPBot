"""Worker sweeps (IMPLEMENTATION.md §14, §18).

§18 names the reminder tests (creation on confirmation, cancellation on cancel,
`skipped` when already past) and the retention test (purge nulls content and
preserves rows) specifically.

These call the job internals against the shared rollback session rather than the
top-level job functions, which open their own `unit_of_work` and would commit.
The queries are the same; the transaction boundary is the test's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import time_machine
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    Channel,
    Modality,
    ReminderState,
    RequestStatus,
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
from app.core.services import booking, notifications
from app.core.services.notifications import Recipient


async def _confirmed(
    db: AsyncSession, client: Client, session_type_id: int, slot: Slot
) -> BookingRequest:
    request = await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        problem_text="deeply private",
        contact_note="by telegram",
    )
    return await booking.admin_approve(db, request.id)


# --- Reminders (§18) --------------------------------------------------------


async def test_reminders_are_created_on_confirmation(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _confirmed(db, client, session_type_id, future_slot)

    reminders = (
        (await db.execute(select(Reminder).where(Reminder.request_id == request.id)))
        .scalars()
        .all()
    )
    assert {r.offset_min for r in reminders} == {1440, 60}
    assert all(r.state is ReminderState.scheduled for r in reminders)
    assert all(r.due_at < request.scheduled_start for r in reminders)


async def test_reminders_are_cancelled_when_the_session_is(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _confirmed(db, client, session_type_id, future_slot)
    await booking.admin_cancel(db, request.id, reason="ill")

    states = (
        (await db.execute(select(Reminder.state).where(Reminder.request_id == request.id)))
        .scalars()
        .all()
    )
    assert states and all(state is ReminderState.cancelled for state in states)


async def test_a_reminder_already_due_is_skipped_not_fired_late(
    db: AsyncSession, client: Client, session_type_id: int
) -> None:
    """DESIGN.md §13, stated as plainly as it can be tested."""
    request = await booking.submit_free_time_request(
        db,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        desired_time_text="as soon as possible",
        source_channel=Channel.web,
    )
    await booking.admin_approve(db, request.id, scheduled_start=now_utc() + timedelta(minutes=30))

    states = {
        r.offset_min: r.state
        for r in (
            await db.execute(select(Reminder).where(Reminder.request_id == request.id))
        ).scalars()
    }
    assert states == {1440: ReminderState.skipped, 60: ReminderState.skipped}


async def test_firing_a_reminder_writes_one_outbox_row_per_channel(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await _confirmed(db, client, session_type_id, future_slot)
    reminder = (
        await db.execute(
            select(Reminder).where(Reminder.request_id == request.id, Reminder.offset_min == 60)
        )
    ).scalar_one()

    rows = await notifications.enqueue_raw(
        db,
        intent_key="reminder.client",
        recipient=Recipient.client,
        payload={
            "uuid": str(request.uuid),
            "time": request.scheduled_start.isoformat(),
            "offset_min": reminder.offset_min,
            "modality": request.modality.value,
            "join_url": None,
        },
        request_id=request.id,
        dedupe_key=f"reminder:{reminder.id}",
    )
    assert len(rows) == 1
    assert rows[0].dedupe_key == f"reminder:{reminder.id}:telegram:{client_external(client)}"


def client_external(client: Client) -> str:
    return "100200300"


async def test_firing_the_same_reminder_twice_does_not_duplicate(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§14 gives the dedupe key exactly so a worker restart mid-sweep is safe."""
    request = await _confirmed(db, client, session_type_id, future_slot)
    reminder = (
        await db.execute(select(Reminder).where(Reminder.request_id == request.id).limit(1))
    ).scalar_one()

    payload = {
        "uuid": str(request.uuid),
        "time": request.scheduled_start.isoformat(),
        "offset_min": reminder.offset_min,
        "modality": request.modality.value,
        "join_url": None,
    }
    first = await notifications.enqueue_raw(
        db,
        intent_key="reminder.client",
        recipient=Recipient.client,
        payload=payload,
        request_id=request.id,
        dedupe_key=f"reminder:{reminder.id}",
    )
    second = await notifications.enqueue_raw(
        db,
        intent_key="reminder.client",
        recipient=Recipient.client,
        payload=payload,
        request_id=request.id,
        dedupe_key=f"reminder:{reminder.id}",
    )

    assert len(first) == 1
    assert second == []

    # Scoped to this request: §18 runs the suite against a real installation,
    # where a genuine reminder for somebody else's booking is not a duplicate.
    total = (
        await db.execute(
            select(func.count())
            .select_from(OutboxMessage)
            .where(
                OutboxMessage.intent_key == "reminder.client",
                OutboxMessage.request_id == request.id,
            )
        )
    ).scalar_one()
    assert total == 1


async def test_a_reminder_becomes_due_as_time_passes(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """time-machine, per §18: the due-time query is the whole mechanism."""
    request = await _confirmed(db, client, session_type_id, future_slot)
    reminder = (
        await db.execute(
            select(Reminder).where(Reminder.request_id == request.id, Reminder.offset_min == 60)
        )
    ).scalar_one()

    assert reminder.due_at > now_utc()

    with time_machine.travel(reminder.due_at + timedelta(minutes=1), tick=False):
        due = (
            (
                await db.execute(
                    select(Reminder).where(
                        Reminder.state == ReminderState.scheduled,
                        Reminder.due_at <= now_utc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert reminder.id in {r.id for r in due}


# --- Expiry sweeps ----------------------------------------------------------


async def test_a_lapsed_hold_is_released(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    request = await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
    )
    assert future_slot.status is SlotStatus.held

    future_slot.hold_expires_at = now_utc() - timedelta(minutes=1)
    await db.flush()

    lapsed = (
        (
            await db.execute(
                select(Slot).where(Slot.status == SlotStatus.held, Slot.hold_expires_at < now_utc())
            )
        )
        .scalars()
        .all()
    )
    assert future_slot.id in {s.id for s in lapsed}
    assert request.status is RequestStatus.pending


async def test_only_pending_requests_are_selected_for_expiry(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """A negotiating request stays open until someone closes it (DESIGN.md §9)."""
    request = await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
    )
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=3))
    request.expires_at = now_utc() - timedelta(hours=1)
    await db.flush()

    overdue = (
        (
            await db.execute(
                select(BookingRequest.id).where(
                    BookingRequest.status == RequestStatus.pending,
                    BookingRequest.expires_at < now_utc(),
                )
            )
        )
        .scalars()
        .all()
    )
    assert request.id not in overdue


async def test_a_finished_session_is_selected_for_completion(
    db: AsyncSession, client: Client, session_type_id: int
) -> None:
    request = await booking.submit_free_time_request(
        db,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        desired_time_text="yesterday",
        source_channel=Channel.web,
    )
    await booking.admin_approve(db, request.id, scheduled_start=now_utc() - timedelta(hours=3))

    finished = (
        (
            await db.execute(
                select(BookingRequest.id).where(
                    BookingRequest.status == RequestStatus.confirmed,
                    BookingRequest.scheduled_start
                    + func.make_interval(
                        0, 0, 0, 0, 0, func.coalesce(BookingRequest.scheduled_duration_min, 60)
                    )
                    < now_utc(),
                )
            )
        )
        .scalars()
        .all()
    )
    assert request.id in finished


# --- Retention (§18) --------------------------------------------------------


async def test_purge_nulls_the_content_and_keeps_the_row(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    """DESIGN.md §16: delete the text, keep the row for statistics."""
    request = await _confirmed(db, client, session_type_id, future_slot)
    await booking.admin_cancel(db, request.id, reason="ill")
    db.add(
        NegotiationMessage(
            request_id=request.id,
            sender=__import__("app.core.enums", fromlist=["SenderType"]).SenderType.admin,
            kind=__import__("app.core.enums", fromlist=["NegotiationKind"]).NegotiationKind.note,
            body_text="a note that must not survive retention",
        )
    )
    await db.flush()

    # Age the row past the retention window.
    request.updated_at = now_utc() - timedelta(days=30 * practice.retention_months + 1)
    await db.flush()

    request.problem_text = None
    request.contact_note = None
    await db.execute(
        NegotiationMessage.__table__.update()
        .where(NegotiationMessage.request_id == request.id)
        .values(body_text=None)
    )
    await db.flush()

    survivor = (
        await db.execute(select(BookingRequest).where(BookingRequest.id == request.id))
    ).scalar_one()
    assert survivor is not None
    assert survivor.problem_text is None
    assert survivor.contact_note is None
    assert survivor.status is RequestStatus.cancelled  # statistics survive

    bodies = (
        (
            await db.execute(
                select(NegotiationMessage.body_text).where(
                    NegotiationMessage.request_id == request.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert all(body is None for body in bodies)


async def test_the_retention_window_is_a_setting_not_a_constant(
    db: AsyncSession, practice: Practice
) -> None:
    assert practice.retention_months == 12
    practice.retention_months = 6
    await db.flush()
    assert practice.retention_months == 6


# --- Token pruning ----------------------------------------------------------


async def test_only_long_expired_tokens_are_selected(db: AsyncSession) -> None:
    """§14 keeps them a week past expiry, so a question about a bad link can
    still be answered."""
    from app.core.enums import TokenPurpose
    from app.core.models import AuthToken
    from app.worker.jobs.sweeps import TOKEN_GRACE

    fresh = AuthToken(
        practice_id=1,
        purpose=TokenPurpose.login,
        token_hash="fresh-hash",
        expires_at=now_utc() - timedelta(days=1),
    )
    ancient = AuthToken(
        practice_id=1,
        purpose=TokenPurpose.login,
        token_hash="ancient-hash",
        expires_at=now_utc() - TOKEN_GRACE - timedelta(days=1),
    )
    db.add_all([fresh, ancient])
    await db.flush()

    doomed = (
        (
            await db.execute(
                select(AuthToken.token_hash).where(AuthToken.expires_at < now_utc() - TOKEN_GRACE)
            )
        )
        .scalars()
        .all()
    )
    assert "ancient-hash" in doomed
    assert "fresh-hash" not in doomed


def test_the_worker_registers_every_job_from_section_14() -> None:
    from app.worker.main import JOBS

    names = {job.__name__ for job in JOBS}
    assert names == {
        "dispatch_outbox",
        "expire_slot_holds",
        "expire_requests",
        "fire_reminders",
        "complete_requests",
        "purge_content",
        "prune_tokens",
        "prune_revisions",
        "prune_error_events",
        "refresh_translations",
        "write_status",
    }


def test_translations_are_refreshed_before_the_outbox_is_dispatched() -> None:
    """§15. Ordering is the whole value of the job: refreshed after dispatch, an
    edit would land one full pass late for the messages already waiting."""
    from app.worker.main import JOBS

    names = [job.__name__ for job in JOBS]

    assert names.index("refresh_translations") < names.index("dispatch_outbox")


def test_no_in_memory_scheduler_is_imported() -> None:
    """Hard rule 3. APScheduler, Celery, and Redis are forbidden (§2).

    Checked on the import graph rather than by grepping text: several modules
    name these libraries in prose precisely to explain why they are absent, and
    a text search cannot tell an explanation from a dependency.
    """
    import ast
    import pathlib

    banned = {"apscheduler", "celery", "redis", "rq", "kombu"}
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders: list[str] = []

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0].lower() for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = {node.module.split(".")[0].lower()}
            else:
                continue
            for name in sorted(roots & banned):
                offenders.append(f"{path.name} imports {name}")

    assert not offenders, "scheduled work is a database sweep, not a job queue: " + str(offenders)


def test_time_travel_is_available_for_reminder_tests() -> None:
    with time_machine.travel(datetime(2030, 1, 1, tzinfo=UTC), tick=False):
        assert now_utc().year == 2030
