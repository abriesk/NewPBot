"""Outbox dispatch, retry, and dedupe (IMPLEMENTATION.md §14, §18).

§18 requires three things of this job specifically: transient failures retry
with backoff, permanent failures do not retry, and a restart mid-send does not
duplicate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import DeliveryResult, RenderedMessage
from app.core.enums import Channel, OutboxStatus
from app.core.models import OutboxAttempt, OutboxMessage, Practice
from app.worker.jobs.outbox import (
    MAX_ATTEMPTS,
    backoff_minutes,
    claim_batch,
    deliver_one,
    record_outcome,
)


class FakeTransport:
    """A transport that returns whatever the test tells it to."""

    def __init__(self, channel: Channel, result: DeliveryResult) -> None:
        self.channel = channel
        self.result = result
        self.sent: list[tuple[str, RenderedMessage]] = []

    async def send(self, address: str, message: RenderedMessage) -> DeliveryResult:
        self.sent.append((address, message))
        return self.result


async def _row(db: AsyncSession, practice: Practice, **overrides: object) -> OutboxMessage:
    values: dict[str, object] = {
        "practice_id": practice.id,
        "channel": Channel.telegram,
        "address": "100200300",
        "intent_key": "request.expired.client",
        "payload": {"uuid": "11111111-1111-1111-1111-111111111111"},
        "locale": "en",
        "status": OutboxStatus.pending,
    }
    values.update(overrides)
    row = OutboxMessage(**values)
    db.add(row)
    await db.flush()
    return row


# --- Backoff (§14) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [(1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (6, 60), (10, 60)],
)
def test_backoff_doubles_and_caps_at_an_hour(attempts: int, expected: int) -> None:
    """§14: min(2^attempts, 60) minutes."""
    assert backoff_minutes(attempts) == expected


# --- Transient failures retry -----------------------------------------------


async def test_a_transient_failure_stays_pending_and_backs_off(
    db: AsyncSession, practice: Practice
) -> None:
    row = await _row(db, practice)
    before = datetime.now(UTC)

    await record_outcome(db, row, DeliveryResult.transient("timeout"))

    assert row.status is OutboxStatus.pending
    assert row.attempts == 1
    assert row.last_error == "timeout"
    assert row.next_attempt_at >= before + timedelta(minutes=backoff_minutes(1) - 1)


async def test_a_transient_failure_dies_after_six_attempts(
    db: AsyncSession, practice: Practice
) -> None:
    row = await _row(db, practice)

    for _ in range(MAX_ATTEMPTS):
        await record_outcome(db, row, DeliveryResult.transient("timeout"))

    assert row.attempts == MAX_ATTEMPTS
    assert row.status is OutboxStatus.dead


async def test_giving_up_alerts_the_therapist(db: AsyncSession, practice: Practice) -> None:
    """§14: after the final attempt she is told on her own channel."""
    row = await _row(db, practice)
    for _ in range(MAX_ATTEMPTS):
        await record_outcome(db, row, DeliveryResult.transient("timeout"))

    alerts = (
        (
            await db.execute(
                select(OutboxMessage).where(
                    OutboxMessage.intent_key == "system.delivery_failed.admin"
                )
            )
        )
        .scalars()
        .all()
    )
    assert alerts


async def _alerts_after(db: AsyncSession, row: OutboxMessage) -> list[OutboxMessage]:
    """Delivery-failure alerts raised *by this test*.

    Scoped by id rather than counting every such row in the table: the suite
    shares a database with the running deployment, whose worker writes real
    alerts of its own for anything it cannot deliver. Without the bound, this
    passes on a clean database and fails the first time the worker has had a
    bad day -- which is not what either assertion is about.
    """
    return list(
        (
            await db.execute(
                select(OutboxMessage).where(
                    OutboxMessage.intent_key == "system.delivery_failed.admin",
                    OutboxMessage.id > row.id,
                )
            )
        )
        .scalars()
        .all()
    )


# --- Permanent failures do not retry ----------------------------------------


async def test_a_permanent_failure_dies_immediately(db: AsyncSession, practice: Practice) -> None:
    """A blocked bot is not a timeout. Retrying it six times helps nobody."""
    row = await _row(db, practice)

    await record_outcome(db, row, DeliveryResult.permanent("bot was blocked by the user"))

    assert row.status is OutboxStatus.dead
    assert row.attempts == 1


async def test_a_first_attempt_permanent_failure_alerts(
    db: AsyncSession, practice: Practice
) -> None:
    """§14: no alert unless attempts = 0 at the time of the attempt -- so a
    permanent failure on the very first try is news worth telling her."""
    row = await _row(db, practice)
    await record_outcome(db, row, DeliveryResult.permanent("blocked"))

    assert await _alerts_after(db, row)


async def test_a_permanent_failure_after_retries_does_not_alert(
    db: AsyncSession, practice: Practice
) -> None:
    """It is usually a condition she already knows about."""
    row = await _row(db, practice)
    await record_outcome(db, row, DeliveryResult.transient("timeout"))
    row.status = OutboxStatus.sending

    await record_outcome(db, row, DeliveryResult.permanent("blocked"))

    assert row.status is OutboxStatus.dead
    assert not await _alerts_after(db, row)


# --- Success ----------------------------------------------------------------


async def test_success_marks_sent_and_stamps_the_time(db: AsyncSession, practice: Practice) -> None:
    row = await _row(db, practice)
    await record_outcome(db, row, DeliveryResult.success())

    assert row.status is OutboxStatus.sent
    assert row.sent_at is not None


async def test_every_attempt_is_logged(db: AsyncSession, practice: Practice) -> None:
    """The delivery log answers "did she get my message?" (DESIGN.md §12)."""
    row = await _row(db, practice)
    await record_outcome(db, row, DeliveryResult.transient("timeout"))
    row.status = OutboxStatus.sending
    await record_outcome(db, row, DeliveryResult.success())

    attempts = (
        (await db.execute(select(OutboxAttempt).where(OutboxAttempt.message_id == row.id)))
        .scalars()
        .all()
    )
    assert [a.ok for a in attempts] == [False, True]


# --- Claiming ---------------------------------------------------------------


async def test_claiming_takes_only_due_rows(db: AsyncSession, practice: Practice) -> None:
    due = await _row(db, practice, address="due")
    await _row(
        db,
        practice,
        address="later",
        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await _row(db, practice, address="sent", status=OutboxStatus.sent)

    claimed = await claim_batch(db)
    assert [row.id for row in claimed] == [due.id]


async def test_claiming_moves_rows_to_sending(db: AsyncSession, practice: Practice) -> None:
    """A crash between claiming and sending leaves the row visibly stuck rather
    than silently re-sent."""
    await _row(db, practice)
    claimed = await claim_batch(db)
    assert all(row.status is OutboxStatus.sending for row in claimed)


# --- Dedupe (§18: a restart mid-send does not duplicate) --------------------


async def test_the_dedupe_key_is_unique(db: AsyncSession, practice: Practice) -> None:
    from sqlalchemy.exc import IntegrityError

    await _row(db, practice, dedupe_key="reminder:1:telegram:100200300")
    with pytest.raises(IntegrityError):
        await _row(db, practice, dedupe_key="reminder:1:telegram:100200300")


# --- Rendering --------------------------------------------------------------


async def test_a_row_with_no_transport_dies_with_a_clear_reason(
    db: AsyncSession, practice: Practice
) -> None:
    row = await _row(db, practice, channel=Channel.email, address="a@example.test")
    result = await deliver_one(db, row, {})

    assert not result.ok
    assert result.permanent_failure
    assert "no transport" in (result.error or "")


async def test_an_unknown_intent_is_a_permanent_failure(
    db: AsyncSession, practice: Practice
) -> None:
    """A missing intent spec is a bug, not something backoff will fix."""
    row = await _row(db, practice, intent_key="no.such.intent")
    transport = FakeTransport(Channel.telegram, DeliveryResult.success())

    result = await deliver_one(db, row, {Channel.telegram: transport})

    assert result.permanent_failure
    assert transport.sent == []


async def test_a_rendered_message_reaches_the_transport(
    db: AsyncSession, practice: Practice
) -> None:
    row = await _row(db, practice)
    transport = FakeTransport(Channel.telegram, DeliveryResult.success())

    result = await deliver_one(db, row, {Channel.telegram: transport})

    assert result.ok
    assert transport.sent
    address, message = transport.sent[0]
    assert address == "100200300"
    assert message.parts and message.parts[0].strip()
    # Hard rule 6: HTML, never the MarkdownV2 parse mode.
    assert message.parse_mode == "HTML"
