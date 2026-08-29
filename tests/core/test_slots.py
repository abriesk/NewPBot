"""Slot state machine (IMPLEMENTATION.md §7.2) and the concurrency test (§18).

Every transition in the §7.2 table, and every move that is not in it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import NullPool, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.core.enums import (
    Channel,
    Modality,
    NegotiationKind,
    RequestStatus,
    SenderType,
    SlotStatus,
)
from app.core.errors import InvalidTransition, SlotInThePast, SlotReferenced, SlotUnavailable
from app.core.models import (
    AuditLog,
    BookingRequest,
    Client,
    Identity,
    NegotiationMessage,
    OutboxMessage,
    Practice,
    Reminder,
    SessionType,
    Slot,
)
from app.core.services import booking
from app.core.services import slots as slot_service
from app.core.services.clients import resolve_client
from app.core.services.settings import get_practice
from app.core.services.slots import SlotPattern


async def _delete_probe(conn: object, external_id: str) -> None:
    """Remove a committed probe identity **and the client behind it**.

    The two concurrency tests below commit like production, so the rollback
    fixture does not cover them. Deleting the identity alone left a `client`
    row with no way to reach it -- junk on a shared database, and exactly what
    §16.9's `unreachable_clients` counts.
    """
    client_ids = (
        await conn.execute(  # type: ignore[attr-defined]
            select(Identity.client_id).where(Identity.external_id == external_id)
        )
    ).scalars().all()
    await conn.execute(  # type: ignore[attr-defined]
        Identity.__table__.delete().where(Identity.__table__.c.external_id == external_id)
    )
    if client_ids:
        await conn.execute(  # type: ignore[attr-defined]
            Client.__table__.delete().where(Client.__table__.c.id.in_(client_ids))
        )


async def test_hold_moves_available_to_held(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    slot = await slot_service.hold_slot(db, future_slot.id, request_id=request_id)
    assert slot.status is SlotStatus.held
    assert slot.held_by_request == request_id
    assert slot.hold_expires_at is not None


async def test_holding_an_already_held_slot_is_refused(
    db: AsyncSession, future_slot: Slot, request_id: int, other_request_id: int
) -> None:
    await slot_service.hold_slot(db, future_slot.id, request_id=request_id)
    with pytest.raises(SlotUnavailable):
        await slot_service.hold_slot(db, future_slot.id, request_id=other_request_id)


async def test_a_slot_in_the_past_cannot_be_held(
    db: AsyncSession, practice: Practice, request_id: int
) -> None:
    """§7.2 guards on starts_at > now()."""
    past = Slot(
        practice_id=practice.id,
        starts_at=datetime.now(UTC) - timedelta(hours=1),
        status=SlotStatus.available,
    )
    db.add(past)
    await db.flush()

    with pytest.raises(SlotInThePast):
        await slot_service.hold_slot(db, past.id, request_id=request_id)


async def test_release_returns_a_held_slot_to_available(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    await slot_service.hold_slot(db, future_slot.id, request_id=request_id)
    slot = await slot_service.release_slot(db, future_slot.id)
    assert slot.status is SlotStatus.available
    assert slot.held_by_request is None
    assert slot.hold_expires_at is None


async def test_book_from_held_requires_the_hold_to_belong_to_the_request(
    db: AsyncSession, future_slot: Slot, request_id: int, other_request_id: int
) -> None:
    """Otherwise approving one request steals a slot another client is still
    filling in a form for."""
    await slot_service.hold_slot(db, future_slot.id, request_id=request_id)

    with pytest.raises(SlotUnavailable):
        await slot_service.book_slot(db, future_slot.id, request_id=other_request_id)

    slot = await slot_service.book_slot(db, future_slot.id, request_id=request_id)
    assert slot.status is SlotStatus.booked
    assert slot.booked_request == request_id
    assert slot.held_by_request is None


async def test_book_direct_from_available(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    """The admin approving a free-text request onto a free slot (§7.2)."""
    slot = await slot_service.book_slot(db, future_slot.id, request_id=request_id)
    assert slot.status is SlotStatus.booked
    assert slot.booked_request == request_id


async def test_release_returns_a_booked_slot_to_available(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    await slot_service.book_slot(db, future_slot.id, request_id=request_id)
    slot = await slot_service.release_slot(db, future_slot.id)
    assert slot.status is SlotStatus.available
    assert slot.booked_request is None


async def test_releasing_an_available_slot_is_a_no_op(db: AsyncSession, future_slot: Slot) -> None:
    slot = await slot_service.release_slot(db, future_slot.id)
    assert slot.status is SlotStatus.available


async def test_block_and_unblock(db: AsyncSession, future_slot: Slot) -> None:
    blocked = await slot_service.block_slot(db, future_slot.id)
    assert blocked.status is SlotStatus.blocked

    available = await slot_service.unblock_slot(db, future_slot.id)
    assert available.status is SlotStatus.available


async def test_a_held_slot_cannot_be_blocked(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    await slot_service.hold_slot(db, future_slot.id, request_id=request_id)
    with pytest.raises(InvalidTransition):
        await slot_service.block_slot(db, future_slot.id)


async def test_an_available_slot_cannot_be_unblocked(db: AsyncSession, future_slot: Slot) -> None:
    with pytest.raises(InvalidTransition):
        await slot_service.unblock_slot(db, future_slot.id)


async def test_a_blocked_slot_cannot_be_held(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    await slot_service.block_slot(db, future_slot.id)
    with pytest.raises(SlotUnavailable):
        await slot_service.hold_slot(db, future_slot.id, request_id=request_id)


async def test_expire_releases_a_lapsed_hold(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    await slot_service.hold_slot(db, future_slot.id, request_id=request_id)
    future_slot.hold_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.flush()

    slot = await slot_service.expire_hold(db, future_slot.id)
    assert slot.status is SlotStatus.available
    assert slot.held_by_request is None


async def test_expire_refuses_a_hold_that_has_not_lapsed(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    await slot_service.hold_slot(db, future_slot.id, request_id=request_id)
    with pytest.raises(InvalidTransition):
        await slot_service.expire_hold(db, future_slot.id)


async def test_a_booked_slot_cannot_be_deleted(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    """A slot a request points at is blocked, not deleted -- which is what
    `blocked` is for (DESIGN.md §8)."""
    await slot_service.book_slot(db, future_slot.id, request_id=request_id)
    with pytest.raises(InvalidTransition):
        await slot_service.delete_slot(db, future_slot.id)


async def test_a_slot_a_finished_request_asked_for_cannot_be_deleted(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    """The slot is back to `available`, and still undeletable.

    Every terminal transition releases the slot and leaves the request pointing
    at it -- that reference is how a rejected request remembers the time it
    asked for (§7.1). Status alone therefore does not answer whether a slot can
    go, and deleting under the foreign key reached the therapist as a 500.
    """
    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.id == request_id))
    ).scalar_one()
    request.slot_id = future_slot.id
    await db.flush()
    await slot_service.book_slot(db, future_slot.id, request_id=request_id)

    await booking.admin_reject(db, request_id, reason="not this one")

    slot = (await db.execute(select(Slot).where(Slot.id == future_slot.id))).scalar_one()
    assert slot.status is SlotStatus.available

    with pytest.raises(SlotReferenced):
        await slot_service.delete_slot(db, future_slot.id)

    # Blocking is the answer the refusal points at, and it still works.
    blocked = await slot_service.block_slot(db, future_slot.id)
    assert blocked.status is SlotStatus.blocked


async def test_an_unreferenced_slot_is_still_deletable(
    db: AsyncSession, future_slot: Slot
) -> None:
    await slot_service.delete_slot(db, future_slot.id)
    assert (
        await db.execute(select(Slot).where(Slot.id == future_slot.id))
    ).scalar_one_or_none() is None


# --- Bulk creation ----------------------------------------------------------


async def test_bulk_creation_materialises_a_weekly_pattern(
    db: AsyncSession, practice: Practice
) -> None:
    # A Monday well clear of any other test's slots.
    start = date(2027, 3, 1)
    pattern = SlotPattern(
        weekdays=frozenset({0, 2}),  # Monday and Wednesday
        times=(time(9, 0), time(10, 0)),
        date_from=start,
        date_to=start + timedelta(days=6),
        duration_min=60,
    )
    created = await slot_service.create_slots_bulk(db, pattern)
    # Two weekdays in the range, two times each.
    assert len(created) == 4


async def test_bulk_creation_skips_slots_that_already_exist(
    db: AsyncSession, practice: Practice
) -> None:
    """Re-running a bulk creation over an overlapping range is a normal thing
    for the therapist to do."""
    start = date(2027, 4, 5)
    pattern = SlotPattern(
        weekdays=frozenset({0}),
        times=(time(11, 0),),
        date_from=start,
        date_to=start,
    )
    assert len(await slot_service.create_slots_bulk(db, pattern)) == 1
    assert await slot_service.create_slots_bulk(db, pattern) == []


async def test_bulk_times_are_interpreted_in_the_practice_timezone(
    db: AsyncSession, practice: Practice
) -> None:
    """The therapist enters slot times in her own clock (DESIGN.md §8)."""
    from zoneinfo import ZoneInfo

    start = date(2027, 5, 3)
    created = await slot_service.create_slots_bulk(
        db,
        SlotPattern(weekdays=frozenset({0}), times=(time(9, 0),), date_from=start, date_to=start),
    )
    local = created[0].starts_at.astimezone(ZoneInfo(practice.timezone))
    assert (local.hour, local.minute) == (9, 0)


# --- Concurrency (§18) ------------------------------------------------------


async def test_two_concurrent_holds_and_exactly_one_wins() -> None:
    """The row lock in hard rule 7, exercised for real (§18).

    This deliberately does not use the `db` fixture. That session runs inside
    one uncommitted transaction, so two coroutines on it would serialise
    trivially and prove nothing about `FOR UPDATE`. Real contention needs two
    independent connections racing for a committed row -- which is exactly the
    shape of the double-booking this lock exists to prevent.

    Everything is created committed and torn down in `finally`, since a
    committed row is by definition not covered by the rollback fixture.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)

    contested_slot_id: int | None = None
    request_ids: list[int] = []

    try:
        async with AsyncSession(engine) as setup:
            practice = await get_practice(setup)
            session_type_id = (
                await setup.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
            ).scalar_one()
            contender = await resolve_client(setup, Channel.telegram, "concurrency-probe")

            for _ in range(2):
                request = BookingRequest(
                    practice_id=practice.id,
                    client_id=contender.id,
                    session_type_id=session_type_id,
                    modality=Modality.online,
                    status=RequestStatus.pending,
                    source_channel=Channel.web,
                )
                setup.add(request)
                await setup.flush()
                request_ids.append(int(request.id))

            slot = Slot(
                practice_id=practice.id,
                # Far enough out that no other test's slot collides with it
                # under the NULLS NOT DISTINCT unique index.
                starts_at=datetime.now(UTC) + timedelta(days=900),
                duration_min=60,
                status=SlotStatus.available,
            )
            setup.add(slot)
            await setup.flush()
            contested_slot_id = int(slot.id)
            await setup.commit()

        async def attempt(request_id: int) -> bool:
            contender_engine = create_async_engine(settings.database_url, poolclass=NullPool)
            try:
                async with AsyncSession(contender_engine) as session:
                    try:
                        assert contested_slot_id is not None
                        await slot_service.hold_slot(
                            session, contested_slot_id, request_id=request_id
                        )
                        await session.commit()
                        return True
                    except SlotUnavailable:
                        await session.rollback()
                        return False
            finally:
                await contender_engine.dispose()

        results = await asyncio.gather(*(attempt(rid) for rid in request_ids))

        assert sum(results) == 1, f"exactly one hold must succeed, got {results}"

        async with AsyncSession(engine) as check:
            held = (
                await check.execute(select(Slot).where(Slot.id == contested_slot_id))
            ).scalar_one()
            assert held.status is SlotStatus.held
            assert held.held_by_request in request_ids
    finally:
        async with engine.begin() as conn:
            if contested_slot_id is not None:
                await conn.execute(Slot.__table__.delete().where(Slot.id == contested_slot_id))
            if request_ids:
                await conn.execute(
                    BookingRequest.__table__.delete().where(
                        BookingRequest.__table__.c.id.in_(request_ids)
                    )
                )
            # The client behind the identity goes too. Deleting only the
            # identity leaves a `client` row nothing can reach, which is a real
            # thing to leave on a shared database -- `unreachable_clients` in
            # §16.9 counts exactly those, and two of them arrived here per run.
            await _delete_probe(conn, "concurrency-probe")
        await engine.dispose()


async def test_two_concurrent_accepts_do_not_both_claim_the_same_slot() -> None:
    """`_matching_slot` finds a slot and `book_slot` takes it, so the search has
    to be locked too (§18, same shape as the hold race above).

    Read without the lock, both accepts were handed the same slot id and the
    loser's `book_slot` raised -- a generic error for a booking that would have
    gone through on retry. Locked, the loser simply finds nothing and confirms
    without a slot, which §7.1's negotiation path already allows.

    Exactly one slot booking, and neither accept fails.
    """
    from app.core.services import booking

    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)

    contested_slot_id: int | None = None
    request_ids: list[int] = []
    request_uuids: list[object] = []
    # Before the 2028 boundary that test_admin_web counts slots from, and clear
    # of the bulk-creation fixtures in 2027-03 and 2027-04. Microsecond
    # precision keeps it unique under the NULLS NOT DISTINCT index.
    when = datetime.now(UTC) + timedelta(days=400)

    try:
        async with AsyncSession(engine) as setup:
            practice = await get_practice(setup)
            session_type = (
                await setup.execute(select(SessionType).order_by(SessionType.id).limit(1))
            ).scalar_one()
            contender = await resolve_client(setup, Channel.telegram, "accept-race-probe")

            for _ in range(2):
                request = BookingRequest(
                    practice_id=practice.id,
                    client_id=contender.id,
                    session_type_id=session_type.id,
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
                        proposed_start=when,
                    )
                )
                request_ids.append(int(request.id))
                request_uuids.append(request.uuid)

            slot = Slot(
                practice_id=practice.id,
                starts_at=when,
                duration_min=60,
                status=SlotStatus.available,
            )
            setup.add(slot)
            await setup.flush()
            contested_slot_id = int(slot.id)
            await setup.commit()

        async def accept(request_id: int) -> bool:
            contender_engine = create_async_engine(settings.database_url, poolclass=NullPool)
            try:
                async with AsyncSession(contender_engine) as session:
                    try:
                        await booking.client_accept(session, request_id)
                        await session.commit()
                        return True
                    except SlotUnavailable:
                        await session.rollback()
                        return False
            finally:
                await contender_engine.dispose()

        results = await asyncio.gather(*(accept(rid) for rid in request_ids))

        assert all(results), f"neither accept should fail on a race, got {results}"

        async with AsyncSession(engine) as check:
            slot_row = (
                await check.execute(select(Slot).where(Slot.id == contested_slot_id))
            ).scalar_one()
            assert slot_row.status is SlotStatus.booked

            owners = (
                (
                    await check.execute(
                        select(BookingRequest.slot_id).where(
                            BookingRequest.id.in_(request_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            # One took the slot; the other confirmed at the same instant without
            # one, which is the therapist's call to make rather than a crash.
            assert sorted(owners, key=lambda v: (v is None, v)) == [contested_slot_id, None]
    finally:
        async with engine.begin() as conn:
            # Free the slot before the requests go: §6.4 requires a `booked`
            # slot to name its request, and the FK nulls `booked_request` on
            # delete -- dropping the request first trips the check constraint.
            if contested_slot_id is not None:
                await conn.execute(
                    Slot.__table__.update()
                    .where(Slot.__table__.c.id == contested_slot_id)
                    .values(
                        status=SlotStatus.available,
                        booked_request=None,
                        held_by_request=None,
                        hold_expires_at=None,
                    )
                )
            if request_ids:
                for table, column in (
                    (Reminder.__table__, "request_id"),
                    (NegotiationMessage.__table__, "request_id"),
                    (OutboxMessage.__table__, "request_id"),
                ):
                    await conn.execute(table.delete().where(table.c[column].in_(request_ids)))
                # Scoped to this test's own rows. `entity_type` alone would take
                # every booking audit row in the database with it.
                await conn.execute(
                    AuditLog.__table__.delete().where(
                        AuditLog.__table__.c.entity_type == "booking_request",
                        AuditLog.__table__.c.entity_id.in_([str(u) for u in request_uuids]),
                    )
                )
                await conn.execute(
                    BookingRequest.__table__.delete().where(
                        BookingRequest.__table__.c.id.in_(request_ids)
                    )
                )
            if contested_slot_id is not None:
                await conn.execute(
                    Slot.__table__.delete().where(Slot.__table__.c.id == contested_slot_id)
                )
            await _delete_probe(conn, "accept-race-probe")
        await engine.dispose()


# --- Filtering (§6.4: an unset modality means "either") ---------------------


async def test_a_slot_with_no_modality_is_offered_for_both(
    db: AsyncSession, future_slot: Slot
) -> None:
    """§6.4: modality NULL means "either".

    Regression: written as `modality IN (chosen, NULL)` this silently returned
    nothing, because in SQL `NULL IN (...)` is never true -- so every "either"
    slot vanished from the picker the moment a client chose online or on-site.
    """
    assert future_slot.modality is None
    window = (
        future_slot.starts_at - timedelta(hours=1),
        future_slot.starts_at + timedelta(hours=1),
    )

    for modality in (Modality.online, Modality.onsite, None):
        found = await slot_service.list_available_slots(
            db, window_from=window[0], window_to=window[1], modality=modality
        )
        assert future_slot.id in {s.id for s in found}, f"missing for {modality}"


async def test_a_modality_specific_slot_is_only_offered_for_that_modality(
    db: AsyncSession, practice: Practice
) -> None:
    starts_at = datetime.now(UTC) + timedelta(days=11, microseconds=31)
    slot = Slot(
        practice_id=practice.id,
        starts_at=starts_at,
        duration_min=60,
        modality=Modality.online,
        status=SlotStatus.available,
    )
    db.add(slot)
    await db.flush()

    window = (starts_at - timedelta(hours=1), starts_at + timedelta(hours=1))
    online = await slot_service.list_available_slots(
        db, window_from=window[0], window_to=window[1], modality=Modality.online
    )
    onsite = await slot_service.list_available_slots(
        db, window_from=window[0], window_to=window[1], modality=Modality.onsite
    )

    assert slot.id in {s.id for s in online}
    assert slot.id not in {s.id for s in onsite}


async def test_a_slot_with_no_session_type_rows_accepts_all_of_them(
    db: AsyncSession, future_slot: Slot, session_type_id: int
) -> None:
    """§6.4: an empty slot_session_type set means all active session types."""
    window = (
        future_slot.starts_at - timedelta(hours=1),
        future_slot.starts_at + timedelta(hours=1),
    )
    found = await slot_service.list_available_slots(
        db, window_from=window[0], window_to=window[1], session_type_id=session_type_id
    )
    assert future_slot.id in {s.id for s in found}


async def test_a_held_slot_is_not_offered(
    db: AsyncSession, future_slot: Slot, request_id: int
) -> None:
    await slot_service.hold_slot(db, future_slot.id, request_id=request_id)
    window = (
        future_slot.starts_at - timedelta(hours=1),
        future_slot.starts_at + timedelta(hours=1),
    )
    found = await slot_service.list_available_slots(db, window_from=window[0], window_to=window[1])
    assert future_slot.id not in {s.id for s in found}
