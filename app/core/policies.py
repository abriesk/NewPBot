"""Booking policies (IMPLEMENTATION.md §8, DESIGN.md §6).

Pure functions over a `Practice` row and a clock. No database access, no I/O --
which is what makes every branch of the §6 matrix testable without fixtures.

Every instant here is timezone-aware UTC. `datetime.utcnow()` must never appear
(hard rule 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.core.enums import BookingMode
from app.core.errors import TextTooLong
from app.core.models import Practice

#: The longest free text a client can send.
#:
#: Telegram caps a message at 4096 characters and the problem description
#: arrives as exactly one message, so this ceiling already applied on that
#: channel whether anybody had written it down or not. The web had none at all.
#: The number is Telegram's rather than a taste of ours, because a limit the web
#: sets lower would accept text the bot could not carry, and one set higher
#: would take an essay on one channel and refuse it on the other.
CLIENT_TEXT_MAX_CHARS = 4096


def check_client_text(value: str | None, field: str) -> str | None:
    """Refuse over-long client text. Returns the value so callers can inline it."""
    if value is not None and len(value) > CLIENT_TEXT_MAX_CHARS:
        raise TextTooLong(f"{field} is longer than {CLIENT_TEXT_MAX_CHARS} characters")
    return value


class BookingPath(StrEnum):
    """What a client is offered when they ask for a consultation."""

    slots = "slots"
    negotiation = "negotiation"
    waitlist = "waitlist"


@dataclass(frozen=True, slots=True)
class BookingModeResult:
    path: BookingPath
    #: Why this path, for the audit log and for admin diagnostics. Not shown to
    #: clients -- their wording is a translation key.
    reason: str


def now_utc() -> datetime:
    return datetime.now(UTC)


def resolve_booking_mode(practice: Practice, *, slots_exist: bool) -> BookingModeResult:
    """Implement the matrix in DESIGN.md §6.

    | availability_on | booking_mode | slots exist | result                     |
    |-----------------|--------------|-------------|----------------------------|
    | false           | -            | -           | waitlist                   |
    | true            | slots        | yes         | slot picker                |
    | true            | slots        | no          | negotiation if fallback,   |
    |                 |              |             | else waitlist              |
    | true            | negotiation  | -           | free-text time request     |

    INTERPRETATION worth confirming: the matrix does not mention
    `negotiation_enabled`, but offering a client a free-text time request when
    the therapist has switched negotiation off would produce a request nobody
    can answer. Both negotiation outcomes are therefore gated on it, and fall
    back to the waitlist. The three settings stay orthogonal in the sense
    DESIGN.md §6 cares about -- availability, mode, and slot inventory are still
    independent.
    """
    if not practice.availability_on:
        return BookingModeResult(BookingPath.waitlist, "availability_off")

    if practice.booking_mode == BookingMode.negotiation:
        if not practice.negotiation_enabled:
            return BookingModeResult(BookingPath.waitlist, "negotiation_disabled")
        return BookingModeResult(BookingPath.negotiation, "booking_mode_negotiation")

    if slots_exist:
        return BookingModeResult(BookingPath.slots, "slots_available")

    if practice.fallback_to_negotiation and practice.negotiation_enabled:
        return BookingModeResult(BookingPath.negotiation, "no_slots_fallback")

    return BookingModeResult(BookingPath.waitlist, "no_slots_no_fallback")


def hold_expiry(practice: Practice, *, at: datetime | None = None) -> datetime:
    """When a slot hold lapses.

    The hold exists to stop two clients picking the same slot while one is
    still typing their problem description (DESIGN.md §8).
    """
    return (at or now_utc()) + timedelta(minutes=practice.slot_hold_minutes)


def pending_expiry(practice: Practice, *, at: datetime | None = None) -> datetime:
    """When an unanswered `pending` request lapses.

    Only `pending` expires. A `negotiating` request stays open until someone
    closes it -- an automatic close would fire on exactly the clients who are
    slowest to reply (DESIGN.md §9).
    """
    return (at or now_utc()) + timedelta(hours=practice.pending_expiry_hours)


def reminder_schedule(
    practice: Practice, scheduled_start: datetime, *, at: datetime | None = None
) -> list[tuple[int, datetime, bool]]:
    """`(offset_min, due_at, is_in_the_past)` for each configured offset.

    An empty `reminder_offsets_min` disables reminders. A reminder whose due
    time has already passed when the request is confirmed is marked `skipped`,
    not fired late (DESIGN.md §13) -- the caller uses the third element to
    decide that.
    """
    moment = at or now_utc()
    schedule = []
    for offset in sorted(set(practice.reminder_offsets_min), reverse=True):
        due_at = scheduled_start - timedelta(minutes=offset)
        schedule.append((offset, due_at, due_at <= moment))
    return schedule


def is_within_cancellation_window(
    practice: Practice, scheduled_start: datetime, *, at: datetime | None = None
) -> bool:
    """Whether `scheduled_start` is far enough out to cancel under the practice
    policy.

    Therapist-initiated cancellation ignores this -- she has to be able to
    cancel when she is ill. It exists for the client-initiated cancellation
    deferred in DESIGN.md §14, so that enabling it later is a UI surface and a
    policy call rather than a schema change.
    """
    return scheduled_start - (at or now_utc()) >= timedelta(hours=practice.cancel_window_hours)
