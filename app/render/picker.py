"""The slot picker's data: a strip of days that have times, and one day's times in three parts.

Pure functions over (slot id, UTC start) pairs and an IANA zone, so the grouping can be tested
without a database or a template. A day is the calendar date in the client's zone (never the
heading it will be given: keying on rendered text is how two days quietly become one), and a part of
the day is read off the local hour.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

PARTS = ("morning", "afternoon", "evening")

#: Local hours: before noon is morning, noon to before 18:00 afternoon, the rest evening.
AFTERNOON_FROM = 12
EVENING_FROM = 18


def part_of_day(hour: int) -> str:
    if hour < AFTERNOON_FROM:
        return "morning"
    if hour < EVENING_FROM:
        return "afternoon"
    return "evening"


@dataclass(frozen=True)
class DayEntry:
    day: date
    count: int
    #: The first free start of the day: the instant whose GMT offset the zone line states.
    first_utc: datetime


@dataclass(frozen=True)
class TimeEntry:
    slot_id: int
    label: str  # 18:00, in the client's zone
    starts_utc: datetime


@dataclass
class PickerData:
    days: list[DayEntry] = field(default_factory=list)
    chosen: date | None = None
    #: Only the parts that have times, in morning, afternoon, evening order.
    groups: list[tuple[str, list[TimeEntry]]] = field(default_factory=list)

    @property
    def zone_instant(self) -> datetime | None:
        """The instant of the chosen day: DST makes the offset the day's, not today's."""
        for entry in self.days:
            if entry.day == self.chosen:
                return entry.first_utc
        return None


def build_picker(
    slots: Sequence[tuple[int, datetime]], tz: str, requested_day: str | None = None
) -> PickerData:
    """`slots` are (id, aware UTC start) pairs. `requested_day` is an ISO date (`2026-11-17`); one
    that has no times, or is not a date, falls back to the first day that has."""
    zone = ZoneInfo(tz)
    by_day: dict[date, list[tuple[int, datetime]]] = {}
    for slot_id, starts in sorted(slots, key=lambda pair: pair[1]):
        by_day.setdefault(starts.astimezone(zone).date(), []).append((slot_id, starts))

    data = PickerData(
        days=[DayEntry(day, len(items), items[0][1]) for day, items in sorted(by_day.items())]
    )
    if not data.days:
        return data

    chosen = data.days[0].day
    if requested_day:
        try:
            wanted = date.fromisoformat(requested_day)
        except ValueError:
            wanted = None
        if wanted in by_day:
            chosen = wanted
    data.chosen = chosen

    grouped: dict[str, list[TimeEntry]] = {part: [] for part in PARTS}
    for slot_id, starts in by_day[chosen]:
        local = starts.astimezone(zone)
        grouped[part_of_day(local.hour)].append(TimeEntry(slot_id, f"{local:%H:%M}", starts))
    data.groups = [(part, grouped[part]) for part in PARTS if grouped[part]]
    return data
