"""Domain enumerations (IMPLEMENTATION.md §5).

Every one of these is persisted as a PostgreSQL *native* enum type, with the
lowercase **values** stored -- not the Python member names. `pg_enum` below wires
`values_callable` to guarantee that; forgetting it silently persists 'PENDING'
where the schema says 'pending'.

`RequestType` from v1.0 deliberately does not exist. Session types are rows
(§6.4) so that adding "supervision" is an insert, not a migration and a deploy.
"""

from __future__ import annotations

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
