"""Domain enumerations (IMPLEMENTATION.md §5).

Almost every one is persisted as a PostgreSQL *native* enum type, with the
lowercase **values** stored -- not the Python member names. `pg_enum` below wires
`values_callable` to guarantee that; forgetting it silently persists 'PENDING'
where the schema says 'pending'.

`RequestType` from v1.0 deliberately does not exist. Session types are rows
(§6.4) so that adding "supervision" is an insert, not a migration and a deploy.

`CheckState` is the exception to the first paragraph: it is written to the
status file (§16.8), never to a column.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum, StrEnum
from typing import Any

from sqlalchemy import Enum as SAEnum


class Channel(StrEnum):
    telegram = "telegram"
    email = "email"
    web = "web"
    # Declared, unimplemented. Present so the outbox and identity tables do not
    # need a migration when the adapter lands.
    whatsapp = "whatsapp"


class Modality(StrEnum):
    online = "online"
    onsite = "onsite"


class RequestStatus(StrEnum):
    pending = "pending"
    negotiating = "negotiating"
    confirmed = "confirmed"
    rejected = "rejected"
    expired = "expired"
    cancelled = "cancelled"
    completed = "completed"


class SlotStatus(StrEnum):
    available = "available"
    held = "held"
    booked = "booked"
    blocked = "blocked"


class NegotiationKind(StrEnum):
    proposal = "proposal"
    counter = "counter"
    accept = "accept"
    decline = "decline"
    note = "note"


class SenderType(StrEnum):
    admin = "admin"
    client = "client"
    system = "system"


class WaitlistStatus(StrEnum):
    new = "new"
    contacted = "contacted"
    converted = "converted"
    closed = "closed"


class BookingMode(StrEnum):
    slots = "slots"
    negotiation = "negotiation"


class TokenPurpose(StrEnum):
    login = "login"
    link_channel = "link_channel"
    view_request = "view_request"


class OutboxStatus(StrEnum):
    pending = "pending"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    dead = "dead"


class ReminderState(StrEnum):
    scheduled = "scheduled"
    sent = "sent"
    skipped = "skipped"
    cancelled = "cancelled"


class ActorType(StrEnum):
    admin = "admin"
    client = "client"
    system = "system"


class ContentBlockKind(StrEnum):
    text = "text"
    link_button = "link_button"


class ErrorSource(StrEnum):
    """Which process caught the exception (§6.9)."""

    web = "web"
    worker = "worker"


class CheckState(StrEnum):
    """One health check's verdict (§16.8).

    Not persisted anywhere -- it is written to the status file, never to a
    column -- so it is deliberately absent from PG_ENUM_NAMES below and needs
    no migration.

    `ok`, `warn` and `fail` rather than colour names: green, amber and red are
    how a template renders these, and a domain that knows about colours is a
    domain that has to change when the design does.
    """

    ok = "ok"
    warn = "warn"
    fail = "fail"

    @property
    def rank(self) -> int:
        return {"ok": 0, "warn": 1, "fail": 2}[self.value]

    @classmethod
    def worst(cls, states: Iterable[CheckState]) -> CheckState:
        """The overall state is the worst of its parts (§16.8)."""
        return max(states, key=lambda state: state.rank, default=cls.ok)


#: Python enum -> PostgreSQL type name. The first migration creates these
#: explicitly, in this order, before any table that references them.
PG_ENUM_NAMES: dict[type[Enum], str] = {
    Channel: "channel",
    Modality: "modality",
    RequestStatus: "request_status",
    SlotStatus: "slot_status",
    NegotiationKind: "negotiation_kind",
    SenderType: "sender_type",
    WaitlistStatus: "waitlist_status",
    BookingMode: "booking_mode",
    TokenPurpose: "token_purpose",
    OutboxStatus: "outbox_status",
    ReminderState: "reminder_state",
    ActorType: "actor_type",
    ContentBlockKind: "content_block_kind",
    ErrorSource: "error_source",
}


def pg_enum(enum_cls: type[Enum], **kwargs: Any) -> SAEnum:
    """A native PostgreSQL enum column type for `enum_cls`.

    `values_callable` is what makes the lowercase values land in the database
    rather than the Python member names.
    """
    return SAEnum(
        enum_cls,
        name=PG_ENUM_NAMES[enum_cls],
        native_enum=True,
        create_type=False,  # created explicitly by the first migration
        values_callable=lambda e: [member.value for member in e],
        **kwargs,
    )
