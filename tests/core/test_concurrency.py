"""Two sessions, one row: the transitions that must serialise (§7.1, §6.2; NP1 F6, F17).

These commit like production -- two real connections, so each test cleans up
after itself rather than relying on the rollback fixture.

Ordering is never a sleep. Session A does its work and keeps its transaction
open; session B is started and observed to be *waiting on A* (a `Lock` wait in
`pg_stat_activity`) before A commits. Whatever B does after that is decided by
the database, which is the thing under test.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta


import pytest_asyncio
from sqlalchemy import NullPool, delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.config import get_settings
from app.core.enums import (
    Channel,
    Modality,
    NegotiationKind,
    RequestStatus,
    SenderType,
    TokenPurpose,
)
from app.core.errors import InvalidTransition, TokenInvalid
from app.core.models import (
    AuditLog,
    AuthToken,
    BookingRequest,
    Client,
    Identity,
    NegotiationMessage,
    SessionType,
)
from app.core.services import booking
from app.core.services.clients import _hash, consume_token, issue_token, resolve_client
from app.core.services.settings import get_practice

PROBE = "np1-concurrency-probe"


@pytest_asyncio.fixture
async def engines() -> AsyncIterator[tuple[AsyncEngine, AsyncEngine, AsyncEngine]]:
    """Three independent connections: A, B, and one only watching."""
    url = get_settings().database_url
    trio = tuple(create_async_engine(url, poolclass=NullPool) for _ in range(3))
    try:
        yield trio  # type: ignore[misc]
    finally:
        for engine in trio:
            await engine.dispose()


async def _wait_until_blocked(
    engine: AsyncEngine, task: asyncio.Task[object], holder: AsyncSession
) -> None:
    """Return once a backend is waiting on a lock **held by `holder`'s
    connection**, or the task ended.

    Filtered on the holder through `pg_blocking_pids`, so a lock wait by some
    other backend in the same database cannot release A's commit early. A poll
    on the database's own state, bounded; not a delay standing in for ordering.
    If B never blocks (no lock at all) it finishes and we return too, and the
    assertions that follow say what that meant.
    """
    holder_pid = (await holder.execute(text("select pg_backend_pid()"))).scalar_one()
    deadline = asyncio.get_running_loop().time() + 15
    async with engine.connect() as watch:
        while not task.done() and asyncio.get_running_loop().time() < deadline:
            waiting = (
                await watch.execute(
                    text(
                        "select count(*) from pg_stat_activity "
                        "where datname = current_database() "
                        "and :holder = any(pg_blocking_pids(pid))"
                    ),
                    {"holder": holder_pid},
                )
            ).scalar_one()
            if waiting:
                return
            await asyncio.sleep(0.02)


async def _drop_probe(engine: AsyncEngine, request_uuid: str | None) -> None:
    async with engine.begin() as conn:
        client_ids = (
            (await conn.execute(select(Identity.client_id).where(Identity.external_id == PROBE)))
            .scalars()
            .all()
        )
        if request_uuid is not None:
            await conn.execute(delete(AuditLog).where(AuditLog.entity_id == request_uuid))
        if client_ids:
            await conn.execute(
                delete(BookingRequest).where(BookingRequest.client_id.in_(client_ids))
            )
            await conn.execute(delete(Identity).where(Identity.client_id.in_(client_ids)))
            await conn.execute(delete(Client).where(Client.id.in_(client_ids)))


async def test_reject_and_accept_on_one_request_exactly_one_wins(
    engines: tuple[AsyncEngine, AsyncEngine, AsyncEngine],
) -> None:
    """F6: `_get` reads the request FOR UPDATE, so the second transition sees
    the first one's outcome instead of passing the same status check."""
    engine_a, engine_b, engine_watch = engines
    request_id: int | None = None
    request_uuid: str | None = None
    try:
        async with AsyncSession(engine_a, expire_on_commit=False) as setup:
            practice = await get_practice(setup)
            session_type_id = (
                await setup.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
            ).scalar_one()
            person = await resolve_client(setup, Channel.telegram, PROBE)
            request = BookingRequest(
                practice_id=practice.id,
                client_id=person.id,
                session_type_id=session_type_id,
                modality=Modality.online,
                status=RequestStatus.negotiating,
                source_channel=Channel.web,
            )
            setup.add(request)
            await setup.flush()
            setup.add(
                NegotiationMessage(
                    request_id=request.id,
                    sender=SenderType.admin,
                    kind=NegotiationKind.proposal,
                    proposed_start=datetime.now(UTC) + timedelta(days=800),
                )
            )
            await setup.commit()
            request_id, request_uuid = int(request.id), str(request.uuid)

        async def accept() -> str:
            async with AsyncSession(engine_b, expire_on_commit=False) as session:
                try:
                    await booking.client_accept(session, request_id)
                    await session.commit()
                    return "accepted"
                except InvalidTransition:
                    await session.rollback()
                    return "refused"

        async with AsyncSession(engine_a, expire_on_commit=False) as session_a:
            await booking.admin_reject(session_a, request_id)
            task_b = asyncio.create_task(accept())
            await _wait_until_blocked(engine_watch, task_b, session_a)
            await session_a.commit()
        outcome_b = await asyncio.wait_for(task_b, 15)

        async with AsyncSession(engine_watch) as check:
            final = (
                await check.execute(
                    select(BookingRequest.status).where(BookingRequest.id == request_id)
                )
            ).scalar_one()
        assert outcome_b == "refused", "the accept passed a status check the reject had failed"
        assert final is RequestStatus.rejected
    finally:
        await _drop_probe(engine_watch, request_uuid)


async def test_one_token_consumed_twice_at_once_succeeds_once(
    engines: tuple[AsyncEngine, AsyncEngine, AsyncEngine],
) -> None:
    """F17: the check and the spend are one UPDATE ... WHERE used_at IS NULL."""
    engine_a, engine_b, engine_watch = engines
    raw: str | None = None
    try:
        async with AsyncSession(engine_a) as setup:
            raw = await issue_token(setup, TokenPurpose.link_channel, payload={"np1": "probe"})
            await setup.commit()

        async def consume_in_b() -> str:
            async with AsyncSession(engine_b) as session:
                try:
                    await consume_token(session, raw, TokenPurpose.link_channel)
                    await session.commit()
                    return "spent"
                except TokenInvalid:
                    await session.rollback()
                    return "refused"

        async with AsyncSession(engine_a) as session_a:
            await consume_token(session_a, raw, TokenPurpose.link_channel)
            task_b = asyncio.create_task(consume_in_b())
            await _wait_until_blocked(engine_watch, task_b, session_a)
            await session_a.commit()
        outcome_b = await asyncio.wait_for(task_b, 15)

        assert outcome_b == "refused", "the same token was spent twice"
    finally:
        if raw is not None:
            async with engine_watch.begin() as conn:
                await conn.execute(delete(AuthToken).where(AuthToken.token_hash == _hash(raw)))


