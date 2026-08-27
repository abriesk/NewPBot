"""Health checks (IMPLEMENTATION.md §16.9, §18).

Each threshold is asserted either side of its boundary, because a check that is
merely "roughly right" is worse than none: it either cries wolf until the dot
is ignored, or stays green through a real fault (DESIGN.md §22.4).

These run against the rollback session, so the outbox rows they invent never
reach a real dispatcher.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel, CheckState, ErrorSource, OutboxStatus, ReminderState
from app.core.models import ErrorEvent, OutboxAttempt, OutboxMessage, Practice, Reminder
from app.core.policies import now_utc
from app.core.services.status import (
    FAILING_WARN_AT,
    OVERDUE_GRACE,
    WEB_ERRORS_FAIL_AT,
    WORKER_ERRORS_FAIL_AT,
    Check,
    check_schema_version,
    record_error,
    run_checks,
    stored_schema_revision,
    where,
)


@pytest_asyncio.fixture(autouse=True)
async def isolated(db: AsyncSession) -> None:
    """Empty the tables these checks read, inside the test's own transaction.

    The suite runs against a real installation (§18), so without this the
    result of "is anything undelivered?" depends on whether anything really is
    -- the tests would pass on a healthy server and fail on the one where they
    matter. The `db` fixture rolls all of this back.
    """
    await db.execute(delete(OutboxAttempt))
    await db.execute(delete(OutboxMessage))
    await db.execute(delete(Reminder))
    await db.execute(delete(ErrorEvent))
    await db.flush()


async def _outbox(
    db: AsyncSession,
    practice: Practice,
    *,
    status: OutboxStatus,
    created_delta: timedelta = timedelta(),
    next_delta: timedelta = timedelta(),
) -> OutboxMessage:
    row = OutboxMessage(
        practice_id=practice.id,
        channel=Channel.telegram,
        address="100200300",
        intent_key="request.confirmed.client",
        payload={},
        locale="ru",
        status=status,
        created_at=now_utc() + created_delta,
        next_attempt_at=now_utc() + next_delta,
    )
    db.add(row)
    await db.flush()
    return row


async def _checks(db: AsyncSession) -> dict[str, Check]:
    return {check.id: check for check in await run_checks(db)}


async def _one(db: AsyncSession, check_id: str) -> Check:
    return (await _checks(db))[check_id]


# --- A healthy install ------------------------------------------------------


async def test_a_healthy_database_reports_every_check_ok(db: AsyncSession) -> None:
    head = await stored_schema_revision(db)

    checks = {c.id: c for c in await run_checks(db, code_head=head)}

    assert set(checks) == {
        "outbox_dead",
        "outbox_failing",
        "outbox_stalled",
        "outbox_stuck",
        "reminders_overdue",
        "web_errors",
        "worker_errors",
        "practice_row",
        "schema_version",
    }
    assert all(c.state is CheckState.ok for c in checks.values()), [
        (c.id, c.detail) for c in checks.values() if c.state is not CheckState.ok
    ]


async def test_an_ok_check_carries_no_summary(db: AsyncSession) -> None:
    """The therapist's sentence exists to explain a problem. A green row that
    also explains itself trains the eye to skip the column."""
    for check in await run_checks(db, code_head=await stored_schema_revision(db)):
        if check.state is CheckState.ok:
            assert check.summary == ""


# --- Delivery ---------------------------------------------------------------


async def test_a_dead_message_is_red(db: AsyncSession, practice: Practice) -> None:
    await _outbox(db, practice, status=OutboxStatus.dead)

    check = await _one(db, "outbox_dead")

    assert check.state is CheckState.fail
    assert "1 message" in check.summary
    assert "1 dead" in check.detail


async def test_a_dead_message_older_than_the_window_is_not(
    db: AsyncSession, practice: Practice
) -> None:
    await _outbox(db, practice, status=OutboxStatus.dead, created_delta=timedelta(days=-8))

    assert (await _one(db, "outbox_dead")).state is CheckState.ok


async def test_retrying_messages_are_amber_only_past_the_threshold(
    db: AsyncSession, practice: Practice
) -> None:
    for _ in range(FAILING_WARN_AT - 1):
        await _outbox(db, practice, status=OutboxStatus.failed)
    assert (await _one(db, "outbox_failing")).state is CheckState.ok

    await _outbox(db, practice, status=OutboxStatus.failed)
    check = await _one(db, "outbox_failing")

    assert check.state is CheckState.warn
    assert "retried" in check.summary


async def test_a_pending_message_is_red_only_once_overdue(
    db: AsyncSession, practice: Practice
) -> None:
    await _outbox(
        db, practice, status=OutboxStatus.pending, next_delta=-OVERDUE_GRACE + timedelta(minutes=1)
    )
    assert (await _one(db, "outbox_stalled")).state is CheckState.ok

    await _outbox(
        db, practice, status=OutboxStatus.pending, next_delta=-OVERDUE_GRACE - timedelta(minutes=1)
    )
    assert (await _one(db, "outbox_stalled")).state is CheckState.fail


async def test_an_interrupted_send_is_red_only_once_it_is_stale(
    db: AsyncSession, practice: Practice
) -> None:
    """§14 commits `sending` before the message reaches a transport, so a row
    left there is a process that died mid-send. Nothing retries it -- whether it
    arrived is exactly what nobody knows -- so the dot is what surfaces it.

    A row *currently* being sent is in the same state, which is why the check
    waits for the row to go stale rather than firing on the state alone.
    """
    from app.core.services.status import STUCK_GRACE

    await _outbox(
        db,
        practice,
        status=OutboxStatus.sending,
        created_delta=-STUCK_GRACE + timedelta(minutes=1),
    )
    assert (await _one(db, "outbox_stuck")).state is CheckState.ok

    await _outbox(
        db,
        practice,
        status=OutboxStatus.sending,
        created_delta=-STUCK_GRACE - timedelta(minutes=1),
    )
    assert (await _one(db, "outbox_stuck")).state is CheckState.fail


async def test_an_overdue_reminder_is_red(db: AsyncSession, request_id: int) -> None:
    db.add(
        Reminder(
            request_id=request_id,
            offset_min=60,
            due_at=now_utc() - OVERDUE_GRACE - timedelta(minutes=1),
            state=ReminderState.scheduled,
        )
    )
    await db.flush()

    check = await _one(db, "reminders_overdue")

    assert check.state is CheckState.fail
    assert "did not fire" in check.summary


async def test_a_reminder_due_in_the_future_is_not(db: AsyncSession, request_id: int) -> None:
    db.add(
        Reminder(
            request_id=request_id,
            offset_min=60,
            due_at=now_utc() + timedelta(hours=1),
            state=ReminderState.scheduled,
        )
    )
    await db.flush()

    assert (await _one(db, "reminders_overdue")).state is CheckState.ok


# --- Exceptions -------------------------------------------------------------


async def _errors(db: AsyncSession, source: ErrorSource, count: int, location: str) -> None:
    for _ in range(count):
        await record_error(
            db, source=source, exc=ValueError("secret@example.com"), location=location
        )


async def test_one_web_error_is_amber_and_many_are_red(db: AsyncSession) -> None:
    await _errors(db, ErrorSource.web, 1, "app.channels.web.client:10")
    assert (await _one(db, "web_errors")).state is CheckState.warn

    await _errors(db, ErrorSource.web, WEB_ERRORS_FAIL_AT - 1, "app.channels.web.client:10")
    check = await _one(db, "web_errors")

    assert check.state is CheckState.fail
    assert "ValueError" in check.detail


async def test_a_repeating_worker_error_is_red(db: AsyncSession) -> None:
    await _errors(db, ErrorSource.worker, WORKER_ERRORS_FAIL_AT - 1, "purge_content")
    assert (await _one(db, "worker_errors")).state is CheckState.warn

    await _errors(db, ErrorSource.worker, 1, "purge_content")
    assert (await _one(db, "worker_errors")).state is CheckState.fail


async def test_an_error_event_records_the_class_and_never_the_message(
    db: AsyncSession,
) -> None:
    """Hard rule 8 has no exception for tracebacks (DESIGN.md §22.5)."""
    try:
        raise ValueError("client wrote: my email is lena@example.com")
    except ValueError as exc:
        await record_error(db, source=ErrorSource.web, exc=exc, location=where(exc, "fallback"))

    row = (
        await db.execute(select(ErrorEvent).order_by(ErrorEvent.id.desc()).limit(1))
    ).scalar_one()

    assert row.kind == "ValueError"
    assert "lena@example.com" not in row.location
    assert "my email" not in row.location
    # `module:line` of where it was raised, not where it was caught.
    assert row.location.startswith("tests.core.test_status:")


async def test_the_detail_of_an_error_check_carries_no_message(db: AsyncSession) -> None:
    await _errors(db, ErrorSource.web, 1, "app.channels.web.client:10")

    check = await _one(db, "web_errors")

    assert "secret@example.com" not in check.detail
    assert "secret@example.com" not in check.summary


async def test_where_falls_back_when_there_is_no_traceback() -> None:
    assert where(ValueError("never raised"), "the-fallback") == "the-fallback"


# --- The database itself ----------------------------------------------------


async def test_a_schema_mismatch_is_red(db: AsyncSession) -> None:
    check = await check_schema_version(db, "0001_something_older")

    assert check.state is CheckState.fail
    assert "not upgraded" in check.summary


async def test_an_unknown_code_head_is_amber_not_green(db: AsyncSession) -> None:
    """Not knowing is never `ok`: reporting healthy because the check could not
    run is the one outcome worse than not checking (§16.9)."""
    check = await check_schema_version(db, None)

    assert check.state is CheckState.warn


async def test_a_matching_schema_is_ok(db: AsyncSession) -> None:
    stored = await stored_schema_revision(db)

    assert (await check_schema_version(db, stored)).state is CheckState.ok


async def test_the_practice_row_check_counts_one(db: AsyncSession) -> None:
    count = int((await db.execute(select(func.count()).select_from(Practice))).scalar_one())
    assert count == 1

    assert (await _one(db, "practice_row")).state is CheckState.ok


# --- The guard --------------------------------------------------------------


async def test_a_check_that_raises_reports_warn_and_does_not_stop_the_others(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16.9: one broken check may not take the status file down with it."""
    import app.core.services.status as status

    async def boom(session: AsyncSession) -> Check:
        raise RuntimeError("the query is wrong")

    boom.__name__ = "_outbox_dead"
    monkeypatch.setattr(status, "_outbox_dead", boom)

    checks = {c.id: c for c in await status.run_checks(db)}

    assert checks["outbox_dead"].state is CheckState.warn
    assert "RuntimeError" in checks["outbox_dead"].detail
    assert checks["practice_row"].state is CheckState.ok


def test_the_worst_state_wins() -> None:
    assert CheckState.worst([CheckState.ok, CheckState.ok]) is CheckState.ok
    assert CheckState.worst([CheckState.ok, CheckState.warn]) is CheckState.warn
    assert CheckState.worst([CheckState.warn, CheckState.fail]) is CheckState.fail
    assert CheckState.worst([]) is CheckState.ok
