"""Domain events.

The core emits **intents**, not messages (DESIGN.md §3.2): a semantic key, a
payload, and -- decided later, by the renderer -- a set of available actions.
Nothing here knows what a Telegram button or an email subject line looks like.

Events are collected on the session and drained by the notification service
(M4), which writes outbox rows inside the same transaction as the domain change
that produced them. That is what makes "a confirmed booking and its notification
either both happen or neither does" true (hard rule 2).

Payloads carry identifiers, never rendered text, and never `problem_text`
(hard rule 8) -- the renderer looks up what it needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: Key under which pending events live on `AsyncSession.info`. Threading a
#: collector through every signature would contradict §8, which shows none.
_SESSION_KEY = "domain_events"


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base. `intent_key` matches the catalogue in §10."""

    intent_key: str = field(init=False, default="")


@dataclass(frozen=True, slots=True)
class RequestSubmitted(DomainEvent):
    request_id: int
    request_uuid: UUID
    intent_key: str = field(init=False, default="request.submitted")


@dataclass(frozen=True, slots=True)
class RequestConfirmed(DomainEvent):
    request_id: int
    request_uuid: UUID
    scheduled_start: datetime
    intent_key: str = field(init=False, default="request.confirmed")


@dataclass(frozen=True, slots=True)
class RequestProposal(DomainEvent):
    request_id: int
    request_uuid: UUID
    proposed_start: datetime | None
    #: What the therapist wrote alongside it. §10 puts it in the client's
    #: message: it is addressed to them, and a proposal of words only is
    #: nothing else (§7.1).
    note: str | None = None
    intent_key: str = field(init=False, default="request.proposal")


@dataclass(frozen=True, slots=True)
class RequestAccepted(DomainEvent):
    """The client agreed to a proposal that named no instant (§7.1).

    Not a confirmation: nothing can be scheduled from words. It exists so the
    therapist hears the agreement and can put a time to it.
    """

    request_id: int
    request_uuid: UUID
    note: str | None = None
    intent_key: str = field(init=False, default="request.accepted")


@dataclass(frozen=True, slots=True)
class RequestCounter(DomainEvent):
    request_id: int
    request_uuid: UUID
    proposed_start: datetime | None
    intent_key: str = field(init=False, default="request.counter")


@dataclass(frozen=True, slots=True)
class RequestNote(DomainEvent):
    """§7.1: the client added information. Not a transition -- the status is
    whatever it was. The body is not carried: it stays in the admin UI."""

    request_id: int
    request_uuid: UUID
    intent_key: str = field(init=False, default="request.note")


@dataclass(frozen=True, slots=True)
class RequestRejected(DomainEvent):
    request_id: int
    request_uuid: UUID
    intent_key: str = field(init=False, default="request.rejected")


@dataclass(frozen=True, slots=True)
class RequestExpired(DomainEvent):
    request_id: int
    request_uuid: UUID
    intent_key: str = field(init=False, default="request.expired")


@dataclass(frozen=True, slots=True)
class RequestCancelled(DomainEvent):
    request_id: int
    request_uuid: UUID
    scheduled_start: datetime | None
    intent_key: str = field(init=False, default="request.cancelled")


@dataclass(frozen=True, slots=True)
class WaitlistJoined(DomainEvent):
    entry_id: int
    entry_uuid: UUID
    intent_key: str = field(init=False, default="waitlist.joined")


def collect(session: AsyncSession, event: DomainEvent) -> None:
    """Queue an event for the notification service to turn into outbox rows."""
    session.info.setdefault(_SESSION_KEY, []).append(event)


def pending(session: AsyncSession) -> list[DomainEvent]:
    """Look at the queue without consuming it. Mostly for tests."""
    events: list[DomainEvent] = session.info.get(_SESSION_KEY, [])
    return list(events)


def drain(session: AsyncSession) -> list[DomainEvent]:
    """Take the queued events and clear it."""
    events: list[DomainEvent] = session.info.get(_SESSION_KEY, [])
    session.info[_SESSION_KEY] = []
    return events
