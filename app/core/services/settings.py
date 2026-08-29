"""Practice settings (IMPLEMENTATION.md §8).

One practice is served. `get_practice` is the single way the rest of the core
reaches it, so no service has to decide what "the practice" means.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.models import Practice

#: Settings the admin UI may change. Anything not listed here is either an
#: environment variable (§4) or not settable at all.
MUTABLE_FIELDS = frozenset(
    {
        "name",
        "default_language",
        "timezone",
        "clinic_onsite_url",
        "online_only",
        "online_meeting_url",
        "availability_on",
        "booking_mode",
        "fallback_to_negotiation",
        "negotiation_enabled",
        "auto_confirm_slots",
        "slot_hold_minutes",
        "pending_expiry_hours",
        "cancel_window_hours",
        "reminder_offsets_min",
        "retention_months",
    }
)


async def get_practice(session: AsyncSession) -> Practice:
    practice = (
        await session.execute(select(Practice).order_by(Practice.id).limit(1))
    ).scalar_one_or_none()
    if practice is None:
        raise NotFound("no practice row; run the seed")
    return practice


async def update_settings(session: AsyncSession, **changes: Any) -> Practice:
    """Apply admin settings changes. Unknown or immutable fields are refused
    rather than silently ignored."""
    unknown = set(changes) - MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"not settable: {sorted(unknown)}")

    practice = await get_practice(session)
    for field, value in changes.items():
        setattr(practice, field, value)
    await session.flush()
    return practice
