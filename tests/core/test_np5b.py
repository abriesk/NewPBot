"""NP5b: the core behind the admin's Today page, the week grid's slots, repeat-last-week and
week templates. Rolled back (the `db` fixture), on dates far enough ahead to be nobody's.

Enforces IMPLEMENTATION.md §7.2 (a slot is blocked, never deleted from under a request) and §12.2.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel, Modality, OutboxStatus, RequestStatus, SlotStatus
from app.core.errors import NotFound
from app.core.models import BookingRequest, Client, OutboxMessage, Practice, Slot
from app.core.policies import now_utc
from app.core.services import booking, notifications
from app.core.services import slots as slot_service
from app.core.services import week_templates as week_tpl

MONDAY = date(2028, 3, 6)  # a Monday, well ahead of any clock


def _zone(practice: Practice) -> ZoneInfo:
    return ZoneInfo(practice.timezone)


async def _slot(
    db: AsyncSession,
    practice: Practice,
    day: date,
    at: str,
    *,
    status: SlotStatus = SlotStatus.available,
    modality: Modality | None = None,
    duration: int = 60,
    request_id: int | None = None,
) -> Slot:
    starts = datetime.combine(day, time.fromisoformat(at), tzinfo=_zone(practice)).astimezone(UTC)
    values: dict[str, object] = {}
    if status is SlotStatus.booked:
        values["booked_request"] = request_id
    if status is SlotStatus.held:
        values["held_by_request"] = request_id
        values["hold_expires_at"] = now_utc() + timedelta(hours=1)
    slot = Slot(
        practice_id=practice.id,
        starts_at=starts,
        duration_min=duration,
        modality=modality,
        status=status,
        **values,
    )
    db.add(slot)
    await db.flush()
    return slot


async def _statuses(db: AsyncSession, day: date, practice: Practice) -> dict[str, SlotStatus]:
    start, end = slot_service.local_day_bounds(_zone(practice), day, day)
    rows = (
        (await db.execute(select(Slot).where(Slot.starts_at >= start, Slot.starts_at < end)))
        .scalars()
        .all()
    )
    return {r.starts_at.astimezone(_zone(practice)).strftime("%H:%M"): r.status for r in rows}


# --- block / unblock a date range ------------------------------------------------------------


async def test_block_range_blocks_every_free_slot_of_the_day(
    db: AsyncSession, practice: Practice
) -> None:
    for at in ("10:00", "11:00", "15:00"):
        await _slot(db, practice, MONDAY, at)
    other = await _slot(db, practice, MONDAY + timedelta(days=1), "10:00")

    result = await slot_service.block_range(db, MONDAY, MONDAY)

    assert result.changed == 3 and result.in_use == ()
    assert set((await _statuses(db, MONDAY, practice)).values()) == {SlotStatus.blocked}
    await db.refresh(other)
    assert other.status is SlotStatus.available  # the next day is untouched


async def test_block_range_refuses_and_lists_a_booked_slot(
    db: AsyncSession, practice: Practice, request_id: int
) -> None:
    await _slot(db, practice, MONDAY, "10:00")
    booked = await _slot(
        db, practice, MONDAY, "12:00", status=SlotStatus.booked, request_id=request_id
    )

    result = await slot_service.block_range(db, MONDAY, MONDAY + timedelta(days=2))

    assert result.changed == 0
    assert result.in_use == (booked.starts_at,)
    # All or nothing: the free 10:00 stayed offered, so the day is not half-closed.
    assert (await _statuses(db, MONDAY, practice))["10:00"] is SlotStatus.available


async def test_block_range_refuses_a_held_slot_too(
    db: AsyncSession, practice: Practice, request_id: int
) -> None:
    held = await _slot(db, practice, MONDAY, "09:00", status=SlotStatus.held, request_id=request_id)
    result = await slot_service.block_range(db, MONDAY, MONDAY)
    assert result.in_use == (held.starts_at,) and result.changed == 0


async def test_unblock_range_offers_blocked_slots_again(
    db: AsyncSession, practice: Practice
) -> None:
    await _slot(db, practice, MONDAY, "10:00", status=SlotStatus.blocked)
    await _slot(db, practice, MONDAY, "11:00")
    result = await slot_service.unblock_range(db, MONDAY, MONDAY)
    assert result.changed == 1
    assert set((await _statuses(db, MONDAY, practice)).values()) == {SlotStatus.available}


async def test_a_range_must_run_forwards_and_stay_within_a_year(db: AsyncSession) -> None:
    with pytest.raises(slot_service.RangeTooLong):
        await slot_service.block_range(db, MONDAY, MONDAY - timedelta(days=1))
    with pytest.raises(slot_service.RangeTooLong):
        await slot_service.block_range(db, MONDAY, MONDAY + timedelta(days=400))


async def test_block_range_leaves_the_past_alone(db: AsyncSession, practice: Practice) -> None:
    yesterday = datetime.now(_zone(practice)).date() - timedelta(days=1)
    past = await _slot(db, practice, yesterday, "10:00")
    result = await slot_service.block_range(db, yesterday, yesterday)
    assert result.changed == 0
    await db.refresh(past)
    assert past.status is SlotStatus.available


# --- repeat a week -----------------------------------------------------------------------------


async def test_repeat_week_copies_the_pattern_to_the_next_week(
    db: AsyncSession, practice: Practice
) -> None:
    await _slot(db, practice, MONDAY, "10:00", modality=Modality.online, duration=50)
    await _slot(db, practice, MONDAY + timedelta(days=2), "15:30")

    result = await slot_service.repeat_week(db, MONDAY, MONDAY + timedelta(days=7))

    assert (result.created, result.existing, result.past, result.blocked) == (2, 0, 0, 0)
    nxt = MONDAY + timedelta(days=7)
    start, end = slot_service.local_day_bounds(_zone(practice), nxt, nxt + timedelta(days=6))
    copies = (
        (
            await db.execute(
                select(Slot)
                .where(Slot.starts_at >= start, Slot.starts_at < end)
                .order_by(Slot.starts_at)
            )
        )
        .scalars()
        .all()
    )
    first = copies[0]
    assert first.starts_at.astimezone(_zone(practice)).strftime("%a %H:%M") == "Mon 10:00"
    assert (first.modality, first.duration_min) == (Modality.online, 50)
    assert copies[1].starts_at.astimezone(_zone(practice)).strftime("%a %H:%M") == "Wed 15:30"


async def test_repeat_week_skips_slots_that_already_exist_and_says_so(
    db: AsyncSession, practice: Practice
) -> None:
    await _slot(db, practice, MONDAY, "10:00")
    await _slot(db, practice, MONDAY, "11:00")
    nxt = MONDAY + timedelta(days=7)
    await _slot(db, practice, nxt, "10:00")  # already there next week

    result = await slot_service.repeat_week(db, MONDAY, nxt)

    assert (result.created, result.existing) == (1, 1)
    again = await slot_service.repeat_week(db, MONDAY, nxt)
    assert (again.created, again.existing) == (0, 2)  # a second press makes nothing


async def test_repeat_week_skips_the_past_and_reports_it(
    db: AsyncSession, practice: Practice
) -> None:
    zone = _zone(practice)
    this_monday = slot_service.week_monday(datetime.now(zone).date())
    last_monday = this_monday - timedelta(days=7)
    await _slot(db, practice, last_monday, "10:00")
    # Copying last week onto *last week's own past*: nothing may be made in the past.
    result = await slot_service.repeat_week(db, last_monday - timedelta(days=7), last_monday)
    assert result.created == 0  # the source week was empty
    await _slot(db, practice, last_monday - timedelta(days=7), "10:00")
    result = await slot_service.repeat_week(db, last_monday - timedelta(days=7), last_monday)
    assert (result.created, result.past) == (0, 1)


async def test_repeat_week_does_not_copy_a_day_she_blocked(
    db: AsyncSession, practice: Practice, request_id: int
) -> None:
    await _slot(db, practice, MONDAY, "10:00")
    await _slot(db, practice, MONDAY + timedelta(days=1), "10:00", status=SlotStatus.blocked)
    await _slot(
        db,
        practice,
        MONDAY + timedelta(days=2),
        "10:00",
        status=SlotStatus.booked,
        request_id=request_id,
    )
    result = await slot_service.repeat_week(db, MONDAY, MONDAY + timedelta(days=7))
    # The booked hour is part of how she works; the blocked day off is not.
    assert (result.created, result.blocked) == (2, 1)


async def test_repeat_week_keeps_the_local_hour_across_a_clock_change(
    db: AsyncSession, practice: Practice
) -> None:
    zone = _zone(practice)
    if zone.key not in ("Europe/Lisbon", "Europe/Berlin", "Europe/London", "Europe/Moscow"):
        pytest.skip("the practice zone has no clock change to cross in March")
    # 2028-03-20 week -> 2028-03-27 week crosses the EU change on 26 March 2028.
    before = date(2028, 3, 20)
    await _slot(db, practice, before + timedelta(days=6), "10:00")  # Sunday 26th is the change day
    result = await slot_service.repeat_week(db, before, before + timedelta(days=7))
    assert result.created == 1
    day = before + timedelta(days=13)
    assert (await _statuses(db, day, practice)).keys() == {"10:00"}


# --- week templates ----------------------------------------------------------------------------


async def test_a_template_saves_a_weeks_pattern_and_applies_it_to_a_range(
    db: AsyncSession, practice: Practice
) -> None:
    await _slot(db, practice, MONDAY, "10:00")
    await _slot(db, practice, MONDAY, "11:00", modality=Modality.onsite)
    await _slot(db, practice, MONDAY + timedelta(days=3), "16:00")
    await _slot(db, practice, MONDAY + timedelta(days=4), "10:00", status=SlotStatus.blocked)

    template, count = await week_tpl.save_template(db, "  Normal   week ", MONDAY)
    assert (template.name, count) == (
        "Normal week",
        3,
    )  # whitespace collapsed; blocked day left out

    first = MONDAY + timedelta(days=14)
    result = await week_tpl.apply_template(db, template.id, first, first + timedelta(days=13))
    assert (result.created, result.existing, result.past) == (6, 0, 0)  # two weeks of three
    assert (await _statuses(db, first, practice)).keys() == {"10:00", "11:00"}
    # Applying again makes nothing new.
    again = await week_tpl.apply_template(db, template.id, first, first + timedelta(days=13))
    assert (again.created, again.existing) == (0, 6)


async def test_saving_under_an_existing_name_replaces_that_template(
    db: AsyncSession, practice: Practice
) -> None:
    await _slot(db, practice, MONDAY, "10:00")
    one, _ = await week_tpl.save_template(db, "Mine", MONDAY)
    await _slot(db, practice, MONDAY, "11:00")
    two, count = await week_tpl.save_template(db, "Mine", MONDAY)
    assert two.id == one.id and count == 2
    assert [t.name for t in await week_tpl.list_templates(db)].count("Mine") == 1


async def test_an_empty_week_or_a_bad_name_is_refused(db: AsyncSession, practice: Practice) -> None:
    with pytest.raises(week_tpl.BadTemplate):
        await week_tpl.save_template(db, "Nothing", MONDAY)
    await _slot(db, practice, MONDAY, "10:00")
    for bad in ("", "   ", "x" * 61):
        with pytest.raises(week_tpl.BadTemplate):
            await week_tpl.save_template(db, bad, MONDAY)


async def test_a_template_can_be_deleted_and_a_missing_one_is_not_found(
    db: AsyncSession, practice: Practice
) -> None:
    await _slot(db, practice, MONDAY, "10:00")
    template, _ = await week_tpl.save_template(db, "Gone soon", MONDAY)
    await week_tpl.delete_template(db, template.id)
    assert "Gone soon" not in [t.name for t in await week_tpl.list_templates(db)]
    with pytest.raises(NotFound):
        await week_tpl.delete_template(db, template.id)
    with pytest.raises(NotFound):
        await week_tpl.apply_template(db, template.id, MONDAY, MONDAY)


# --- the grid's slots and the Today page's facts --------------------------------------------------


async def test_a_slot_next_to_a_booking_is_covered_not_free(
    db: AsyncSession, practice: Practice, request_id: int
) -> None:
    await _slot(db, practice, MONDAY, "10:00", status=SlotStatus.booked, request_id=request_id)
    near = await _slot(db, practice, MONDAY, "10:30")  # overlaps the booked hour
    apart = await _slot(db, practice, MONDAY, "14:00")
    start, end = slot_service.local_day_bounds(_zone(practice), MONDAY, MONDAY)
    by_id = {
        s.id: s for s in await slot_service.slots_in_window(db, window_from=start, window_to=end)
    }
    assert by_id[near.id].covered and not by_id[apart.id].covered


async def test_approve_in_place_only_where_the_slot_is_still_held(
    db: AsyncSession, practice: Practice, request_id: int, client: Client
) -> None:
    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.id == request_id))
    ).scalar_one()
    assert not await booking.can_approve_as_held(db, request)  # no slot at all

    slot = await _slot(db, practice, MONDAY, "10:00", status=SlotStatus.held, request_id=request_id)
    request.slot_id = slot.id
    await db.flush()
    assert await booking.can_approve_as_held(db, request)

    slot.hold_expires_at = now_utc() - timedelta(minutes=1)  # the hold lapsed
    await db.flush()
    assert not await booking.can_approve_as_held(db, request)

    slot.hold_expires_at = now_utc() + timedelta(hours=1)
    request.status = RequestStatus.negotiating  # a negotiation is looked at, not approved blind
    await db.flush()
    assert not await booking.can_approve_as_held(db, request)


async def test_free_slots_for_a_request_match_its_type_and_modality(
    db: AsyncSession, practice: Practice, request_id: int
) -> None:
    request = (
        await db.execute(select(BookingRequest).where(BookingRequest.id == request_id))
    ).scalar_one()  # an online request
    soon = datetime.now(_zone(practice)).date() + timedelta(days=3)
    online = await _slot(db, practice, soon, "09:00", modality=Modality.online)
    await _slot(db, practice, soon, "10:00", modality=Modality.onsite)
    either = await _slot(db, practice, soon, "11:00")
    far = await _slot(db, practice, soon + timedelta(days=40), "09:00")

    ids = {s.id for s in await booking.free_slots_for_request(db, request, days=14)}

    assert {online.id, either.id} <= ids
    assert far.id not in ids
    assert len(ids & {online.id, either.id}) == 2 and all(
        s.modality in (None, Modality.online)
        for s in await booking.free_slots_for_request(db, request, days=14)
        if s.id in ids
    )


async def test_undelivered_counts_are_failed_and_dead_only(
    db: AsyncSession, practice: Practice
) -> None:
    before = await notifications.undelivered_counts(db)
    for n, status in enumerate(
        (
            OutboxStatus.dead,
            OutboxStatus.dead,
            OutboxStatus.failed,
            OutboxStatus.pending,
            OutboxStatus.sent,
        )
    ):
        db.add(
            OutboxMessage(
                practice_id=practice.id,
                channel=Channel.email,
                address=f"x{n}@example.invalid",
                intent_key="request.confirmed.client",
                payload={},
                locale="en",
                status=status,
            )
        )
    await db.flush()
    after = await notifications.undelivered_counts(db)
    assert after[OutboxStatus.dead] - before[OutboxStatus.dead] == 2
    assert after[OutboxStatus.failed] - before[OutboxStatus.failed] == 1
    assert set(after) == {OutboxStatus.dead, OutboxStatus.failed}
