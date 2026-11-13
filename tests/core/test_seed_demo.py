"""scripts/seed_demo.py (np-shot.sh's data): every status, enough free slots, a waitlist entry, a block, a priced type.

Covers IMPLEMENTATION.md §6.4 (the tables it fills) and refuses databases not named *_test (CLAUDE.md, Commands).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.enums import RequestStatus, SlotStatus
from app.core.models import (
    BookingRequest,
    ContentBlock,
    NegotiationMessage,
    SessionType,
    Slot,
    WaitlistEntry,
)
from scripts import seed_demo


@pytest.mark.asyncio
async def test_demo_seed_covers_what_the_screenshots_need(db) -> None:
    assert await seed_demo.seed(db) is True
    assert await seed_demo.seed(db) is False  # idempotent
    rows = (await db.execute(select(BookingRequest))).scalars().all()
    assert {r.status for r in rows} == set(RequestStatus)
    assert all(r.scheduled_start is not None and r.client_id for r in rows)
    free = await db.scalar(select(func.count()).select_from(Slot).where(Slot.status == SlotStatus.available))
    assert free >= 30
    assert (await db.scalar(select(func.count()).select_from(WaitlistEntry))) >= 1
    assert (await db.scalar(select(func.count()).select_from(ContentBlock))) >= 1
    assert (await db.scalar(select(func.count()).select_from(NegotiationMessage))) >= 3
    priced = await db.scalar(select(func.count()).select_from(SessionType).where(SessionType.price_amount_minor.is_not(None)))
    assert priced == 1


def test_demo_seed_refuses_a_database_not_named_test(monkeypatch) -> None:
    monkeypatch.setattr(seed_demo, "get_settings", lambda: type("S", (), {"database_url": "postgresql+asyncpg://u:p@h/psychobooking"})())
    with pytest.raises(SystemExit):
        seed_demo.refuse_unless_test_database()
