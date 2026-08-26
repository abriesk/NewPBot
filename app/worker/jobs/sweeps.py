"""The database sweeps (IMPLEMENTATION.md §14).

Nine jobs. Eight are the same shape: claim due rows with FOR UPDATE SKIP LOCKED,
act, commit. Every one is idempotent and safe to run beside a second worker,
even though only one is deployed.

There is no in-memory scheduler here and there must never be: APScheduler,
Celery, and Redis are forbidden (§2). A due-time query survives a deploy; a
process holding jobs in memory does not.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, func, select, update

from app.core.enums import ReminderState, RequestStatus, SlotStatus
from app.core.models import (
    AuthToken,
    BookingRequest,
    ContentBlockRevision,
    ErrorEvent,
    NegotiationMessage,
    Reminder,
    Slot,
)
from app.core.policies import now_utc
from app.core.services import booking, notifications, translations
from app.core.services import slots as slot_service
from app.core.services.content import MAX_REVISIONS
from app.core.services.notifications import Recipient
from app.core.services.settings import get_practice
from app.db import unit_of_work

logger = logging.getLogger(__name__)

BATCH_SIZE = 100

#: §14. Expired tokens are kept a week so a support question about a bad link
#: can still be answered, then deleted.
TOKEN_GRACE = timedelta(days=7)

#: §14. Long enough to answer "how long has this been happening?", no longer.
ERROR_EVENT_RETENTION = timedelta(days=30)


async def expire_slot_holds() -> int:
    """`slot.status='held' AND hold_expires_at < now()` -> available.

    A hold lapses when the window it was given runs out: the client's
    form-filling one for a slot nobody has submitted against, the request's own
    `expires_at` once they have (§7.2).

    The transition is `slot_service.expire_hold` rather than three assignments
    here. Written out a second time, this job and the service were free to
    disagree about what expiring a hold means, and only one of them is the state
    machine.
    """
    async with unit_of_work() as session:
        slot_ids = (
            (
                await session.execute(
                    select(Slot.id)
                    .where(
                        Slot.status == SlotStatus.held,
                        Slot.hold_expires_at < now_utc(),
                    )
                    .limit(BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for slot_id in slot_ids:
            await slot_service.expire_hold(session, slot_id)
        if slot_ids:
            logger.info("released %d lapsed slot hold(s)", len(slot_ids))
        return len(slot_ids)


async def expire_requests() -> int:
    """`status='pending' AND expires_at < now()` -> expired.

    Only `pending` expires. A `negotiating` request stays open until someone
    closes it -- an automatic close would fire on exactly the clients who are
    slowest to reply (DESIGN.md §9).
    """
    async with unit_of_work() as session:
        rows = (
            (
                await session.execute(
                    select(BookingRequest.id)
                    .where(
                        BookingRequest.status == RequestStatus.pending,
                        BookingRequest.expires_at < now_utc(),
                    )
                    .limit(BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for request_id in rows:
            await booking.expire_request(session, request_id)
        await notifications.publish(session)
        if rows:
            logger.info("expired %d pending request(s)", len(rows))
        return len(rows)


async def complete_requests() -> int:
    """`status='confirmed' AND scheduled_start + duration < now()` -> completed.

    Set by the worker once the end time has passed, not by anyone clicking
    anything, and deliberately silent -- no notification (§7.1).
    """
    async with unit_of_work() as session:
        rows = (
            (
                await session.execute(
                    select(BookingRequest.id)
                    .where(
                        BookingRequest.status == RequestStatus.confirmed,
                        BookingRequest.scheduled_start.isnot(None),
                        BookingRequest.scheduled_start
                        + func.make_interval(
                            0, 0, 0, 0, 0, func.coalesce(BookingRequest.scheduled_duration_min, 60)
                        )
                        < now_utc(),
                    )
                    .limit(BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for request_id in rows:
            await booking.complete_request(session, request_id)
        if rows:
            logger.info("completed %d session(s)", len(rows))
        return len(rows)


async def fire_reminders() -> int:
    """`reminder.state='scheduled' AND due_at <= now()`.

    Creates outbox rows with `dedupe_key = 'reminder:{reminder_id}'` (§14) and
    marks the reminder `sent`. The dedupe key is what makes a worker restart
    mid-sweep unable to double-notify.
    """
    async with unit_of_work() as session:
        due = (
            (
                await session.execute(
                    select(Reminder)
                    .where(
                        Reminder.state == ReminderState.scheduled,
                        Reminder.due_at <= now_utc(),
                    )
                    .limit(BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        for reminder in due:
            request = (
                await session.execute(
                    select(BookingRequest).where(BookingRequest.id == reminder.request_id)
                )
            ).scalar_one()

            # A reminder for a booking that is no longer confirmed is not sent;
            # cancellation cancels its reminders, but a race is still possible.
            if request.status is not RequestStatus.confirmed:
                reminder.state = ReminderState.skipped
                continue

            await notifications.enqueue_raw(
                session,
                intent_key="reminder.client",
                recipient=Recipient.client,
                payload={
                    "uuid": str(request.uuid),
                    "time": request.scheduled_start.isoformat()
                    if request.scheduled_start
                    else None,
                    "offset_min": reminder.offset_min,
                    "modality": request.modality.value,
                    "join_url": await notifications.join_info(session, request),
                },
                request_id=request.id,
                dedupe_key=f"reminder:{reminder.id}",
            )
            reminder.state = ReminderState.sent
            reminder.fired_at = now_utc()

        if due:
            logger.info("fired %d reminder(s)", len(due))
        return len(due)


async def purge_content() -> int:
    """§16 of DESIGN, §14 here: null out the sensitive free text on terminal
    requests older than `retention_months`, keeping the row for statistics.

    `problem_text` is health-related information about an identifiable person.
    It is the one field in this system that deserves proportionate handling, and
    this job is most of that handling.
    """
    terminal = (
        RequestStatus.completed,
        RequestStatus.cancelled,
        RequestStatus.rejected,
        RequestStatus.expired,
    )

    async with unit_of_work() as session:
        practice = await get_practice(session)
        cutoff = now_utc() - timedelta(days=30 * practice.retention_months)

        rows = (
            (
                await session.execute(
                    select(BookingRequest)
                    .where(
                        BookingRequest.status.in_(terminal),
                        BookingRequest.updated_at < cutoff,
                        (
                            BookingRequest.problem_text.isnot(None)
                            | BookingRequest.contact_note.isnot(None)
                        ),
                    )
                    .limit(BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        for request in rows:
            request.problem_text = None
            request.contact_note = None
            await session.execute(
                update(NegotiationMessage)
                .where(NegotiationMessage.request_id == request.id)
                .values(body_text=None)
            )

        if rows:
            # Identifiers, never content (hard rule 8).
            logger.info("purged retained content from %d request(s)", len(rows))
        return len(rows)


async def prune_tokens() -> int:
    """`auth_token.expires_at < now() - 7 days` -> deleted."""
    async with unit_of_work() as session:
        result = await session.execute(
            delete(AuthToken).where(AuthToken.expires_at < now_utc() - TOKEN_GRACE)
        )
        count = int(getattr(result, "rowcount", 0) or 0)
        if count:
            logger.info("pruned %d expired token(s)", count)
        return count


async def prune_revisions() -> int:
    """Blocks with more than 20 revisions lose the oldest (§6.7).

    `upsert_block` already prunes as it writes; this catches rows left by an
    import or a direct edit.
    """
    async with unit_of_work() as session:
        block_ids = (
            (
                await session.execute(
                    select(ContentBlockRevision.block_id)
                    .group_by(ContentBlockRevision.block_id)
                    .having(func.count() > MAX_REVISIONS)
                )
            )
            .scalars()
            .all()
        )

        removed = 0
        for block_id in block_ids:
            stale = (
                (
                    await session.execute(
                        select(ContentBlockRevision.id)
                        .where(ContentBlockRevision.block_id == block_id)
                        .order_by(ContentBlockRevision.version.desc())
                        .offset(MAX_REVISIONS)
                    )
                )
                .scalars()
                .all()
            )
            if stale:
                await session.execute(
                    delete(ContentBlockRevision).where(ContentBlockRevision.id.in_(stale))
                )
                removed += len(stale)

        if removed:
            logger.info("pruned %d content revision(s)", removed)
        return removed


async def prune_error_events() -> int:
    """`error_event.at < now() - 30 days` -> deleted (§14).

    The health checks only ever look an hour back (§16.9); the rest is kept
    long enough for somebody to ask "how long has this been happening?" and no
    longer.
    """
    async with unit_of_work() as session:
        result = await session.execute(
            delete(ErrorEvent).where(ErrorEvent.at < now_utc() - ERROR_EVENT_RETENTION)
        )
        count = int(getattr(result, "rowcount", 0) or 0)
        if count:
            logger.info("pruned %d error event(s)", count)
        return count


async def refresh_translations() -> int:
    """Drop this process's UI-string cache when another one has edited it (§15).

    The admin UI clears the cache it shares with the web process the moment the
    therapist saves. The worker is a separate process and renders every outbox
    message through the same cache, so without this the wording on the web
    changes and the wording in Telegram and email does not -- until the
    container happens to restart.

    Runs before `dispatch_outbox` so an edit is picked up in the same pass that
    renders the messages waiting behind it. The whole cost is one aggregate over
    a few hundred short rows.
    """
    async with unit_of_work() as session:
        dropped = await translations.invalidate_if_stale(session)
    if dropped:
        logger.info("translation cache refreshed: another process edited a string")
    return int(dropped)
