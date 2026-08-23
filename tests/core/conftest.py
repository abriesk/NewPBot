"""Fixtures for the core domain tests.

No channel imports anywhere under tests/core (IMPLEMENTATION.md §3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel, Modality, RequestStatus, SlotStatus
from app.core.models import BookingRequest, Client, Practice, SessionType, Slot
from app.core.services.clients import resolve_client
from app.core.services.settings import get_practice


@pytest_asyncio.fixture
async def practice(db: AsyncSession) -> Practice:
    return await get_practice(db)


@pytest_asyncio.fixture
async def session_type_id(db: AsyncSession) -> int:
    return int(
        (await db.execute(select(SessionType.id).order_by(SessionType.id).limit(1))).scalar_one()
    )


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> Client:
    """A Telegram-identified client. Telegram vouches for the id, so the
    identity is verified by construction (DESIGN.md §5.1)."""
    return await resolve_client(db, Channel.telegram, "100200300", verified=True)


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
async def future_slot(db: AsyncSession, practice: Practice) -> AsyncIterator[Slot]:
    """One available slot, comfortably in the future.

    Each test gets a distinct instant so the NULLS NOT DISTINCT unique index
    (§6.4) does not make tests collide with each other.
    """
    starts_at = datetime.now(UTC) + timedelta(days=7, microseconds=_next_offset())
    slot = Slot(
        practice_id=practice.id,
        starts_at=starts_at,
        duration_min=60,
        status=SlotStatus.available,
    )
    db.add(slot)
    await db.flush()
    yield slot


_counter = 0


def _next_offset() -> int:
    global _counter
    _counter += 1
    return _counter
