"""Dates in the reader's language (§15).

`strftime` speaks the process locale, which in a container is C -- so every day
heading in the slot pickers came out in English whatever the client had chosen.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.render.dates import day_label

WHEN = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)  # a Thursday


async def test_the_day_is_written_in_the_clients_language(db: AsyncSession) -> None:
    assert await day_label(db, "ru", WHEN, "Asia/Yerevan") == "чт 27 авг"
    assert await day_label(db, "en", WHEN, "Asia/Yerevan") == "Thu 27 Aug"


async def test_the_day_is_the_one_the_client_is_living_in(db: AsyncSession) -> None:
    """22:00 UTC is already tomorrow in Yerevan, and the heading has to agree
    with the times listed under it."""
    late = datetime(2026, 8, 27, 22, 0, tzinfo=UTC)

    assert await day_label(db, "en", late, "UTC") == "Thu 27 Aug"
    assert await day_label(db, "en", late, "Asia/Yerevan") == "Fri 28 Aug"


async def test_an_untranslated_language_falls_back_whole(db: AsyncSession) -> None:
    """§15's chain, applied to every part: a half-Russian half-English heading
    would be worse than either."""
    label = await day_label(db, "hy", WHEN, "Asia/Yerevan")

    assert label == await day_label(db, "ru", WHEN, "Asia/Yerevan")


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_every_month_and_weekday_has_a_word(db: AsyncSession, lang: str) -> None:
    """A missing key renders as the key itself, in front of a client."""
    for month in range(1, 13):
        for day in range(1, 8):  # 1..7 Sep 2026 covers every weekday
            label = await day_label(
                db, lang, datetime(2026, month, day, 12, 0, tzinfo=UTC), "UTC"
            )
            assert "date." not in label, label
