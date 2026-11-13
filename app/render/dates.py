"""Dates in the reader's language (IMPLEMENTATION.md §15).

`strftime("%A %d %B")` speaks whatever the process locale says, which in a
container is C -- so a Russian client picking a slot was reading "Thursday 27
August". The names come from the catalogue instead, like every other word a
client sees.

Names are abbreviated on purpose. A full month name declines in Russian and in
Armenian ("27 августа", not "27 август"), and a format string cannot know that;
abbreviations are case-neutral in all three languages. Anything the therapist
would want spelled out in full belongs in a content block, not here.

Numeric formats stay where they are: `2026-08-29 16:00` needs no translation,
which is why §13.4's reminders carry it.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.services.translations import get_text
from app.render.zones import city_name, gmt_offset


async def day_label(session: AsyncSession, lang: str, value: datetime, tz: str) -> str:
    """The day `value` falls on in `tz`, written in `lang`.

    A slot picker groups by day and heads each group with this. The time of day
    is not included: the times are the buttons underneath.
    """
    local = value.astimezone(ZoneInfo(tz))
    return await get_text(
        session,
        lang,
        "date.day",
        weekday=await get_text(session, lang, f"date.weekday.{local.weekday()}"),
        day=local.day,
        month=await get_text(session, lang, f"date.month.{local.month}"),
    )


async def when_label(session: AsyncSession, lang: str, value: datetime, tz: str) -> str:
    """Every client-visible moment: `вт 17 ноя, 18:00 (Ереван, GMT+4)`.

    The weekday and month come from the catalogue (like `day_label`), the time is 24-hour, and the
    zone is named by its city in the reader's language with the GMT offset in force at that instant
    -- so a Moscow client is never shown a bare Yerevan time. Numeric ISO stamps are for logs and for
    admins, not for this.
    """
    local = value.astimezone(ZoneInfo(tz))
    day = await day_label(session, lang, value, tz)
    return f"{day}, {local:%H:%M} ({city_name(tz, lang)}, {gmt_offset(tz, value)})"


__all__ = ["day_label", "when_label"]
