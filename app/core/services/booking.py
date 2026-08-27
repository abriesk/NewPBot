"""Booking request lifecycle and negotiation (IMPLEMENTATION.md §7.1, §8).

The transition table, which is the whole point of this module existing in one
place rather than once per channel:

| From        | Event                        | To          |
|-------------|------------------------------|-------------|
| -           | submit                       | pending     |
| pending     | admin_approve                | confirmed   |
| pending     | admin_propose                | negotiating |
| pending     | admin_reject                 | rejected    |
| pending     | expire (worker)              | expired     |
| negotiating | client_accept                | confirmed   |
| negotiating | client_counter               | negotiating |
| negotiating | admin_propose                | negotiating |
| negotiating | client_decline, admin_reject | rejected    |
| confirmed   | admin_cancel                 | cancelled   |
| confirmed   | complete (worker)            | completed   |

Anything not in that table raises `InvalidTransition` and changes nothing.
There is no path from `confirmed` back to `negotiating`: a change of time after
confirmation is a cancellation plus a new request, which is what keeps reminders
and slot bookkeeping honest (DESIGN.md §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    ActorType,
    Channel,
    Modality,
    NegotiationKind,
    ReminderState,
    RequestStatus,
    SenderType,
    SlotStatus,
)
from app.core.errors import (
    BookingClosed,
    InvalidTransition,
    NegotiationDisabled,
    NotFound,
    RateLimited,
)
from app.core.events import (
    RequestAccepted,
    RequestCancelled,
    RequestConfirmed,
    RequestCounter,
    RequestExpired,
    RequestNote,
    RequestProposal,
    RequestRejected,
    RequestSubmitted,
    collect,
)
from app.core.models import (
    AuditLog,
    BookingRequest,
    Client,
    NegotiationMessage,
    Reminder,
    SessionType,
    Slot,
)
from app.core.policies import check_client_text, now_utc, pending_expiry, reminder_schedule
from app.core.services import slots as slot_service
from app.core.services.settings import get_practice

#: §17: booking submission, 5 per hour per client. Counted from the requests
#: themselves rather than a counter table -- they already carry the timestamp,
#: and the count survives a restart. Enforced in the core so every channel gets
#: it, not just the one that remembered to ask.
SUBMISSIONS_PER_HOUR = 5
SUBMISSION_WINDOW = timedelta(hours=1)

#: A request the therapist can still act on: not yet answered, being negotiated,
#: or booked. Everything else is history.
ACTIVE_STATUSES = frozenset(
    {RequestStatus.pending, RequestStatus.negotiating, RequestStatus.confirmed}
)

#: §12.2: what the week schedule draws. `completed` is here and `cancelled` is
#: not, which is the difference between a record of the week and a wish for it.
SCHEDULE_STATUSES = frozenset(
    {
        RequestStatus.pending,
        RequestStatus.negotiating,
        RequestStatus.confirmed,
        RequestStatus.completed,
    }
)


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One row of §12.2's week schedule, flat enough to render without a query.

    Exactly two shapes exist and they are mutually exclusive: an entry with a
    `starts_at` goes in a day column, and an entry with `desired_time_text` goes
    in the list beneath it. Deliberately not carrying duration or modality --
    the view is a quiet one and both are a click away on the request page.
    """

    uuid: UUID
    status: RequestStatus
    display_name: str | None
    starts_at: datetime | None = None
    desired_time_text: str | None = None


#: §7.1: where a client note is accepted -- the same set, for the same reason.
#: Not part of `ALLOWED` on purpose: a note is not a transition, and putting it
#: there would invite treating it like one.
NOTE_STATUSES = ACTIVE_STATUSES

#: A note is measured by §17's one cap for client free text, like every field
#: beside it. It had a cap of its own at 2000 and truncated to it in silence,
#: which is the wrong answer twice over: the client lost the end of what they
#: wrote, and one field disagreeing with the rest about how much a person may
#: say is a rule nobody could have predicted from the form.

#: §7.1, as data. Keeping it declarative means a rejected transition is a
#: lookup miss rather than a forgotten `elif`.
ALLOWED: dict[RequestStatus, frozenset[str]] = {
    RequestStatus.pending: frozenset({"admin_approve", "admin_propose", "admin_reject", "expire"}),
    RequestStatus.negotiating: frozenset(
        {
            "client_accept",
            "client_counter",
            # §7.1: a client who counters with a time has said what suits them,
            # and the therapist agreeing is an approval. Without this she had to
            # propose that same time back and wait to be accepted again -- and
            # both surfaces offered an Approve button that the core refused.
            "admin_approve",
            "admin_propose",
            "client_decline",
            "admin_reject",
        }
    ),
    RequestStatus.confirmed: frozenset({"admin_cancel", "complete"}),
    RequestStatus.rejected: frozenset(),
    RequestStatus.expired: frozenset(),
    RequestStatus.cancelled: frozenset(),
    RequestStatus.completed: frozenset(),
}


async def _enforce_submission_rate(session: AsyncSession, client_id: UUID) -> None:
    """§17: 5 submissions per hour per client.

    Every terminal status counts. A client who books and cancels five times in
    an hour is doing the thing the limit exists to stop, and only counting live
    requests would make the limit trivial to walk around.
    """
    from sqlalchemy import func

    recent = (
        await session.execute(
            select(func.count())
            .select_from(BookingRequest)
            .where(
                BookingRequest.client_id == client_id,
                BookingRequest.created_at >= now_utc() - SUBMISSION_WINDOW,
            )
        )
    ).scalar_one()
    if int(recent) >= SUBMISSIONS_PER_HOUR:
        raise RateLimited(f"{SUBMISSIONS_PER_HOUR} booking requests an hour is the limit")


async def _learn_display_name(
    session: AsyncSession, client_id: UUID, display_name: str | None
) -> None:
    """Fill `client.display_name` from the first submission that supplies one.

    It was only ever written when the client row was created (§6.2), which
    Telegram does from the profile and the web cannot do at all -- the address
    arrives before the name. So the web asked "what should I call you?" on every
    booking, threw the answer onto the request, and asked again next time.

    Never overwritten (§12.1). A later booking may carry a different name, but a
    typo on one request must not rename the person on every other.
    """
    if not display_name or not display_name.strip():
        return

    client = (
        await session.execute(select(Client).where(Client.id == client_id))
    ).scalar_one_or_none()
    if client is None or (client.display_name or "").strip():
        return

    client.display_name = display_name.strip()
    await session.flush()


def _guard(request: BookingRequest, event: str) -> None:
    """Refuse anything outside §7.1 before a single field is touched."""
    if event not in ALLOWED[request.status]:
        raise InvalidTransition("booking_request", request.status.value, event)


async def _get(session: AsyncSession, request_id: int) -> BookingRequest:
    request = (
        await session.execute(select(BookingRequest).where(BookingRequest.id == request_id))
    ).scalar_one_or_none()
    if request is None:
        raise NotFound(f"booking request {request_id}")
    return request


async def last_contact_note(session: AsyncSession, client_id: UUID) -> str | None:
    """The most recent contact note this client gave, if they ever gave one.

    §12.1 prefills step 3 with it. Unlike the name it stays on the request
    rather than moving to the client: "phone after six" is situational, and the
    prefill is a convenience rather than a fact about the person.
    """
    return (
        await session.execute(
            select(BookingRequest.contact_note)
            .where(
                BookingRequest.client_id == client_id,
                BookingRequest.contact_note.isnot(None),
                BookingRequest.contact_note != "",
            )
            .order_by(BookingRequest.created_at.desc(), BookingRequest.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def active_for_client(
    session: AsyncSession, client_id: UUID, *, limit: int = 3
) -> list[BookingRequest]:
    """§13.1 step 9: what this client currently has booked or pending.

    Newest first, because the one they just made is the one they are asking
    about. Terminal requests are history and are left out: a client looking for
    "my appointment" is not asking to be reminded of a rejection.
    """
    rows = (
        await session.execute(
            select(BookingRequest)
            .where(
                BookingRequest.client_id == client_id,
                BookingRequest.status.in_(ACTIVE_STATUSES),
            )
            .order_by(BookingRequest.created_at.desc(), BookingRequest.id.desc())
            .limit(limit)
        )
    ).scalars()
    return list(rows.all())


async def count_with_status(session: AsyncSession, *statuses: RequestStatus) -> int:
    """How many requests are in any of these statuses. §13.2's panel counts."""
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(BookingRequest)
                .where(BookingRequest.status.in_(statuses))
            )
        ).scalar_one()
    )


async def queue_for_admin(
    session: AsyncSession, *, limit: int = 5, offset: int = 0
) -> list[BookingRequest]:
    """§13.2: what is waiting on the therapist, newest first."""
    rows = (
        await session.execute(
            select(BookingRequest)
            .where(
                BookingRequest.status.in_((RequestStatus.pending, RequestStatus.negotiating))
            )
            .order_by(BookingRequest.created_at.desc(), BookingRequest.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return list(rows.all())


async def upcoming_sessions(
    session: AsyncSession, *, within: timedelta, limit: int = 10
) -> list[BookingRequest]:
    """§13.2: confirmed sessions from now to `within` from now, soonest first.

    Sorted the opposite way from the queue on purpose: a queue is answered
    newest first, a day is lived in order.
    """
    now = now_utc()
    rows = (
        await session.execute(
            select(BookingRequest)
            .where(
                BookingRequest.status == RequestStatus.confirmed,
                BookingRequest.scheduled_start >= now,
                BookingRequest.scheduled_start < now + within,
            )
            .order_by(BookingRequest.scheduled_start)
            .limit(limit)
        )
    ).scalars()
    return list(rows.all())


def _slot_still_belongs(request_id: Any) -> Any:
    """Whether the joined slot is actually this request's, as SQL.

    `booking_request.slot_id` records the slot the client *asked for*, and
    nothing clears it: a terminal transition releases the slot without
    forgetting which one it was, and a hold that lapses in an abandoned
    negotiation releases it too. So the column can name a slot that has since
    gone back on the picker and been taken by somebody else.

    Kept as one expression because three readers ask the same question, and a
    reader that forgot to ask drew a request in the week schedule at a time
    belonging to another client (§12.2).
    """
    return (Slot.held_by_request == request_id) | (Slot.booked_request == request_id)


async def scheduled_in_window(
    session: AsyncSession, *, window_from: datetime, window_to: datetime
) -> list[ScheduleEntry]:
    """§12.2's week schedule: everything with a time, in `[from, to)`.

    "With a time" means `requested_start` would answer: `scheduled_start` once
    approval has set it, the held slot's instant before that. Expressed as one
    `coalesce` so a week is one query -- the caller draws seven columns and must
    not go back to the database per row.

    `completed` belongs here with `confirmed`. The worker sweeps a session to it
    the moment its end passes (§14), so a schedule without it would render every
    past week empty, which is a false statement about a week that happened.
    """
    effective = func.coalesce(BookingRequest.scheduled_start, Slot.starts_at)
    rows = await session.execute(
        select(
            BookingRequest.uuid,
            BookingRequest.status,
            BookingRequest.display_name,
            effective.label("starts_at"),
        )
        .outerjoin(
            Slot,
            (Slot.id == BookingRequest.slot_id) & _slot_still_belongs(BookingRequest.id),
        )
        .where(
            BookingRequest.status.in_(SCHEDULE_STATUSES),
            effective >= window_from,
            effective < window_to,
        )
        .order_by(effective, BookingRequest.id)
    )
    return [
        ScheduleEntry(uuid=uuid, status=status, display_name=name, starts_at=starts_at)
        for uuid, status, name, starts_at in rows.all()
    ]


async def unscheduled_for_admin(session: AsyncSession, *, limit: int = 20) -> list[ScheduleEntry]:
    """§12.2: the requests the week schedule has no honest cell for.

    A free-text request carries wording rather than an instant -- "some evening
    next week?" -- so it has neither `scheduled_start` nor a slot, and belongs
    beside the grid rather than nowhere. These are the ones most in need of an
    answer; dropping them would hide exactly the wrong thing.

    A request whose slot lapsed out from under it lands here too, and must: the
    grid has no honest cell for it either, and this is the exact complement of
    what the grid draws. Between the two, every live request appears once.
    """
    rows = await session.execute(
        select(
            BookingRequest.uuid,
            BookingRequest.status,
            BookingRequest.display_name,
            BookingRequest.desired_time_text,
        )
        .outerjoin(
            Slot,
            (Slot.id == BookingRequest.slot_id) & _slot_still_belongs(BookingRequest.id),
        )
        .where(
            BookingRequest.status.in_(
                (RequestStatus.pending, RequestStatus.negotiating),
            ),
            BookingRequest.scheduled_start.is_(None),
            Slot.id.is_(None),
        )
        .order_by(BookingRequest.created_at.desc(), BookingRequest.id.desc())
        .limit(limit)
    )
    return [
        ScheduleEntry(uuid=uuid, status=status, display_name=name, desired_time_text=wanted)
        for uuid, status, name, wanted in rows.all()
    ]


async def taken_hours_on(
    session: AsyncSession, *, day: date, tz: str
) -> frozenset[int]:
    """§13.2: the local hours of `day` the therapist cannot be offered.

    A confirmed or completed session starting in the hour, or a slot in it she
    has already booked out or blocked off. A *held* slot is not taken: a hold
    lapses on its own and belongs to a client who has not finished a form.

    Bucketed by local wall-clock hour rather than by an offset from midnight,
    for the reason §12.2 gives about the week grid -- a day containing a DST
    transition is not twenty-four hours long, and arithmetic that assumes it is
    silently moves everything after the boundary.
    """
    zone = ZoneInfo(tz)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)

    sessions = (
        await session.execute(
            select(BookingRequest.scheduled_start).where(
                BookingRequest.status.in_((RequestStatus.confirmed, RequestStatus.completed)),
                BookingRequest.scheduled_start >= start,
                BookingRequest.scheduled_start < end,
            )
        )
    ).scalars()

    blocked = (
        await session.execute(
            select(Slot.starts_at).where(
                Slot.status.in_((SlotStatus.booked, SlotStatus.blocked)),
                Slot.starts_at >= start,
                Slot.starts_at < end,
            )
        )
    ).scalars()

    return frozenset(
        instant.astimezone(zone).hour
        for instant in (*sessions.all(), *blocked.all())
        if instant is not None
    )


async def requested_start(session: AsyncSession, request: BookingRequest) -> datetime | None:
    """The instant this request is about.

    `scheduled_start` is only set at approval (§7.1), so before that the answer
    is the slot being held. A free-text request has neither, and its wording
    lives in `desired_time_text` -- the caller decides what to do with that.
    """
    if request.scheduled_start is not None:
        return request.scheduled_start
    if request.slot_id is None:
        return None
    # Only while the slot is still this request's: a hold that lapsed in an
    # abandoned negotiation leaves the column pointing at a slot the practice
    # has since offered to somebody else.
    return (
        await session.execute(
            select(Slot.starts_at).where(
                Slot.id == request.slot_id, _slot_still_belongs(request.id)
            )
        )
    ).scalar_one_or_none()


async def get_by_uuid(session: AsyncSession, request_uuid: UUID) -> BookingRequest:
    """`uuid` is the client-visible identifier; internal ids never leave."""
    request = (
        await session.execute(select(BookingRequest).where(BookingRequest.uuid == request_uuid))
    ).scalar_one_or_none()
    if request is None:
        raise NotFound(f"booking request {request_uuid}")
    return request


async def _audit(
    session: AsyncSession,
    request: BookingRequest,
    actor: ActorType,
    action: str,
    meta: dict[str, Any] | None = None,
) -> None:
    """Append an audit row.

    `meta` MUST NOT carry problem_text or negotiation bodies (hard rule 8).
    Log identifiers; the content stays in the admin UI.
    """
    session.add(
        AuditLog(
            practice_id=request.practice_id,
            actor_type=actor,
            action=action,
            entity_type="booking_request",
            entity_id=str(request.uuid),
            meta=meta,
        )
    )


async def _release_slot(session: AsyncSession, request: BookingRequest) -> None:
    """Every terminal transition releases whatever the request was holding, in
    the same transaction (§7.1)."""
    if request.slot_id is not None:
        await slot_service.release_slot(session, request.slot_id)


async def _create_reminders(session: AsyncSession, request: BookingRequest) -> list[Reminder]:
    """Explicit rows per configured offset (DESIGN.md §13).

    One whose due time has already passed is created `skipped`, not fired late.
    """
    practice = await get_practice(session)
    assert request.scheduled_start is not None  # guaranteed by the caller
    created = []
    for offset, due_at, already_past in reminder_schedule(practice, request.scheduled_start):
        reminder = Reminder(
            request_id=request.id,
            offset_min=offset,
            due_at=due_at,
            state=ReminderState.skipped if already_past else ReminderState.scheduled,
        )
        session.add(reminder)
        created.append(reminder)
    await session.flush()
    return created


async def _cancel_reminders(session: AsyncSession, request: BookingRequest) -> None:
    """Pending reminders die with the booking. Rows rather than booleans is
    what makes this expressible at all."""
    scheduled = (
        (
            await session.execute(
                select(Reminder).where(
                    Reminder.request_id == request.id,
                    Reminder.state == ReminderState.scheduled,
                )
            )
        )
        .scalars()
        .all()
    )
    for reminder in scheduled:
        reminder.state = ReminderState.cancelled
    await session.flush()


async def _last_admin_proposal(session: AsyncSession, request_id: int) -> NegotiationMessage | None:
    return (
        await session.execute(
            select(NegotiationMessage)
            .where(
                NegotiationMessage.request_id == request_id,
                NegotiationMessage.sender == SenderType.admin,
                NegotiationMessage.kind == NegotiationKind.proposal,
            )
            .order_by(NegotiationMessage.created_at.desc(), NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _slot(session: AsyncSession, slot_id: int | None) -> Slot | None:
    if slot_id is None:
        return None
    return (await session.execute(select(Slot).where(Slot.id == slot_id))).scalar_one_or_none()


async def _keep_hold_alive(session: AsyncSession, request: BookingRequest) -> None:
    """Push the held slot's expiry out while the conversation is still going.

    §7.1 expires only `pending`, so a negotiation has no expiry of its own and
    the hold inherited from submission would lapse in the middle of one --
    putting the time under discussion back on the picker with the request still
    pointing at it (§7.2).

    Every negotiation move that *keeps* the slot calls this, not just the rare
    one that proposes the very time being held. A words-only proposal -- what a
    mistyped time produces -- keeps the slot precisely because it has not moved
    away from it, and so has the most need of this.
    """
    if request.slot_id is None:
        return
    practice = await get_practice(session)
    await slot_service.extend_hold(session, request.slot_id, pending_expiry(practice))


async def _last_proposed_instant(session: AsyncSession, request_id: int) -> datetime | None:
    """The most recent time either side put forward, if either did (§7.1).

    Deliberately not restricted to the therapist's own proposals: the case this
    exists for is her approving the time the *client* countered with.
    """
    return (
        await session.execute(
            select(NegotiationMessage.proposed_start)
            .where(
                NegotiationMessage.request_id == request_id,
                NegotiationMessage.proposed_start.isnot(None),
            )
            .order_by(NegotiationMessage.created_at.desc(), NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def whose_turn(session: AsyncSession, request_id: int) -> SenderType | None:
    """Derived from the last message's sender, never stored (§6.6).

    Only the two senders who have a turn are considered. Nothing writes a
    `system` message today, but the flip below is an admin/client either-or:
    one system row would silently hand the turn to the admin and leave the
    client unable to answer.
    """
    last = (
        await session.execute(
            select(NegotiationMessage)
            .where(
                NegotiationMessage.request_id == request_id,
                NegotiationMessage.sender.in_((SenderType.admin, SenderType.client)),
            )
            .order_by(NegotiationMessage.created_at.desc(), NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None:
        return None
    return SenderType.client if last.sender == SenderType.admin else SenderType.admin


# --- Submission -------------------------------------------------------------


async def _submit(
    session: AsyncSession,
    *,
    client_id: UUID,
    session_type_id: int,
    modality: Modality,
    source_channel: Channel,
    slot_id: int | None,
    desired_time_text: str | None,
    problem_text: str | None,
    contact_note: str | None,
    display_name: str | None,
    client_timezone: str | None,
) -> BookingRequest:
    practice = await get_practice(session)
    if not practice.availability_on:
        raise BookingClosed("the practice is not accepting bookings")

    check_client_text(problem_text, "problem_text")
    check_client_text(contact_note, "contact_note")
    check_client_text(desired_time_text, "desired_time_text")

    await _enforce_submission_rate(session, client_id)

    session_type = (
        await session.execute(select(SessionType).where(SessionType.id == session_type_id))
    ).scalar_one_or_none()
    if session_type is None or not session_type.is_active:
        raise NotFound(f"session type {session_type_id}")

    await _learn_display_name(session, client_id, display_name)

    request = BookingRequest(
        practice_id=practice.id,
        client_id=client_id,
        session_type_id=session_type_id,
        modality=modality,
        status=RequestStatus.pending,
        source_channel=source_channel,
        slot_id=slot_id,
        desired_time_text=desired_time_text,
        problem_text=problem_text,
        contact_note=contact_note,
        display_name=display_name,
        client_timezone=client_timezone,
        expires_at=pending_expiry(practice),
    )
    session.add(request)
    await session.flush()

    await _audit(session, request, ActorType.client, "request.submit")
    collect(session, RequestSubmitted(request_id=request.id, request_uuid=request.uuid))
    return request


async def submit_slot_request(
    session: AsyncSession,
    *,
    client_id: UUID,
    slot_id: int,
    session_type_id: int,
    modality: Modality,
    source_channel: Channel,
    problem_text: str | None = None,
    contact_note: str | None = None,
    display_name: str | None = None,
    client_timezone: str | None = None,
) -> BookingRequest:
    """Submit against a chosen slot.

    The slot is held, not booked: the therapist approves every booking unless
    `auto_confirm_slots` is on, and holding is what stops a second client
    picking it meanwhile.
    """
    practice = await get_practice(session)
    request = await _submit(
        session,
        client_id=client_id,
        session_type_id=session_type_id,
        modality=modality,
        source_channel=source_channel,
        slot_id=slot_id,
        desired_time_text=None,
        problem_text=problem_text,
        contact_note=contact_note,
        display_name=display_name,
        client_timezone=client_timezone,
    )

    # The hold lasts as long as the request can stay undecided, not
    # `slot_hold_minutes`: this request is submitted, so the window it needs is
    # the therapist's, not the client's form-filling one (§7.2).
    slot = await slot_service.hold_slot(session, slot_id, request.id, until=request.expires_at)

    if practice.auto_confirm_slots:
        # One line of policy, off by default: keeping the therapist in the loop
        # is the point of the product (DESIGN.md §6).
        return await admin_approve(session, request.id, scheduled_start=slot.starts_at)
    return request


async def submit_free_time_request(
    session: AsyncSession,
    *,
    client_id: UUID,
    session_type_id: int,
    modality: Modality,
    desired_time_text: str,
    source_channel: Channel,
    problem_text: str | None = None,
    contact_note: str | None = None,
    display_name: str | None = None,
    client_timezone: str | None = None,
) -> BookingRequest:
    """Submit a free-text desired time.

    "some evening next week?" is a normal thing for a client to say; forcing it
    into a datetime picker would be worse than storing the sentence
    (DESIGN.md §9).
    """
    practice = await get_practice(session)
    if not practice.negotiation_enabled:
        raise NegotiationDisabled("free-time requests are switched off")

    return await _submit(
        session,
        client_id=client_id,
        session_type_id=session_type_id,
        modality=modality,
        source_channel=source_channel,
        slot_id=None,
        desired_time_text=desired_time_text,
        problem_text=problem_text,
        contact_note=contact_note,
        display_name=display_name,
        client_timezone=client_timezone,
    )


# --- Admin transitions ------------------------------------------------------


async def admin_approve(
    session: AsyncSession,
    request_id: int,
    *,
    scheduled_start: datetime | None = None,
    meeting_url: str | None = None,
) -> BookingRequest:
    """pending -> confirmed."""
    request = await _get(session, request_id)
    _guard(request, "admin_approve")

    # What is being confirmed, in the order of how sure we are of it. The slot
    # is the answer only while nobody has discussed anything else: once a
    # proposal exists, the slot is what the client asked for and the
    # negotiation is what they settled on (§7.1).
    start = scheduled_start
    if start is None:
        start = await _last_proposed_instant(session, request.id)
    if start is None and request.status is RequestStatus.pending:
        held = await _slot(session, request.slot_id)
        start = held.starts_at if held is not None else None
    if start is None:
        # A negotiation that never named an instant has nothing to confirm. The
        # slot is *not* a fallback here: the conversation moved to words, and
        # confirming the client's original pick would tell them a time they
        # never agreed to. Only she can turn "thursday evening" into one.
        raise InvalidTransition("booking_request", request.status.value, "admin_approve")

    # A slot is the booking only while it is the time. A negotiation that
    # settled on another one leaves it for somebody else, rather than marking it
    # booked for a session that is not at it.
    if request.slot_id is not None:
        held = await _slot(session, request.slot_id)
        if held is not None and held.starts_at == start:
            await slot_service.book_slot(session, request.slot_id, request.id)
        else:
            await slot_service.release_slot(session, request.slot_id)
            request.slot_id = None

    if request.slot_id is None:
        # The agreed time may happen to be a slot the practice offers; if it is,
        # take it off the picker rather than leave it double-bookable.
        matching = await _matching_slot(session, start)
        if matching is not None:
            await slot_service.book_slot(session, matching, request.id)
            request.slot_id = matching

    session_type = (
        await session.execute(select(SessionType).where(SessionType.id == request.session_type_id))
    ).scalar_one()

    request.status = RequestStatus.confirmed
    request.scheduled_start = start
    request.scheduled_duration_min = session_type.duration_min
    request.confirmed_at = now_utc()
    if meeting_url is not None:
        request.meeting_url = meeting_url
    await session.flush()

    await _create_reminders(session, request)
    await _audit(session, request, ActorType.admin, "request.confirm")
    collect(
        session,
        RequestConfirmed(request_id=request.id, request_uuid=request.uuid, scheduled_start=start),
    )
    return request


async def admin_propose(
    session: AsyncSession,
    request_id: int,
    *,
    proposed_start: datetime | None = None,
    body_text: str | None = None,
) -> BookingRequest:
    """pending|negotiating -> negotiating."""
    request = await _get(session, request_id)
    _guard(request, "admin_propose")

    practice = await get_practice(session)
    if not practice.negotiation_enabled:
        raise NegotiationDisabled("negotiation is switched off")

    # Releasing when the proposal names a different time (§7.1): holding a slot
    # the therapist has just proposed moving away from would keep it off the
    # picker for no reason.
    #
    # The comparison MUST come before the release, not after. Releasing first
    # and then discovering the proposal named the very slot being held puts that
    # slot back on the picker while the request still points at it, and a second
    # client can take it out from under the negotiation.
    if request.slot_id is not None and proposed_start is not None:
        held = (
            await session.execute(select(Slot).where(Slot.id == request.slot_id))
        ).scalar_one_or_none()
        if held is None or held.starts_at != proposed_start:
            await slot_service.release_slot(session, request.slot_id)
            request.slot_id = None

    # Whatever slot is still held after that, the conversation keeps alive.
    await _keep_hold_alive(session, request)

    session.add(
        NegotiationMessage(
            request_id=request.id,
            sender=SenderType.admin,
            kind=NegotiationKind.proposal,
            proposed_start=proposed_start,
            body_text=body_text,
        )
    )
    request.status = RequestStatus.negotiating
    await session.flush()

    await _audit(session, request, ActorType.admin, "request.propose")
    collect(
        session,
        RequestProposal(
            request_id=request.id,
            request_uuid=request.uuid,
            proposed_start=proposed_start,
            note=body_text,
        ),
    )
    return request


async def admin_reject(
    session: AsyncSession, request_id: int, *, reason: str | None = None
) -> BookingRequest:
    """pending|negotiating -> rejected."""
    request = await _get(session, request_id)
    _guard(request, "admin_reject")

    await _release_slot(session, request)
    request.status = RequestStatus.rejected
    request.rejected_reason = reason
    await session.flush()

    await _audit(session, request, ActorType.admin, "request.reject")
    collect(
        session,
        RequestRejected(request_id=request.id, request_uuid=request.uuid, by=ActorType.admin),
    )
    return request


async def admin_cancel(
    session: AsyncSession, request_id: int, *, reason: str | None = None
) -> BookingRequest:
    """confirmed -> cancelled.

    Not optional and not gated on `cancel_window_hours`: without it a confirmed
    booking has no exit that releases its slot, and the therapist has no
    recourse when she is ill (DESIGN.md §14).
    """
    request = await _get(session, request_id)
    _guard(request, "admin_cancel")

    scheduled_start = request.scheduled_start
    await _release_slot(session, request)
    await _cancel_reminders(session, request)

    request.status = RequestStatus.cancelled
    request.cancelled_at = now_utc()
    request.cancelled_by = ActorType.admin
    request.cancellation_reason = reason
    await session.flush()

    await _audit(session, request, ActorType.admin, "request.cancel")
    collect(
        session,
        RequestCancelled(
            request_id=request.id,
            request_uuid=request.uuid,
            scheduled_start=scheduled_start,
        ),
    )
    return request


# --- Client transitions -----------------------------------------------------


async def client_accept(session: AsyncSession, request_id: int) -> BookingRequest:
    """negotiating -> confirmed, at the last time the admin proposed.

    Or, when that proposal named no instant, negotiating -> negotiating: §7.1
    lets a proposal be words only, and agreeing to words confirms nothing --
    there is no `scheduled_start` to set, no reminder to schedule and nothing
    for the schedule to draw. Refusing the client outright was worse: the one
    person who could move it forward was told nothing, and the client's tap did
    nothing visible. So the agreement is recorded and the therapist is asked to
    put a time to it.
    """
    request = await _get(session, request_id)
    _guard(request, "client_accept")

    proposal = await _last_admin_proposal(session, request.id)
    if proposal is None:
        raise InvalidTransition("booking_request", request.status.value, "client_accept")

    session.add(
        NegotiationMessage(
            request_id=request.id, sender=SenderType.client, kind=NegotiationKind.accept
        )
    )

    if proposal.proposed_start is None:
        await session.flush()
        await _audit(session, request, ActorType.client, "request.accept")
        collect(
            session,
            RequestAccepted(
                request_id=request.id, request_uuid=request.uuid, note=proposal.body_text
            ),
        )
        return request

    matching = await _matching_slot(session, proposal.proposed_start)
    if matching is not None:
        await slot_service.book_slot(session, matching, request.id)
        request.slot_id = matching

    session_type = (
        await session.execute(select(SessionType).where(SessionType.id == request.session_type_id))
    ).scalar_one()

    request.status = RequestStatus.confirmed
    request.scheduled_start = proposal.proposed_start
    request.scheduled_duration_min = session_type.duration_min
    request.confirmed_at = now_utc()
    await session.flush()

    await _create_reminders(session, request)
    await _audit(session, request, ActorType.client, "request.accept")
    collect(
        session,
        RequestConfirmed(
            request_id=request.id,
            request_uuid=request.uuid,
            scheduled_start=proposal.proposed_start,
        ),
    )
    return request


async def _matching_slot(session: AsyncSession, starts_at: datetime) -> int | None:
    """An available slot at exactly this instant, if the practice offers one.

    Locked, because the caller books what this returns. Read without the lock,
    two clients accepting proposals for the same instant could both be handed
    the same slot id; the second `book_slot` then raised `SlotUnavailable` and
    the client saw a generic error for a booking that was about to succeed on
    retry. Postgres re-checks the predicate after taking the lock, so a slot
    taken in the meantime comes back as None -- and confirming without a slot
    is a case the negotiation path already allows (§7.1).
    """
    from app.core.enums import SlotStatus
    from app.core.models import Slot

    return (
        await session.execute(
            select(Slot.id)
            .where(Slot.starts_at == starts_at, Slot.status == SlotStatus.available)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def client_counter(
    session: AsyncSession,
    request_id: int,
    *,
    proposed_start: datetime | None = None,
    body_text: str | None = None,
) -> BookingRequest:
    """negotiating -> negotiating.

    The slot is deliberately *not* released: a client suggesting an alternative
    has not given up on the one they picked, and the therapist may still approve
    it. The hold is kept alive instead -- a client still talking is a client
    still interested.
    """
    request = await _get(session, request_id)
    _guard(request, "client_counter")

    session.add(
        NegotiationMessage(
            request_id=request.id,
            sender=SenderType.client,
            kind=NegotiationKind.counter,
            proposed_start=proposed_start,
            body_text=body_text,
        )
    )
    await _keep_hold_alive(session, request)
    await session.flush()

    await _audit(session, request, ActorType.client, "request.counter")
    collect(
        session,
        RequestCounter(
            request_id=request.id,
            request_uuid=request.uuid,
            proposed_start=proposed_start,
            note=body_text,
        ),
    )
    return request


async def client_note(
    session: AsyncSession, request_id: int, *, body_text: str
) -> BookingRequest:
    """§7.1: the client adds information. The status does not move.

    Allowed while the therapist can still act on what it says, and refused once
    the request is terminal -- a note on a rejected request would be read by
    nobody.

    Over-long text is refused, not shortened. This cut at 2000 characters and
    told nobody: not the client, whose last paragraph vanished, and not the
    therapist, who had no way to know she was reading part of a sentence. §17
    caps every client free-text field at the same 4096, so a note is now
    measured the way `problem_text` beside it always was.
    """
    request = await _get(session, request_id)
    if request.status not in NOTE_STATUSES:
        raise InvalidTransition("booking_request", request.status.value, "client_note")

    body = check_client_text(body_text.strip(), "note")
    if not body:
        raise InvalidTransition("booking_request", request.status.value, "client_note")

    session.add(
        NegotiationMessage(
            request_id=request.id,
            sender=SenderType.client,
            kind=NegotiationKind.note,
            body_text=body,
        )
    )
    await session.flush()

    # Hard rule 8: the identifier, never the content -- the audit log is not a
    # place for what somebody wrote. The event carries it, because a
    # notification is where it is meant to be read (§10).
    await _audit(session, request, ActorType.client, "request.note")
    collect(session, RequestNote(request_id=request.id, request_uuid=request.uuid, note=body))
    return request


async def client_decline(
    session: AsyncSession, request_id: int, *, to_waitlist: bool = False
) -> BookingRequest:
    """negotiating -> rejected.

    The event says the client did it, which is the difference between something
    she should hear about and her own rejection read back to her (§12.1). Both
    sides emitted this identically, so a client walking away from a negotiation
    reached her as silence.
    """
    request = await _get(session, request_id)
    _guard(request, "client_decline")

    session.add(
        NegotiationMessage(
            request_id=request.id, sender=SenderType.client, kind=NegotiationKind.decline
        )
    )
    await _release_slot(session, request)
    request.status = RequestStatus.rejected
    await session.flush()

    await _audit(session, request, ActorType.client, "request.decline")
    collect(
        session,
        RequestRejected(
            request_id=request.id,
            request_uuid=request.uuid,
            by=ActorType.client,
            to_waitlist=to_waitlist,
        ),
    )
    return request


async def client_decline_to_waitlist(
    session: AsyncSession, request_id: int
) -> tuple[BookingRequest, Any]:
    """§12.1's way out: close the negotiation and ask to be told when something
    opens.

    One use-case rather than two calls from a route, because §7.1's transition
    and the waitlist entry have to succeed or fail together -- a route that did
    both would be a scheduling rule living in a channel, which is the drift the
    whole architecture exists to prevent.

    Not a new transition. It *is* `client_decline`; the entry is what the
    client gets in exchange for it, and the request's own words go with them so
    the therapist is not handed an anonymous line on the waitlist page.
    """
    from app.core.services import waitlist as waitlist_service

    request = await _get(session, request_id)
    _guard(request, "client_decline")

    problem_text, contact_note = request.problem_text, request.contact_note
    declined = await client_decline(session, request_id, to_waitlist=True)
    entry = await waitlist_service.join_waitlist(
        session,
        client_id=request.client_id,
        problem_text=problem_text,
        contact_note=contact_note,
    )
    return declined, entry


# --- Worker transitions -----------------------------------------------------


async def expire_request(session: AsyncSession, request_id: int) -> BookingRequest:
    """pending -> expired. Only `pending` expires (DESIGN.md §9)."""
    request = await _get(session, request_id)
    _guard(request, "expire")

    await _release_slot(session, request)
    request.status = RequestStatus.expired
    await session.flush()

    await _audit(session, request, ActorType.system, "request.expire")
    collect(session, RequestExpired(request_id=request.id, request_uuid=request.uuid))
    return request


async def complete_request(session: AsyncSession, request_id: int) -> BookingRequest:
    """confirmed -> completed, once the end time has passed.

    Set by the worker, not by anyone clicking anything, and deliberately silent
    -- no notification (§7.1).
    """
    request = await _get(session, request_id)
    _guard(request, "complete")

    await _release_slot(session, request)
    request.status = RequestStatus.completed
    await session.flush()

    await _audit(session, request, ActorType.system, "request.complete")
    return request
