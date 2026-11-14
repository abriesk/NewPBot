"""Named week templates (NP5b): save a real week's slot pattern, apply it to a date range.

A template is the *pattern* of a week -- which weekdays, which local times, how long, which
modality -- not its slots: applying it makes ordinary slots, the same way the bulk form and
"repeat last week" do (skipping what exists and what is in the past). Times are kept in the
practice's own clock, so a template applied across a DST change keeps 10:00 at 10:00.
"""

from __future__ import annotations

import unicodedata
from datetime import date, time, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Modality
from app.core.errors import NotFound
from app.core.models import WeekTemplate
from app.core.services import slots as slot_service
from app.core.services.settings import get_practice

NAME_MAX = 60
#: What a template may hold and how many there may be. The config import enforces the same numbers,
#: so every template she can save can be exported and imported again.
ENTRIES_MAX = 300
TEMPLATES_MAX = 50


class BadTemplate(ValueError):
    """A name that is empty, too long or holds a control character; a week with nothing to save, or
    more than a template may hold; too many templates. `args[0]` says which: name, empty week,
    limits, many."""


def clean_name(raw: str) -> str:
    """One line: runs of whitespace (and any newline) become one space."""
    name = " ".join(raw.split())
    # A NUL (or any other control character) is not a name, and Postgres refuses it in text.
    if not name or len(name) > NAME_MAX or any(unicodedata.category(c) == "Cc" for c in name):
        raise BadTemplate("name")
    return name


async def list_templates(session: AsyncSession) -> list[WeekTemplate]:
    return list(
        (await session.execute(select(WeekTemplate).order_by(WeekTemplate.name))).scalars().all()
    )


async def save_template(session: AsyncSession, name: str, monday: date) -> tuple[WeekTemplate, int]:
    """Save the pattern of the week starting `monday` under `name` (replacing a template of that
    name). Returns the template and how many entries it holds. A week with no slots is refused."""
    name = clean_name(name)
    pattern, _blocked = await slot_service.week_pattern(session, monday)
    if not pattern:
        raise BadTemplate("empty week")
    if len(pattern) > ENTRIES_MAX or any(
        not slot_service.MIN_DURATION <= duration <= slot_service.MAX_DURATION
        for _, _, duration, _ in pattern
    ):
        raise BadTemplate("limits")
    entries = [
        {
            "weekday": weekday,
            "time": at.strftime("%H:%M"),
            "duration_min": duration,
            "modality": modality.value if modality else None,
        }
        for weekday, at, duration, modality in pattern
    ]
    practice = await get_practice(session)
    known = (
        await session.execute(
            select(func.count())
            .select_from(WeekTemplate)
            .where(WeekTemplate.practice_id == practice.id)
        )
    ).scalar_one()
    replaces = (
        await session.execute(
            select(WeekTemplate.id).where(
                WeekTemplate.practice_id == practice.id, WeekTemplate.name == name
            )
        )
    ).first()
    if replaces is None and known >= TEMPLATES_MAX:
        raise BadTemplate("many")
    # One statement, so two saves of the same name cannot race on the unique (practice, name).
    saved = (
        (
            await session.execute(
                pg_insert(WeekTemplate)
                .values(practice_id=practice.id, name=name, entries=entries)
                .on_conflict_do_update(
                    index_elements=["practice_id", "name"], set_={"entries": entries}
                )
                .returning(WeekTemplate)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one()
    )
    await session.flush()
    return saved, len(entries)


async def delete_template(session: AsyncSession, template_id: int) -> None:
    gone = await session.execute(delete(WeekTemplate).where(WeekTemplate.id == template_id))
    if gone.rowcount == 0:  # type: ignore[attr-defined]
        raise NotFound(f"week template {template_id}")


async def apply_template(
    session: AsyncSession, template_id: int, date_from: date, date_to: date
) -> slot_service.CopyResult:
    """Make the template's slots on every matching weekday from `date_from` to `date_to`."""
    template = (
        await session.execute(select(WeekTemplate).where(WeekTemplate.id == template_id))
    ).scalar_one_or_none()
    if template is None:
        raise NotFound(f"week template {template_id}")
    if date_to < date_from or (date_to - date_from).days >= slot_service.MAX_RANGE_DAYS:
        raise slot_service.RangeTooLong(f"{date_from}..{date_to}")

    by_weekday: dict[int, list[tuple[time, int, Modality | None]]] = {}
    for entry in template.entries:
        by_weekday.setdefault(int(entry["weekday"]), []).append(
            (
                time.fromisoformat(str(entry["time"])),
                int(entry["duration_min"]),
                Modality(entry["modality"]) if entry.get("modality") else None,
            )
        )
    entries: list[tuple[date, time, int, Modality | None]] = []
    day = date_from
    while day <= date_to:
        for at, duration, modality in by_weekday.get(day.weekday(), []):
            entries.append((day, at, duration, modality))
        day += timedelta(days=1)
    return await slot_service.create_entries(session, entries)
