"""Waitlist (IMPLEMENTATION.md §6.6, §8).

Its own table and its own small lifecycle: a waitlist entry has no slot, no
negotiation, and no reminders. Folding it into `booking_request` as
`type='waitlist'` -- as v1.0 did -- forces half the request columns nullable and
pollutes the state machine (DESIGN.md §2).

    new -> contacted -> converted
                     -> closed
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ActorType, WaitlistStatus
from app.core.errors import InvalidTransition, NotFound, RateLimited
from app.core.events import WaitlistJoined, collect
from app.core.models import AuditLog, WaitlistEntry
from app.core.policies import check_client_text, now_utc
from app.core.services.settings import get_practice

#: §17 caps booking submission at 5 an hour per client, and §8 lists
#: `join_waitlist` among the booking submissions. It was the one submission path
#: with nothing in front of it: when availability is off the waitlist is the
#: *only* thing the client is offered (DESIGN.md §6), so it is exactly the form
#: somebody bored can hold down, and every entry pages the therapist.
JOINS_PER_HOUR = 5
JOIN_WINDOW = timedelta(hours=1)

ALLOWED: dict[WaitlistStatus, frozenset[WaitlistStatus]] = {
    WaitlistStatus.new: frozenset({WaitlistStatus.contacted, WaitlistStatus.closed}),
    WaitlistStatus.contacted: frozenset({WaitlistStatus.converted, WaitlistStatus.closed}),
    WaitlistStatus.converted: frozenset(),
    WaitlistStatus.closed: frozenset(),
}


async def _get(session: AsyncSession, entry_id: int) -> WaitlistEntry:
    entry = (
        await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_id))
    ).scalar_one_or_none()
    if entry is None:
        raise NotFound(f"waitlist entry {entry_id}")
    return entry


async def _enforce_join_rate(session: AsyncSession, client_id: UUID) -> None:
    """§17's submission limit, counted over waitlist entries.

    Every status counts, closed ones included: an entry the therapist has
    already dealt with still cost her the reading of it.
    """
    recent = (
        await session.execute(
            select(func.count())
            .select_from(WaitlistEntry)
            .where(
                WaitlistEntry.client_id == client_id,
                WaitlistEntry.created_at >= now_utc() - JOIN_WINDOW,
            )
        )
    ).scalar_one()
    if int(recent) >= JOINS_PER_HOUR:
        raise RateLimited(f"{JOINS_PER_HOUR} waitlist joins an hour is the limit")


async def join_waitlist(
    session: AsyncSession,
    *,
    client_id: UUID,
    problem_text: str | None = None,
    contact_note: str | None = None,
) -> WaitlistEntry:
    """What a client leaves when the practice is closed to new bookings.

    Deliberately not gated on `availability_on`: the waitlist is precisely what
    the client is offered when availability is off (DESIGN.md §6).
    """
    check_client_text(problem_text, "problem_text")
    check_client_text(contact_note, "contact_note")

    await _enforce_join_rate(session, client_id)

    practice = await get_practice(session)
    entry = WaitlistEntry(
        practice_id=practice.id,
        client_id=client_id,
        problem_text=problem_text,
        contact_note=contact_note,
        status=WaitlistStatus.new,
    )
    session.add(entry)
    await session.flush()

    session.add(
        AuditLog(
            practice_id=practice.id,
            actor_type=ActorType.client,
            action="waitlist.join",
            entity_type="waitlist_entry",
            entity_id=str(entry.uuid),
        )
    )
    collect(session, WaitlistJoined(entry_id=entry.id, entry_uuid=entry.uuid))
    return entry


async def _transition(
    session: AsyncSession,
    entry_id: int,
    to: WaitlistStatus,
    action: str,
    *,
    admin_note: str | None = None,
) -> WaitlistEntry:
    entry = await _get(session, entry_id)
    if to not in ALLOWED[entry.status]:
        raise InvalidTransition("waitlist_entry", entry.status.value, to.value)

    entry.status = to
    if to == WaitlistStatus.contacted:
        entry.contacted_at = now_utc()
    if admin_note is not None:
        entry.admin_note = admin_note
    await session.flush()

    session.add(
        AuditLog(
            practice_id=entry.practice_id,
            actor_type=ActorType.admin,
            action=action,
            entity_type="waitlist_entry",
            entity_id=str(entry.uuid),
        )
    )
    return entry


async def mark_contacted(
    session: AsyncSession, entry_id: int, *, admin_note: str | None = None
) -> WaitlistEntry:
    return await _transition(
        session, entry_id, WaitlistStatus.contacted, "waitlist.contact", admin_note=admin_note
    )


async def mark_converted(session: AsyncSession, entry_id: int) -> WaitlistEntry:
    """The client has been turned into a real request. The request itself is
    created through the booking service; this only closes the entry."""
    return await _transition(session, entry_id, WaitlistStatus.converted, "waitlist.convert")


async def close_entry(
    session: AsyncSession, entry_id: int, *, admin_note: str | None = None
) -> WaitlistEntry:
    return await _transition(
        session, entry_id, WaitlistStatus.closed, "waitlist.close", admin_note=admin_note
    )


async def recent(
    session: AsyncSession, *, limit: int = 5, offset: int = 0
) -> list[WaitlistEntry]:
    """§13.2: the waitlist as the phone panel reads it -- newest first."""
    rows = (
        await session.execute(
            select(WaitlistEntry)
            .order_by(WaitlistEntry.created_at.desc(), WaitlistEntry.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return list(rows.all())


async def count_open(session: AsyncSession) -> int:
    """Entries nobody has finished with: `new` or `contacted`."""
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(WaitlistEntry)
                .where(
                    WaitlistEntry.status.in_((WaitlistStatus.new, WaitlistStatus.contacted))
                )
            )
        ).scalar_one()
    )
