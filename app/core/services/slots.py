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
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.enums import Modality, SlotStatus
from app.core.errors import (
    InvalidTransition,
    NotFound,
    SlotInThePast,
    SlotReferenced,
    SlotUnavailable,
)
from app.core.models import BookingRequest, Slot, SlotSessionType
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


def _ends_at(slot: Any) -> Any:
    return slot.starts_at + func.make_interval(0, 0, 0, 0, 0, slot.duration_min)


def overlaps_a_booked_slot() -> Any:
    """SQL condition on `Slot`: another slot of the practice that is *booked*
    overlaps this one's interval, whatever its modality.

    The therapist is one person. An online and an on-site slot at 09:00 are one
    hour of her, and a 09:30 slot overlaps a 09:00-10:00 one. Derived from the
    bookings themselves rather than written onto the siblings: there is nothing
    to undo when the booking is released, so reopening can only ever reopen what
    the booking closed, and a slot she blocked by hand is never touched (F12).
    """
    other = aliased(Slot)
    return (
        select(other.id)
        .where(
            other.practice_id == Slot.practice_id,
            other.id != Slot.id,
            other.status == SlotStatus.booked,
            other.starts_at < _ends_at(Slot),
            Slot.starts_at < _ends_at(other),
        )
        .exists()
    )


async def open_slot_start(session: AsyncSession, slot_id: int | None) -> datetime | None:
    """The start of this slot if it can still be offered, else None.

    "Can be offered" is what a re-read of a stale screen needs: the slot is
    `available` **and** no booked slot overlaps it (F12 -- an overlapping slot
    stays `available` in the table, it is only closed by the booking next to
    it). One answer for the Telegram counter, the therapist's proposal and the
    web counter, so none of them can name an hour the practice has since lost.
    """
    if slot_id is None:
        return None
    return (
        await session.execute(
            select(Slot.starts_at).where(
                Slot.id == slot_id,
                Slot.status == SlotStatus.available,
                ~overlaps_a_booked_slot(),
            )
        )
    ).scalar_one_or_none()


def _overlaps(base: Slot) -> Any:
    """SQL condition on `Slot`: shares part of `base`'s interval (same practice)."""
    return and_(
        Slot.practice_id == base.practice_id,
        Slot.starts_at < base.starts_at + timedelta(minutes=base.duration_min),
        _ends_at(Slot) > base.starts_at,
    )


async def lock_for_move(
    session: AsyncSession, old_slot_id: int | None, new_start: datetime
) -> None:
    """Take, in one id-ordered statement, every slot a move from `old_slot_id`
    to whatever is published at `new_start` will touch: the old slot, the slots
    at the new instant, and everything overlapping either.

    Releasing or blocking the old slot, then booking the new one, used to lock
    the old slot alone and the new slot's overlap set afterwards. A submit for
    the new slot locks that set first (in id order) and then waits for the old
    one -- a cycle Postgres ends with an uncaught DeadlockDetected. With the
    whole set taken up front, in the one order every other path uses, the later
    `release`/`block`/`book` calls find their rows already locked.
    """
    base_filter = Slot.starts_at == new_start
    if old_slot_id is not None:
        base_filter = or_(base_filter, Slot.id == old_slot_id)
    bases = (await session.execute(select(Slot).where(base_filter))).scalars().all()
    if not bases:
        return
    await session.execute(
        select(Slot.id)
        .where(or_(*[or_(Slot.id == b.id, _overlaps(b)) for b in bases]))
        .order_by(Slot.id)
        .with_for_update()
    )


async def _lock_with_siblings(session: AsyncSession, slot_id: int) -> tuple[Slot, list[Slot]]:
    """Lock a slot **and every slot overlapping it**, in one statement.

    Hard rule 7 for a reservation, widened: booking one of two overlapping slots
    must serialise against booking the other, or both succeed. One statement in
    id order, so two contenders lock the same set in the same order and wait on
    each other rather than deadlocking.
    """
    base = (await session.execute(select(Slot).where(Slot.id == slot_id))).scalar_one_or_none()
    if base is None:
        raise NotFound(f"slot {slot_id}")

    rows = (
        (
            await session.execute(
                select(Slot)
                .where(or_(Slot.id == slot_id, _overlaps(base)))
                .order_by(Slot.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    slot = next((row for row in rows if row.id == slot_id), None)
    if slot is None:
        raise NotFound(f"slot {slot_id}")
    return slot, [row for row in rows if row.id != slot_id]


def _refuse_if_a_sibling_is_booked(slot: Slot, siblings: list[Slot]) -> None:
    for sibling in siblings:
        if sibling.status is SlotStatus.booked:
            raise SlotUnavailable(f"slot {slot.id} overlaps booked slot {sibling.id}")


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
        ~overlaps_a_booked_slot(),
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
    slot, siblings = await _lock_with_siblings(session, slot_id)

    if slot.status != SlotStatus.available:
        raise SlotUnavailable(f"slot {slot_id} is {slot.status.value}")
    if slot.starts_at <= now_utc():
        raise SlotInThePast(f"slot {slot_id} starts in the past")
    _refuse_if_a_sibling_is_booked(slot, siblings)

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
    slot, siblings = await _lock_with_siblings(session, slot_id)

    if slot.status == SlotStatus.held:
        if slot.held_by_request != request_id:
            raise SlotUnavailable(f"slot {slot_id} is held by another request")
    elif slot.status != SlotStatus.available:
        raise SlotUnavailable(f"slot {slot_id} is {slot.status.value}")
    _refuse_if_a_sibling_is_booked(slot, siblings)

    slot.status = SlotStatus.booked
    slot.hold_expires_at = None
    slot.held_by_request = None
    slot.booked_request = request_id
    await session.flush()
    return slot


async def release_slot(session: AsyncSession, slot_id: int, request_id: int) -> Slot:
    """held|booked -> available, by the request that holds or booked it.

    Releasing an already-available slot is a no-op rather than an error: the
    terminal request transitions all release unconditionally, and several of
    them can reach a request that never held a slot at all.

    So is releasing a slot that belongs to somebody else. A request keeps
    pointing at the slot it asked for after it is refused or moved on (§7.1),
    and when it later expires it must not hand back an hour another request
    holds or has booked.
    """
    slot = await _lock(session, slot_id)

    owned = request_id in (slot.held_by_request, slot.booked_request)
    if slot.status in (SlotStatus.held, SlotStatus.booked) and owned:
        slot.status = SlotStatus.available
        slot.hold_expires_at = None
        slot.held_by_request = None
        slot.booked_request = None
        await session.flush()
    return slot


async def block_slot(session: AsyncSession, slot_id: int) -> Slot:
    """available|booked -> blocked (§7.2).

    Blocking rather than deleting is what lets a request keep referencing a slot
    the therapist has withdrawn (DESIGN.md §8).

    From `booked` it is the emergency: she has moved or called off a session and
    the hour must not go back on the picker, because the reason it is free is
    that her day came apart (DESIGN.md §14). Releasing would have offered a
    stranger the very hour she is ill in. The reservation fields are cleared the
    way `release_slot` clears them -- §6.4's CHECK allows neither on a slot that
    is not held or booked -- so the request keeps pointing at the time it had
    and the slot belongs to nobody.
    """
    slot = await _lock(session, slot_id)
    if slot.status not in (SlotStatus.available, SlotStatus.booked):
        raise InvalidTransition("slot", slot.status.value, "block")
    slot.status = SlotStatus.blocked
    slot.hold_expires_at = None
    slot.held_by_request = None
    slot.booked_request = None
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

    # Status alone does not answer "does anything reference this". Every
    # terminal transition releases the slot back to `available` and leaves
    # `booking_request.slot_id` pointing at it -- that is how a rejected or
    # expired request remembers the time it asked for (§7.1). Deleting under
    # that foreign key raised IntegrityError out of the flush, which reached the
    # therapist as a 500 on a button the page had offered her.
    referenced = (
        await session.execute(
            select(BookingRequest.id).where(BookingRequest.slot_id == slot_id).limit(1)
        )
    ).scalar_one_or_none()
    if referenced is not None:
        raise SlotReferenced(f"slot {slot_id} is referenced by a booking request")

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

    return await _insert_slots(session, rows)


async def _insert_slots(session: AsyncSession, rows: list[dict[str, object]]) -> list[Slot]:
    """Insert the rows that do not exist yet; the unique index (§6.4) says what "exists" means."""
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


# --- The week at a glance, and week-sized edits (NP5b) --------------------------------------------


class RangeTooLong(ValueError):
    """A date range longer than a year, or one that ends before it starts."""


MAX_RANGE_DAYS = 366

#: How long one slot may last, in minutes. The bulk form, week templates and the config import all
#: use these, so whatever she can make can be saved in a template and re-imported.
MIN_DURATION = 15
MAX_DURATION = 480


@dataclass(frozen=True, slots=True)
class WeekSlot:
    """A slot as the admin's week grid draws it."""

    id: int
    starts_at: datetime
    duration_min: int
    modality: Modality | None
    status: SlotStatus
    #: `available` in the table, but an overlapping booking has closed it (F12): not really free.
    covered: bool


@dataclass(frozen=True, slots=True)
class RangeResult:
    """What a block / unblock over a date range did. `in_use` is non-empty only when it refused:
    the start of every booked or held slot in the range, soonest first."""

    changed: int
    in_use: tuple[datetime, ...] = ()


@dataclass(frozen=True, slots=True)
class CopyResult:
    """What a week copy or a template application made, and why it did not make the rest."""

    created: int = 0
    existing: int = 0
    past: int = 0
    blocked: int = 0


def local_day_bounds(zone: ZoneInfo, first: date, last: date) -> tuple[datetime, datetime]:
    """UTC instants of local midnight on `first` and of local midnight after `last`."""
    return (
        datetime.combine(first, time.min, tzinfo=zone).astimezone(UTC),
        datetime.combine(last + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC),
    )


async def slots_in_window(
    session: AsyncSession, *, window_from: datetime, window_to: datetime
) -> list[WeekSlot]:
    """Every slot in `[from, to)`, whatever its status, soonest first."""
    rows = await session.execute(
        select(Slot, overlaps_a_booked_slot())
        .where(Slot.starts_at >= window_from, Slot.starts_at < window_to)
        .order_by(Slot.starts_at, Slot.id)
    )
    return [
        WeekSlot(
            id=slot.id,
            starts_at=slot.starts_at,
            duration_min=slot.duration_min,
            modality=slot.modality,
            status=slot.status,
            covered=bool(shadowed) and slot.status is SlotStatus.available,
        )
        for slot, shadowed in rows.all()
    ]


async def _range_rows(
    session: AsyncSession, practice_tz: str, date_from: date, date_to: date
) -> list[Slot]:
    if date_to < date_from or (date_to - date_from).days >= MAX_RANGE_DAYS:
        raise RangeTooLong(f"{date_from}..{date_to}")
    start, end = local_day_bounds(ZoneInfo(practice_tz), date_from, date_to)
    # What is already behind her is not hers to block or free.
    start = max(start, now_utc())
    return list(
        (
            await session.execute(
                select(Slot)
                .where(Slot.starts_at >= start, Slot.starts_at < end)
                .order_by(Slot.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )


async def block_range(session: AsyncSession, date_from: date, date_to: date) -> RangeResult:
    """Block every free slot from local `date_from` to `date_to` inclusive, in one step.

    All or nothing: if any slot in the range is booked or held, **nothing** is blocked and the
    ones in the way are returned, because "block the day" over a booked session would otherwise
    leave her believing the day was clear. Blocking a booked session stays what it always was --
    a deliberate act on that one session (cancel and block).
    """
    practice = await get_practice(session)
    rows = await _range_rows(session, practice.timezone, date_from, date_to)
    in_use = sorted(r.starts_at for r in rows if r.status in (SlotStatus.booked, SlotStatus.held))
    if in_use:
        return RangeResult(0, tuple(in_use))
    free = [r for r in rows if r.status is SlotStatus.available]
    for slot in free:
        slot.status = SlotStatus.blocked
    await session.flush()
    return RangeResult(len(free))


async def unblock_range(session: AsyncSession, date_from: date, date_to: date) -> RangeResult:
    """The inverse: every blocked slot in the range is offered again."""
    practice = await get_practice(session)
    rows = await _range_rows(session, practice.timezone, date_from, date_to)
    blocked = [r for r in rows if r.status is SlotStatus.blocked]
    for slot in blocked:
        slot.status = SlotStatus.available
    await session.flush()
    return RangeResult(len(blocked))


def slot_rows(
    practice_id: int,
    zone: ZoneInfo,
    entries: list[tuple[date, time, int, Modality | None]],
) -> tuple[list[dict[str, object]], int]:
    """(rows to insert, how many were in the past) for local (day, time, duration, modality)."""
    now = now_utc()
    rows: list[dict[str, object]] = []
    past = 0
    for day, local_time, duration, modality in entries:
        starts_at = datetime.combine(day, local_time, tzinfo=zone).astimezone(UTC)
        if starts_at <= now:
            past += 1
            continue
        rows.append(
            {
                "practice_id": practice_id,
                "starts_at": starts_at,
                "duration_min": duration,
                "modality": modality,
                "status": SlotStatus.available,
            }
        )
    return rows, past


async def create_entries(
    session: AsyncSession,
    entries: list[tuple[date, time, int, Modality | None]],
    *,
    blocked: int = 0,
) -> CopyResult:
    """Make the slots for these local entries: never one in the past, never one that exists."""
    practice = await get_practice(session)
    rows, past = slot_rows(practice.id, ZoneInfo(practice.timezone), entries)
    created = await _insert_slots(session, rows)
    return CopyResult(
        created=len(created), existing=len(rows) - len(created), past=past, blocked=blocked
    )


def week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


async def week_pattern(
    session: AsyncSession, monday: date
) -> tuple[list[tuple[int, time, int, Modality | None]], int]:
    """The slot pattern of the week starting `monday`, local clock: ((weekday, time, duration,
    modality) ..., how many blocked slots were left out).

    A blocked slot is a day she took off, not part of how she works, so it is not part of the
    pattern; a booked or held one is, because she offered that hour.
    """
    practice = await get_practice(session)
    zone = ZoneInfo(practice.timezone)
    start, end = local_day_bounds(zone, monday, monday + timedelta(days=6))
    rows = (
        (
            await session.execute(
                select(Slot)
                .where(Slot.starts_at >= start, Slot.starts_at < end)
                .order_by(Slot.starts_at, Slot.id)
            )
        )
        .scalars()
        .all()
    )
    pattern: list[tuple[int, time, int, Modality | None]] = []
    blocked = 0
    for slot in rows:
        if slot.status is SlotStatus.blocked:
            blocked += 1
            continue
        local = slot.starts_at.astimezone(zone)
        pattern.append(
            (local.weekday(), local.time().replace(tzinfo=None), slot.duration_min, slot.modality)
        )
    return pattern, blocked


async def repeat_week(
    session: AsyncSession, source_monday: date, target_monday: date
) -> CopyResult:
    """Copy the slot pattern of the week starting `source_monday` onto the week starting
    `target_monday` (same weekday, same local time). Skips what already exists and what is already
    in the past, and says how many of each."""
    pattern, blocked = await week_pattern(session, source_monday)
    entries = [
        (target_monday + timedelta(days=weekday), at, duration, modality)
        for weekday, at, duration, modality in pattern
    ]
    return await create_entries(session, entries, blocked=blocked)
