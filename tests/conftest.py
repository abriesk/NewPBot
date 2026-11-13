"""Test-wide fixtures.

Required configuration is injected before anything imports app.config, so tests
never depend on a developer's local .env.

Database tests run against real PostgreSQL. SQLite MUST NOT be substituted --
the schema depends on native enums, arrays, partial indexes, NULLS NOT
DISTINCT, and FOR UPDATE SKIP LOCKED (IMPLEMENTATION.md §18).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

#: Filled in only when the environment does not already say otherwise, so the
#: container's DATABASE_URL (db:5432) wins over this localhost placeholder.
TEST_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://psycho:psycho@localhost:5432/psychobooking_test",
    "SECRET_KEY": "test-secret-key-that-is-at-least-32-bytes-long",
    "BASE_URL": "https://example.test",
    "TELEGRAM_BOT_TOKEN": "0000000000:test-token",
    "TELEGRAM_BOT_USERNAME": "test_bot",
    "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "test-admin-password",
}

#: Overridden unconditionally. A deployment's .env legitimately leaves
#: TELEGRAM_ADMIN_IDS empty, but then there is no admin to notify and §13.3's
#: routing cannot be exercised at all.
FORCED_ENV = {
    "TELEGRAM_ADMIN_IDS": "1,2",
}


def refuse_a_database_that_is_not_a_test_one(url: str) -> None:
    """B1: end the session unless `url` names a database ending in `_test`.

    The suite commits, deletes and rewrites rows, and the end-to-end tests run
    against a database whose worker may be live. Pointed at the deployment's
    own database (as `docker compose exec web pytest` is, by default) it used to
    start without a word and fail on -- or damage -- real rows. The name is the
    only thing checked on purpose: it is what a person typed, and the one thing
    that cannot be mistaken for the deployment's.
    """
    from sqlalchemy.engine import make_url

    name = make_url(url).database or ""
    if not name.endswith("_test"):
        pytest.exit(
            f"refusing to run: DATABASE_URL names the database {name!r}, which does not end in "
            "'_test'. The suite writes to its database; point it at a separate one, e.g. "
            "DATABASE_URL=postgresql+asyncpg://user:pass@db:5432/psychobooking_test "
            "(CLAUDE.md, Commands).",
            returncode=2,
        )


for _key, _value in TEST_ENV.items():
    os.environ.setdefault(_key, _value)
for _key, _value in FORCED_ENV.items():
    os.environ[_key] = _value

refuse_a_database_that_is_not_a_test_one(os.environ["DATABASE_URL"])


from sqlalchemy import NullPool  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402


@pytest.fixture
def email_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn the email channel on for one test.

    `get_settings` is lru_cached and every module calls it at runtime rather
    than binding the result, so clearing the cache around an env change is
    enough to switch the channel on and back off again.
    """
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_FROM", "no-reply@example.test")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def email_disabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """§4: with SMTP_HOST unset the channel is disabled cleanly."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """A session whose work is rolled back at the end of the test.

    Everything runs inside one outer transaction that is never committed, so
    tests see the migrated schema and the seeded rows without leaving anything
    behind for the next test.

    The engine is built per test with NullPool rather than reusing the module
    level one: pytest-asyncio gives each test its own event loop, and an asyncpg
    connection pooled across loops fails on reuse.
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


# --- Shared domain fixtures -------------------------------------------------
# At the tests/ root rather than tests/core/ so the channel tests can use them
# too. They import only core services, so tests/core stays free of channel
# imports (IMPLEMENTATION.md §3).

from collections.abc import AsyncIterator as _AsyncIterator  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

from app.core.enums import Channel, Modality, RequestStatus, SlotStatus  # noqa: E402
from app.core.models import (  # noqa: E402
    BookingRequest,
    Client,
    Practice,
    SessionType,
    Slot,
)
from app.core.services.clients import resolve_client  # noqa: E402
from app.core.services.settings import get_practice  # noqa: E402


@pytest_asyncio.fixture
async def practice(db: AsyncSession) -> Practice:
    return await get_practice(db)


@pytest_asyncio.fixture
async def session_type_id(db: AsyncSession) -> int:
    from sqlalchemy import select

    return int(
        (await db.execute(select(SessionType.id).order_by(SessionType.id).limit(1))).scalar_one()
    )


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> Client:
    """A Telegram-identified client. Telegram vouches for the id, so the
    identity is verified by construction (DESIGN.md §5.1)."""
    return await resolve_client(db, Channel.telegram, "100200300", verified=True)


async def _make_request(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> int:
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
    return int(request.id)


@pytest_asyncio.fixture
async def request_id(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> int:
    """A real pending request, holding no slot.

    The slot tests need one because `slot.held_by_request` is a foreign key --
    a fabricated id is rejected by the database, as it should be.
    """
    return await _make_request(db, practice, client, session_type_id)


@pytest_asyncio.fixture
async def other_request_id(
    db: AsyncSession, practice: Practice, client: Client, session_type_id: int
) -> int:
    """A second request, for the "someone else holds it" cases."""
    return await _make_request(db, practice, client, session_type_id)


_slot_offset = 0


@pytest_asyncio.fixture
async def future_slot(db: AsyncSession, practice: Practice) -> _AsyncIterator[Slot]:
    """One available slot, comfortably in the future.

    Each test gets a distinct instant so the NULLS NOT DISTINCT unique index
    (§6.4) does not make tests collide with each other.
    """
    global _slot_offset
    _slot_offset += 1
    slot = Slot(
        practice_id=practice.id,
        starts_at=datetime.now(UTC) + timedelta(days=7, microseconds=_slot_offset),
        duration_min=60,
        status=SlotStatus.available,
    )
    db.add(slot)
    await db.flush()
    yield slot


@pytest.fixture(autouse=True)
def _fresh_rate_limits() -> Iterator[None]:
    """Every test starts with an empty rate-limit window.

    The limiter is process-global by design (§17, and Redis is forbidden), so
    without this the admin suite exhausts its own 5-per-15-minutes allowance
    part-way through and every later sign-in gets a 429. That is the limiter
    working; it just makes a shared test process an unrealistic caller.
    """
    from app.channels.web import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()


def pytest_sessionstart(session: pytest.Session) -> None:
    """A practice is now created with the captcha gate on (NP2). The suite's
    form tests post without solving a challenge and the tests that want the gate
    switch it on themselves, so the baseline is the old one: gate off. Only the
    `_test` database is touched (checked above)."""
    import asyncio

    from sqlalchemy import NullPool, text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _off() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE practice SET captcha_on = false"))
        finally:
            await engine.dispose()

    asyncio.run(_off())
