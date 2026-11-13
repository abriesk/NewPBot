"""NP4b: the slot picker's data (IMPLEMENTATION.md §12.1, DESIGN.md §8).

Days are calendar dates in the client's zone, times are grouped morning / afternoon / evening by
the local hour, and the zone's GMT offset is read at the chosen day's instant, not today's.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.render.picker import build_picker, part_of_day
from app.render.zones import gmt_offset


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def test_the_parts_of_the_day_split_at_noon_and_six() -> None:
    assert [part_of_day(h) for h in (0, 9, 11, 12, 17, 18, 23)] == [
        "morning",
        "morning",
        "morning",
        "afternoon",
        "afternoon",
        "evening",
        "evening",
    ]


def test_the_strip_has_only_days_with_times_and_counts_them() -> None:
    slots = [
        (3, _utc(2026, 11, 18, 6)),
        (1, _utc(2026, 11, 16, 6)),
        (2, _utc(2026, 11, 16, 7)),
    ]
    data = build_picker(slots, "Asia/Yerevan")
    assert [(d.day.isoformat(), d.count) for d in data.days] == [("2026-11-16", 2), ("2026-11-18", 1)]
    assert data.chosen is not None and data.chosen.isoformat() == "2026-11-16"


def test_a_late_utc_slot_belongs_to_the_next_local_day() -> None:
    # 21:30 UTC is 01:30 the next day in Yerevan (GMT+4): the day is the client's, not UTC's.
    data = build_picker([(1, _utc(2026, 11, 16, 21, 30))], "Asia/Yerevan")
    assert data.days[0].day.isoformat() == "2026-11-17"
    assert data.groups[0][0] == "morning"


def test_the_chosen_days_times_are_grouped_in_order_and_empty_parts_are_left_out() -> None:
    slots = [(1, _utc(2026, 11, 16, 15)), (2, _utc(2026, 11, 16, 5)), (3, _utc(2026, 11, 16, 6))]
    data = build_picker(slots, "Asia/Yerevan", "2026-11-16")  # 09:00 10:00 19:00 local
    assert [(part, [t.label for t in times]) for part, times in data.groups] == [
        ("morning", ["09:00", "10:00"]),
        ("evening", ["19:00"]),
    ]


def test_a_requested_day_without_times_or_not_a_date_falls_back_to_the_first() -> None:
    slots = [(1, _utc(2026, 11, 16, 6)), (2, _utc(2026, 11, 18, 6))]
    assert build_picker(slots, "UTC", "2026-11-18").chosen.isoformat() == "2026-11-18"  # type: ignore[union-attr]
    assert build_picker(slots, "UTC", "2026-11-17").chosen.isoformat() == "2026-11-16"  # type: ignore[union-attr]
    assert build_picker(slots, "UTC", "garbage").chosen.isoformat() == "2026-11-16"  # type: ignore[union-attr]


def test_no_slots_gives_an_empty_picker() -> None:
    data = build_picker([], "UTC")
    assert data.days == [] and data.chosen is None and data.groups == []
    assert data.zone_instant is None


def test_the_zone_offset_is_the_chosen_days_across_a_clock_change() -> None:
    # Berlin changes clocks on 2026-03-29: Saturday is GMT+1, Monday GMT+2.
    slots = [(1, _utc(2026, 3, 28, 10)), (2, _utc(2026, 3, 30, 10))]
    saturday = build_picker(slots, "Europe/Berlin", "2026-03-28")
    monday = build_picker(slots, "Europe/Berlin", "2026-03-30")
    assert saturday.zone_instant is not None and monday.zone_instant is not None
    assert gmt_offset("Europe/Berlin", saturday.zone_instant) == "GMT+1"
    assert gmt_offset("Europe/Berlin", monday.zone_instant) == "GMT+2"
