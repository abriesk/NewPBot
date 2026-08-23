"""Test-wide fixtures.

Required configuration is injected before anything imports app.config, so tests
never depend on a developer's local .env.

Database tests run against real PostgreSQL. SQLite MUST NOT be substituted --
the schema depends on native enums, arrays, partial indexes, NULLS NOT
DISTINCT, and FOR UPDATE SKIP LOCKED (IMPLEMENTATION.md §18).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio

TEST_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://psycho:psycho@localhost:5432/psychobooking_test",
    "SECRET_KEY": "test-secret-key-that-is-at-least-32-bytes-long",
    "BASE_URL": "https://example.test",
    "TELEGRAM_BOT_TOKEN": "0000000000:test-token",
    "TELEGRAM_BOT_USERNAME": "test_bot",
    "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
    "TELEGRAM_ADMIN_IDS": "1,2",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "test-admin-password",
}

for _key, _value in TEST_ENV.items():
    os.environ.setdefault(_key, _value)


from sqlalchemy import NullPool  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402


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
