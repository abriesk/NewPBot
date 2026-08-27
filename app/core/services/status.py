"""Health checks that read the database (IMPLEMENTATION.md §16.9).

The checks the domain can answer on its own: is anything undelivered, is the
worker's own work piling up, has something been throwing exceptions, is the
schema the one this code expects. The filesystem checks -- dumps, disk -- live
in the worker beside them (§14), because disk space is not a domain concept.

Two rules shape everything here (DESIGN.md §22.4). **Red means clients are
affected right now; amber means something will break.** And the therapist's own
workload is never a check: ten unanswered requests is Tuesday, not a fault, and
colouring it teaches the eye that the dot means "there is work" -- which is
exactly the meaning that makes it useless for "there is a fault".

Every check is a bounded, index-backed query, and every check that cannot run
returns `warn` rather than raising. One broken check may not take the status
file down with it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import CheckState, ErrorSource, OutboxStatus, ReminderState
from app.core.models import ErrorEvent, OutboxMessage, Practice, Reminder
from app.core.policies import now_utc

logger = logging.getLogger(__name__)

#: §16.9. Named here rather than inline so the tests and the spec can be read
#: against each other. These are not settings: a threshold the therapist can
#: raise is a threshold that gets raised the first time it is inconvenient.
DEAD_WINDOW = timedelta(days=7)
FAILING_WINDOW = timedelta(hours=1)
FAILING_WARN_AT = 3
OVERDUE_GRACE = timedelta(minutes=15)

#: How long a row may sit in `sending` before it counts as an interrupted send
#: rather than one in flight. A dispatch pass takes seconds; past this it is a
#: process that died between the claim and the transport (§14).
STUCK_GRACE = timedelta(minutes=15)
ERROR_WINDOW = timedelta(hours=1)
WEB_ERRORS_FAIL_AT = 10
WORKER_ERRORS_FAIL_AT = 3


@dataclass(frozen=True)
class Check:
    """One verdict, and the two sentences that make it actionable (§16.8).

    `summary` is for the therapist and speaks of consequences. `detail` is for
    whoever runs the server and speaks of causes -- counts, identifiers,
    timestamps, thresholds, and nothing a client wrote.
    """

    id: str
    state: CheckState
    summary: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state.value,
            "summary": self.summary,
            "detail": self.detail,
        }


def ok(check_id: str, detail: str = "") -> Check:
    return Check(check_id, CheckState.ok, "", detail)


# --- Recording exceptions ---------------------------------------------------


async def record_error(
    session: AsyncSession, *, source: ErrorSource, exc: BaseException, location: str
) -> None:
    """Write one `error_event` (§6.9).

    The exception object is taken rather than a string so that no caller can
    pass `str(exc)` by accident. Only its **class** is stored: a message can
    carry an email address or a fragment of problem text, and hard rule 8 has
    no exception for tracebacks (DESIGN.md §22.5). The logs keep the detail.
    """
    from app.core.models import Practice as _Practice

    practice_id = (
        await session.execute(select(_Practice.id).order_by(_Practice.id).limit(1))
    ).scalar_one_or_none()
    if practice_id is None:
        return

    session.add(
        ErrorEvent(
            practice_id=practice_id,
            source=source,
            kind=type(exc).__name__,
            location=location[:200],
        )
    )
    await session.flush()


def where(exc: BaseException, fallback: str) -> str:
    """`module:line` of the frame the exception came from, for `location`.

    The deepest frame is the useful one: it is where the failure actually is,
    not where it was caught.
    """
    tb = exc.__traceback__
    if tb is None:
        return fallback
    while tb.tb_next is not None:
        tb = tb.tb_next
    frame = tb.tb_frame
    module = frame.f_globals.get("__name__", fallback)
    return f"{module}:{tb.tb_lineno}"


async def run_checks(session: AsyncSession, *, code_head: str | None = None) -> list[Check]:
    """Every database-derived check in §16.9, in file order."""
    checks: list[Check] = []
    for check in (
        _outbox_dead,
        _outbox_failing,
        _outbox_stalled,
        _outbox_stuck,
        _reminders_overdue,
        _web_errors,
        _worker_errors,
        _practice_row,
    ):
        checks.append(await _guard(session, check))

    checks.append(
        await _guard(
            session,
            _named("schema_version", lambda s: check_schema_version(s, code_head)),
        )
    )
    return checks


def _named(
    name: str, check: Callable[[AsyncSession], Awaitable[Check]]
) -> Callable[[AsyncSession], Awaitable[Check]]:
    """`_guard` names a failing check after the function it called; a lambda
    would otherwise report itself as `<lambda>`."""
    check.__name__ = name
    return check


async def _guard(
    session: AsyncSession, check: Callable[[AsyncSession], Awaitable[Check]]
) -> Check:
    """§16.9: a check that cannot run reports `warn`, never `ok`, and never
    raises. Reporting `ok` because the query failed is the one outcome worse
    than not checking at all."""
    name = check.__name__.lstrip("_")
    try:
        return await check(session)
    except Exception as exc:
        logger.warning("health check %s failed to run: %s", name, type(exc).__name__)
        return Check(
            name,
            CheckState.warn,
            "One of the self-checks could not run. The service is probably fine, "
            "but mention this if you call for help.",
            f"check {name} raised {type(exc).__name__}",
        )


# --- Delivery ---------------------------------------------------------------


async def _outbox_dead(session: AsyncSession) -> Check:
    """Somebody never got something the practice promised them."""
    since = now_utc() - DEAD_WINDOW
    rows = (
        await session.execute(
            select(func.count(), func.min(OutboxMessage.created_at)).where(
                OutboxMessage.status == OutboxStatus.dead,
                OutboxMessage.created_at >= since,
            )
        )
    ).one()
    count, oldest = int(rows[0]), rows[1]

    if not count:
        return ok("outbox_dead", "no undelivered messages in 7 days")

    return Check(
        "outbox_dead",
        CheckState.fail,
        f"{count} message(s) could not be delivered. Someone has not heard back "
        f"from the practice. Open Delivery to see who, and call for help.",
        f"outbox: {count} dead since {since:%Y-%m-%dT%H:%MZ}, oldest {oldest:%Y-%m-%dT%H:%MZ}",
    )


async def _outbox_failing(session: AsyncSession) -> Check:
    """Still retrying, so nothing is lost yet -- but it is going to be."""
    since = now_utc() - FAILING_WINDOW
    count = int(
        (
            await session.execute(
                select(func.count()).where(
                    OutboxMessage.status == OutboxStatus.failed,
                    OutboxMessage.created_at >= since,
                )
            )
        ).scalar_one()
    )

    if count < FAILING_WARN_AT:
        return ok("outbox_failing", f"{count} retrying in the last hour")

    return Check(
        "outbox_failing",
        CheckState.warn,
        "Messages are being retried rather than delivered. Nothing is lost yet.",
        f"outbox: {count} failed in the last hour (warn at {FAILING_WARN_AT})",
    )


async def _outbox_stalled(session: AsyncSession) -> Check:
    """Dispatch is not running even though something is writing this file."""
    cutoff = now_utc() - OVERDUE_GRACE
    count = int(
        (
            await session.execute(
                select(func.count()).where(
                    OutboxMessage.status == OutboxStatus.pending,
                    OutboxMessage.next_attempt_at < cutoff,
                )
            )
        ).scalar_one()
    )

    if not count:
        return ok("outbox_stalled", "nothing overdue")

    return Check(
        "outbox_stalled",
        CheckState.fail,
        f"{count} message(s) should have gone out and have not. Clients are "
        f"waiting on replies the practice thinks it sent.",
        f"outbox: {count} pending past next_attempt_at by more than "
        f"{int(OVERDUE_GRACE.total_seconds() // 60)}m",
    )


async def _outbox_stuck(session: AsyncSession) -> Check:
    """A send that was claimed and never finished.

    `sending` is committed before the message reaches a transport (§14), so a
    process that dies mid-send leaves the row here rather than rolling it back
    to `pending` for the next pass to send twice. Nothing moves it out again on
    its own, and that is deliberate: whether the message actually went is
    exactly what nobody knows, and a sweep that guessed would reintroduce the
    duplicate the `sending` state exists to prevent. So it is surfaced for a
    person to decide about instead.
    """
    cutoff = now_utc() - STUCK_GRACE
    rows = (
        await session.execute(
            select(func.count(), func.min(OutboxMessage.created_at)).where(
                OutboxMessage.status == OutboxStatus.sending,
                OutboxMessage.created_at < cutoff,
            )
        )
    ).one()
    count, oldest = int(rows[0]), rows[1]

    if not count:
        return ok("outbox_stuck", "no interrupted sends")

    return Check(
        "outbox_stuck",
        CheckState.fail,
        f"{count} message(s) were interrupted while being sent. Nobody knows "
        f"whether they arrived, so nothing has been retried. Call for help.",
        f"outbox: {count} in sending for more than "
        f"{int(STUCK_GRACE.total_seconds() // 60)}m, oldest {oldest:%Y-%m-%dT%H:%MZ}",
    )


async def _reminders_overdue(session: AsyncSession) -> Check:
    cutoff = now_utc() - OVERDUE_GRACE
    count = int(
        (
            await session.execute(
                select(func.count()).where(
                    Reminder.state == ReminderState.scheduled,
                    Reminder.due_at < cutoff,
                )
            )
        ).scalar_one()
    )

    if not count:
        return ok("reminders_overdue", "nothing overdue")

    return Check(
        "reminders_overdue",
        CheckState.fail,
        f"{count} reminder(s) did not fire. Clients may arrive unprepared, or not at all.",
        f"reminder: {count} scheduled past due_at by more than "
        f"{int(OVERDUE_GRACE.total_seconds() // 60)}m",
    )


# --- Exceptions -------------------------------------------------------------


async def _errors_since(session: AsyncSession, source: ErrorSource) -> list[tuple[str, str, int]]:
    since = now_utc() - ERROR_WINDOW
    rows = (
        await session.execute(
            select(ErrorEvent.kind, ErrorEvent.location, func.count())
            .where(ErrorEvent.source == source, ErrorEvent.at >= since)
            .group_by(ErrorEvent.kind, ErrorEvent.location)
            .order_by(func.count().desc())
            .limit(5)
        )
    ).all()
    return [(kind, location, int(count)) for kind, location, count in rows]


def _error_detail(rows: list[tuple[str, str, int]]) -> str:
    return "; ".join(f"{kind} at {location} x{count}" for kind, location, count in rows)


async def _web_errors(session: AsyncSession) -> Check:
    """The gap the domain tables cannot see: a request that 500ed and a client
    who simply left (DESIGN.md §22.2)."""
    rows = await _errors_since(session, ErrorSource.web)
    total = sum(count for _, _, count in rows)

    if not total:
        return ok("web_errors", "no request errors in the last hour")

    state = CheckState.fail if total >= WEB_ERRORS_FAIL_AT else CheckState.warn
    summary = (
        "The website is throwing errors. Some clients are seeing a broken page instead "
        "of a booking form."
        if state is CheckState.fail
        else "The website hit an error. One visitor may have seen a broken page."
    )
    detail = f"web: {total} in the last hour"
    return Check("web_errors", state, summary, f"{detail} — {_error_detail(rows)}")


async def _worker_errors(session: AsyncSession) -> Check:
    """A job failing behind §14's per-job `except` -- which is correct, and
    which is exactly why it can fail for a month unnoticed."""
    rows = await _errors_since(session, ErrorSource.worker)
    total = sum(count for _, _, count in rows)

    if not total:
        return ok("worker_errors", "no background errors in the last hour")

    worst = max(count for _, _, count in rows)
    state = CheckState.fail if worst >= WORKER_ERRORS_FAIL_AT else CheckState.warn
    summary = (
        "A background task keeps failing. Reminders, cleanup, or backups may not be running."
        if state is CheckState.fail
        else "A background task failed once and will be retried."
    )
    return Check(
        "worker_errors", state, summary, f"worker: {total} in the last hour — {_error_detail(rows)}"
    )


# --- The database itself ----------------------------------------------------


async def stored_schema_revision(session: AsyncSession) -> str | None:
    """What the database says it has been migrated to."""
    return (
        await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
    ).scalar_one_or_none()


async def check_schema_version(session: AsyncSession, code_head: str | None) -> Check:
    """A half-applied upgrade: the code expects columns the database does not
    have, and the failure surfaces somewhere unrelated, hours later.

    `code_head` is passed in rather than read here. Finding it means asking
    Alembic to read `alembic/versions`, which is deployment machinery, not a
    domain concept -- so the caller in `app/worker/jobs/` does that part.
    """
    stored = await stored_schema_revision(session)

    if code_head is None or stored is None:
        return Check(
            "schema_version",
            CheckState.warn,
            "The self-check could not read the database version.",
            f"alembic: code head {code_head!r}, database {stored!r}",
        )
    if code_head != stored:
        return Check(
            "schema_version",
            CheckState.fail,
            "The database was not upgraded to match this version of the software. "
            "Do not make changes; call for help.",
            f"alembic: code head {code_head}, database {stored}",
        )
    return ok("schema_version", f"alembic at {stored}")


async def _practice_row(session: AsyncSession) -> Check:
    """Exactly one practice is served (§6.1). Zero means the seed never ran or
    a restore went in half; more than one means something invented a tenant."""
    count = int((await session.execute(select(func.count()).select_from(Practice))).scalar_one())

    if count == 1:
        return ok("practice_row", "one practice row")

    return Check(
        "practice_row",
        CheckState.fail,
        "The practice's own settings are missing or duplicated. Call for help "
        "before changing anything.",
        f"practice: {count} rows, expected 1",
    )


__all__ = [
    "Check",
    "CheckState",
    "check_schema_version",
    "record_error",
    "run_checks",
    "stored_schema_revision",
    "where",
]
