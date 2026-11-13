"""NP4a: one helper for every client-visible time: localised weekday + date + 24 h time + city and GMT offset."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.render.dates import when_label
from app.render.zones import city_name, gmt_offset, zone_label

#: Tuesday 17 November 2026, 14:00 UTC = 18:00 in Yerevan (GMT+4, no daylight saving).
INSTANT = datetime(2026, 11, 17, 14, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("ru", "вт 17 ноя, 18:00 (Ереван, GMT+4)"),
        ("en", "Tue 17 Nov, 18:00 (Yerevan, GMT+4)"),
        ("hy", "Երք 17 Նոյ, 18:00 (Երևան, GMT+4)"),
    ],
)
async def test_when_label_per_language(db, lang: str, expected: str) -> None:
    assert await when_label(db, lang, INSTANT, "Asia/Yerevan") == expected


async def test_when_label_follows_the_clients_zone_and_the_date_line(db) -> None:
    # 23:30 UTC on the 17th is already the 18th in Yerevan, and 18:30 in New York (GMT-5 in November).
    late = datetime(2026, 11, 17, 23, 30, tzinfo=UTC)
    assert await when_label(db, "en", late, "Asia/Yerevan") == "Wed 18 Nov, 03:30 (Yerevan, GMT+4)"
    assert await when_label(db, "ru", late, "America/New_York") == "вт 17 ноя, 18:30 (Нью-Йорк, GMT-5)"


def test_zone_names_and_offsets() -> None:
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert gmt_offset("Europe/Berlin", summer) == "GMT+2"
    assert gmt_offset("Europe/London", INSTANT) == "GMT"
    assert gmt_offset("Asia/Tehran", INSTANT) == "GMT+3:30"
    assert zone_label("Europe/Moscow", "hy", INSTANT) == "Մոսկվա (GMT+3)"
    # an unlisted zone falls back to the city part of its id, never the raw id
    assert city_name("Pacific/Port_Moresby", "ru") == "Port Moresby"
