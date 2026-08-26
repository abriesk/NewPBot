"""Outbox dispatch transaction boundaries (IMPLEMENTATION.md §14).

The claim has to reach the database before anything reaches a transport. Testing
that needs committed rows and a second connection -- the rollback session the
rest of the outbox tests use cannot see across a transaction boundary, which is
the only thing this file is about.

The row is parked with `next_attempt_at` in the future so the worker container
running against this same database cannot claim it out from under the test; the
claim under test is invoked directly instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.channels.base import DeliveryResult
from app.config import get_settings
from app.core.enums import Channel, OutboxStatus
from app.core.models import OutboxAttempt, OutboxMessage, Practice
from app.db import dispose_engine, unit_of_work
from app.worker.jobs import outbox as job


@pytest_asyncio.fixture(autouse=True)
async def _dispose_shared_engine() -> AsyncIterator[None]:
    """This is the only test that drives the *module-level* engine, which the
    rest of the suite never touches -- `conftest.py` builds a NullPool engine
    per test precisely because pytest-asyncio gives each test its own loop.
    Left cached, its connections outlive this loop and the next test to open a
    TestClient fails with "attached to a different loop".
    """
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def committed() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def parked_row(committed: AsyncSession) -> AsyncIterator[OutboxMessage]:
    """One committed outbox row the live worker will not touch."""
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    row = OutboxMessage(
        practice_id=practice.id,
        channel=Channel.telegram,
        address="100200300",
        intent_key="request.expired.client",
        payload={"uuid": "11111111-1111-1111-1111-111111111111"},
        locale="en",
        status=OutboxStatus.pending,
        next_attempt_at=datetime.now(UTC) + timedelta(days=1),
    )
    committed.add(row)
    await committed.commit()

    yield row

    await committed.execute(delete(OutboxAttempt).where(OutboxAttempt.message_id == row.id))
    await committed.execute(delete(OutboxMessage).where(OutboxMessage.id == row.id))
    await committed.commit()


async def test_the_claim_is_committed_before_anything_is_sent(
    committed: AsyncSession, parked_row: OutboxMessage, monkeypatch: object
) -> None:
    """§14. Claiming and sending in one transaction means a crash after a
    successful send rolls the row back to `pending`, and the next pass sends it
    a second time. `dedupe_key` does not help there -- it dedupes what goes
    *into* the outbox, not what leaves it.

    The proof is what a separate connection can see at the moment of the send.
    """
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)

    seen: list[OutboxStatus] = []

    async def only_our_row(session: AsyncSession, limit: int = 0) -> list[OutboxMessage]:
        row = (
            await session.execute(
                select(OutboxMessage).where(OutboxMessage.id == parked_row.id).with_for_update()
            )
        ).scalar_one()
        row.status = OutboxStatus.sending
        await session.flush()
        return [row]

    async def watching_deliver(
        session: AsyncSession, message: OutboxMessage, transports: object
    ) -> DeliveryResult:
        # A *different* connection: it can only see what has been committed.
        async with unit_of_work() as other:
            seen.append(
                (
                    await other.execute(
                        select(OutboxMessage.status).where(OutboxMessage.id == message.id)
                    )
                ).scalar_one()
            )
        return DeliveryResult.success()

    monkeypatch.setattr(job, "claim_batch", only_our_row)
    monkeypatch.setattr(job, "deliver_one", watching_deliver)

    attempted = await job.dispatch_outbox(transports={})

    assert attempted == 1
    assert seen == [OutboxStatus.sending], (
        f"the send saw the row as {seen}: the claim had not been committed yet"
    )

    await committed.refresh(parked_row)
    assert parked_row.status is OutboxStatus.sent
