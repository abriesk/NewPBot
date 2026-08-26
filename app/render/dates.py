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


__all__ = ["day_label"]
