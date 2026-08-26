"""Slot lifecycle (IMPLEMENTATION.md §7.2, §8).

The transition table:

| From        | Event            | To        | Guard                            |
|-------------|------------------|-----------|----------------------------------|
| available   | hold(request)    | held      | row lock; starts_at > now()      |
| held        | release          | available | -                                |
| held        | expire (worker)  | available | hold_expires_at < now()          |
| held        | book(request)    | booked    | held_by_request = request.id     |
| available   | book(request)    | booked    | row lock                         |
| booked      | release          | available | request left confirmed           |
| available   | block            | blocked   | admin                            |
| blocked     | unblock          | available | admin                            |

`hold` and `book` take `SELECT ... FOR UPDATE` on the slot **before** checking
its status (hard rule 7). This is the only place in the system where a lost
update would double-book a client, and the concurrency test in
tests/core/test_slots.py is what keeps it honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Modality, SlotStatus
from app.core.errors import InvalidTransition, NotFound, SlotInThePast, SlotUnavailable
from app.core.models import Slot, SlotSessionType
from app.core.policies import hold_expiry, now_utc
from app.core.services.settings import get_practice


@dataclass(frozen=True, slots=True)
class SlotView:
    """A slot as a client sees it: the instant, plus the same instant rendered
    in their own timezone. Conversion happens here, at the edge; storage stays
    UTC (DESIGN.md §8)."""

    id: int
    starts_at_utc: datetime
    starts_at_local: datetime
    duration_min: int
    modality: Modality | None


async def _lock(session: AsyncSession, slot_id: int) -> Slot:
    """Fetch a slot under a row lock.

    Hard rule 7: this MUST happen before any status check in hold or book.
    """
    slot = (
        await session.execute(select(Slot).where(Slot.id == slot_id).with_for_update())
    ).scalar_one_or_none()
    if slot is None:
        raise NotFound(f"slot {slot_id}")
    return slot


async def list_available_slots(
    session: AsyncSession,
    *,
    window_from: datetime,
    window_to: datetime,
    session_type_id: int | None = None,
    modality: Modality | None = None,
    tz: str | None = None,
) -> list[SlotView]:
    """Bookable slots in a window, rendered in the client's timezone.

    A slot with no `slot_session_type` rows accepts **all** active session
    types (§6.4), so the session-type filter must not exclude it.
    """
    practice = await get_practice(session)
    zone = ZoneInfo(tz or practice.timezone)

    stmt = select(Slot).where(
        Slot.status == SlotStatus.available,
        Slot.starts_at >= window_from,
        Slot.starts_at <= window_to,
    )
    if modality is not None:
        # A slot with modality NULL is offered as "either". This MUST NOT be
        # written as `IN (modality, NULL)`: in SQL, NULL IN (...) is never
        # true, so every "either" slot would silently disappear from the
        # picker the moment a client chose online or on-site.
        stmt = stmt.where((Slot.modality == modality) | (Slot.modality.is_(None)))
    if session_type_id is not None:
        restricted = select(SlotSessionType.slot_id).where(
            SlotSessionType.session_type_id == session_type_id
        )
        unrestricted = (
            ~select(SlotSessionType.slot_id).where(SlotSessionType.slot_id == Slot.id).exists()
        )
        stmt = stmt.where(Slot.id.in_(restricted) | unrestricted)

    slots = (await session.execute(stmt.order_by(Slot.starts_at))).scalars().all()
    return [
        SlotView(
            id=slot.id,
            starts_at_utc=slot.starts_at,
            starts_at_local=slot.starts_at.astimezone(zone),
            duration_min=slot.duration_min,
            modality=slot.modality,
        )
        for slot in slots
    ]


async def hold_slot(
    session: AsyncSession,
    slot_id: int,
    request_id: int,
    *,
    until: datetime | None = None,
) -> Slot:
    """available -> held. Takes the row lock first.

    `until` is how long the hold lasts. The default is `slot_hold_minutes`, the
    window a client has to finish a form; a request that has been *submitted*
    passes its own `expires_at` instead, so the slot stays held for exactly as
    long as the request can stay undecided. One timer serving both meant a
    request lived 48 hours while the slot behind it went back on the picker
    after 15 minutes, still pointed at by the request (§7.2).

    Never None: §6.4 makes `status='held'` ⟺ `hold_expires_at IS NOT NULL`, so
    "held indefinitely" is not a state this schema has.
    """
    slot = await _lock(session, slot_id)

    if slot.status != SlotStatus.available:
        raise SlotUnavailable(f"slot {slot_id} is {slot.status.value}")
    if slot.starts_at <= now_utc():
        raise SlotInThePast(f"slot {slot_id} starts in the past")

    practice = await get_practice(session)
    slot.status = SlotStatus.held
    slot.hold_expires_at = until or hold_expiry(practice)
    slot.held_by_request = request_id
    await session.flush()
    return slot


async def extend_hold(session: AsyncSession, slot_id: int, until: datetime) -> Slot:
    """Push a live hold's expiry out. Anything but `held` is left alone.

    A negotiation keeps a slot held while nobody has expired -- §7.1 expires
    only `pending`, so without this the hold inherited from submission lapses
    mid-conversation and the time under discussion goes back on the picker.
    """
    slot = await _lock(session, slot_id)
    if slot.status is SlotStatus.held:
        slot.hold_expires_at = until
        await session.flush()
    return slot


async def book_slot(session: AsyncSession, slot_id: int, request_id: int) -> Slot:
    """available|held -> booked. Takes the row lock first.

    From `held`, the hold must belong to this request -- otherwise approving one
    request would steal the slot another client is still filling in a form for.
    """
    slot = await _lock(session, slot_id)

    if slot.status == SlotStatus.held:
        if slot.held_by_request != request_id:
            raise SlotUnavailable(f"slot {slot_id} is held by another request")
    elif slot.status != SlotStatus.available:
        raise SlotUnavailable(f"slot {slot_id} is {slot.status.value}")

    slot.status = SlotStatus.booked
    slot.hold_expires_at = None
    slot.held_by_request = None
    slot.booked_request = request_id
    await session.flush()
    return slot


async def release_slot(session: AsyncSession, slot_id: int) -> Slot:
    """held|booked -> available.

    Releasing an already-available slot is a no-op rather than an error: the
    terminal request transitions all release unconditionally, and several of
    them can reach a request that never held a slot at all.
    """
    slot = await _lock(session, slot_id)

    if slot.status in (SlotStatus.held, SlotStatus.booked):
        slot.status = SlotStatus.available
        slot.hold_expires_at = None
        slot.held_by_request = None
        slot.booked_request = None
        await session.flush()
    return slot


async def block_slot(session: AsyncSession, slot_id: int) -> Slot:
    """available -> blocked.

    Blocking rather than deleting is what lets a request keep referencing a slot
    the therapist has withdrawn (DESIGN.md §8).
    """
    slot = await _lock(session, slot_id)
    if slot.status != SlotStatus.available:
        raise InvalidTransition("slot", slot.status.value, "block")
    slot.status = SlotStatus.blocked
    await session.flush()
    return slot


async def unblock_slot(session: AsyncSession, slot_id: int) -> Slot:
    """blocked -> available."""
    slot = await _lock(session, slot_id)
    if slot.status != SlotStatus.blocked:
        raise InvalidTransition("slot", slot.status.value, "unblock")
    slot.status = SlotStatus.available
    await session.flush()
    return slot


async def delete_slot(session: AsyncSession, slot_id: int) -> None:
    """Remove a slot outright.

    Only legal while nothing references it. A slot a request points at is
    blocked instead, which is what `blocked` exists for.
    """
    slot = await _lock(session, slot_id)
    if slot.status in (SlotStatus.held, SlotStatus.booked):
        raise InvalidTransition("slot", slot.status.value, "delete")
    await session.delete(slot)
    await session.flush()


@dataclass(frozen=True, slots=True)
class SlotPattern:
    """A weekly pattern over a date range, as the admin bulk form describes it.

    `times` are **local to the practice timezone** -- the therapist thinks in
    her own clock. Converting per day rather than per range is what keeps a
    9am slot at 9am across a DST boundary.
    """

    weekdays: frozenset[int]  # 0 = Monday, matching date.weekday()
    times: tuple[time, ...]
    date_from: date
    date_to: date
    duration_min: int = 60
    modality: Modality | None = None


async def create_slots_bulk(session: AsyncSession, pattern: SlotPattern) -> list[Slot]:
    """Materialise a pattern into slots, skipping ones that already exist.

    Collisions are skipped rather than raised: re-running a bulk creation over
    an overlapping range is a normal thing for the therapist to do, and the
    unique index (§6.4) already defines what "already exists" means.
    """
    practice = await get_practice(session)
    zone = ZoneInfo(practice.timezone)

    rows: list[dict[str, object]] = []
    day = pattern.date_from
    while day <= pattern.date_to:
        if day.weekday() in pattern.weekdays:
            for local_time in pattern.times:
                starts_at = datetime.combine(day, local_time, tzinfo=zone).astimezone(UTC)
                rows.append(
                    {
                        "practice_id": practice.id,
                        "starts_at": starts_at,
                        "duration_min": pattern.duration_min,
                        "modality": pattern.modality,
                        "status": SlotStatus.available,
                    }
                )
        day += timedelta(days=1)

    if not rows:
        return []

    created = (
        (
            await session.execute(
                pg_insert(Slot)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["practice_id", "starts_at", "modality"])
                .returning(Slot)
            )
        )
        .scalars()
        .all()
    )
    await session.flush()
    return list(created)


async def expire_hold(session: AsyncSession, slot_id: int) -> Slot:
    """held -> available, once the hold has lapsed. Called by the worker."""
    slot = await _lock(session, slot_id)
    if slot.status != SlotStatus.held:
        raise InvalidTransition("slot", slot.status.value, "expire")
    if slot.hold_expires_at is not None and slot.hold_expires_at > now_utc():
        raise InvalidTransition("slot", "held", "expire")  # not lapsed yet

    slot.status = SlotStatus.available
    slot.hold_expires_at = None
    slot.held_by_request = None
    await session.flush()
    return slot
