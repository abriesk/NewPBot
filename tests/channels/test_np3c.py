"""NP3c: follow-ups from the NP1-NP4a reviews and the NP2 deploy (one test module per slice, the
item number in each docstring)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.telegram import keyboards as kb
from app.channels.telegram.router import _counter_with_slot, _propose_screen
from app.channels.web.client import _SLOT_GONE, _countered_instant
from app.core.enums import Channel, Modality, NegotiationKind, SlotStatus
from app.core.models import BookingRequest, Client, NegotiationMessage, Practice, Slot
from app.core.services import booking
from app.core.services import slots as slot_service
from app.core.services.translations import get_text

_n = 0


async def _stale_setup(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    other_request_id: int,
) -> tuple[BookingRequest, Slot]:
    """A negotiating request, a booked slot, and an `available` slot half an hour into it: the
    stale screen offered the second before the first was booked."""
    global _n
    _n += 1
    future_slot.modality = Modality.online
    overlapping = Slot(
        practice_id=practice.id,
        starts_at=future_slot.starts_at + timedelta(minutes=30),
        duration_min=60,
        modality=Modality.onsite,
        status=SlotStatus.available,
    )
    spare = Slot(
        practice_id=practice.id,
        starts_at=datetime.now(UTC) + timedelta(days=50, microseconds=_n + 900),
        duration_min=60,
        status=SlotStatus.available,
    )
    db.add_all([overlapping, spare])
    await db.flush()
    request = await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=spare.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        problem_text="x",
    )
    await booking.admin_propose(db, request.id, proposed_start=spare.starts_at)
    await slot_service.book_slot(db, future_slot.id, other_request_id)
    return request, overlapping


async def _counters(db: AsyncSession, request: BookingRequest) -> int:
    rows = (
        (
            await db.execute(
                select(NegotiationMessage).where(
                    NegotiationMessage.request_id == request.id,
                    NegotiationMessage.kind == NegotiationKind.counter,
                )
            )
        )
        .scalars()
        .all()
    )
    return len(rows)


async def test_telegram_counter_over_a_booked_slot_is_refused(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    other_request_id: int,
) -> None:
    """NP3c/2: the stale keyboard offered an hour a booking has since overlapped."""
    request, overlapping = await _stale_setup(
        db, practice, client, session_type_id, future_slot, other_request_id
    )
    reply = await _counter_with_slot(db, client, request, overlapping.id)
    assert reply.text == await get_text(db, client.language, "booking.slot.taken")
    assert await _counters(db, request) == 0


async def test_the_therapists_proposal_over_a_booked_slot_is_refused(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    other_request_id: int,
) -> None:
    request, overlapping = await _stale_setup(
        db, practice, client, session_type_id, future_slot, other_request_id
    )
    before = (
        await db.execute(
            select(NegotiationMessage.id).where(NegotiationMessage.request_id == request.id)
        )
    ).all()
    await _propose_screen(db, 1, request, kb.PROPOSE_SLOT, str(overlapping.id))
    after = (
        await db.execute(
            select(NegotiationMessage.id).where(NegotiationMessage.request_id == request.id)
        )
    ).all()
    assert len(after) == len(before)


async def test_web_counter_over_a_booked_slot_is_slot_gone(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    other_request_id: int,
) -> None:
    _, overlapping = await _stale_setup(
        db, practice, client, session_type_id, future_slot, other_request_id
    )
    assert await _countered_instant(db, str(overlapping.id), "", "UTC") is _SLOT_GONE
