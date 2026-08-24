"""Domain exceptions.

These are the vocabulary the core uses to refuse. Adapters translate them into
whatever their channel understands -- an HTTP 409, a Telegram message, a form
error -- but the decision to refuse is made here and only here.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base for everything the domain raises deliberately."""


class NotFound(DomainError):
    """A referenced entity does not exist."""


class InvalidTransition(DomainError):
    """A state machine was asked for a move that is not in its table (§7).

    The transition MUST change nothing. Raising this after a partial mutation
    is a bug: the caller's transaction may well be rolled back, but the domain
    object in memory would be left inconsistent.
    """

    def __init__(self, entity: str, from_state: str, event: str) -> None:
        super().__init__(f"{entity}: {event!r} is not allowed from {from_state!r}")
        self.entity = entity
        self.from_state = from_state
        self.event = event


class SlotUnavailable(DomainError):
    """The slot is not in a state that allows the requested reservation.

    Raised after the row lock, so it means genuinely taken -- not a race the
    caller should retry.
    """


class SlotInThePast(DomainError):
    """§7.2 guards holding on `starts_at > now()`."""


class BookingClosed(DomainError):
    """`availability_on` is off; the client gets the waitlist instead (§6)."""


class NegotiationDisabled(DomainError):
    """The therapist has switched the negotiation feature off."""


class TokenInvalid(DomainError):
    """Unknown, already used, expired, or issued for a different purpose.

    Deliberately one exception for all four: telling a caller which of them
    applies leaks whether a token existed.
    """


class RateLimited(DomainError):
    """§17's limits. The caller has had their allowance for the window.

    A domain error rather than an HTTP concern, because the booking limit is
    per *client* and applies on every channel -- a limit only the web enforced
    would be no limit at all.
    """
