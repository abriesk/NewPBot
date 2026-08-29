"""SQLAlchemy models (IMPLEMENTATION.md §6).

All timestamps are `timestamptz` holding UTC. Naive datetimes are banned
outright (hard rule 4): conversion to a local zone happens at the edges, never
in storage, and timezones are IANA names, never UTC offsets.

Nothing in this package may import fastapi, aiogram, jinja2, aiosmtplib, or nh3
(§3, enforced by tests/core/test_architecture.py).

A note on `practice_id`: §6's prose says every table carries one, but the DDL in
that same section omits it from the tables that reach their practice through an
unambiguous parent (admin_session, negotiation_message, content_block_revision,
outbox_attempt, reminder, slot_session_type). The DDL is followed here -- adding
a redundant denormalised key would create a second, desynchronisable source of
truth for the same fact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.enums import (
    ActorType,
    BookingMode,
    Channel,
    ContentBlockKind,
    ErrorSource,
    Modality,
    NegotiationKind,
    OutboxStatus,
    ReminderState,
    RequestStatus,
    SenderType,
    SlotStatus,
    TokenPurpose,
    WaitlistStatus,
    pg_enum,
)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _tstz(**kwargs: Any) -> Mapped[Any]:
    """A timestamptz column. Every instant in this schema is one."""
    return mapped_column(TIMESTAMP(timezone=True), **kwargs)


# --- Practice ---------------------------------------------------------------


class Practice(Base):
    """The deployment's single tenant.

    Exactly one row is seeded at install. `practice_id` exists on the other
    tables so a tenant key never has to be retrofitted across a live schema
    (DESIGN.md §18) -- it is not an invitation to build practice switching.
    """

    __tablename__ = "practice"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    default_language: Mapped[str] = mapped_column(Text, nullable=False, server_default="ru")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="Asia/Yerevan")
    clinic_onsite_url: Mapped[str | None] = mapped_column(Text)
    #: §6.1: the practice is working online only for now. Not derived from an
    #: empty `clinic_onsite_url` -- she may keep the address while not working
    #: there this month, and a blank address means "not filled in", not
    #: "in-person bookings are off".
    online_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    online_meeting_url: Mapped[str | None] = mapped_column(Text)
    availability_on: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    booking_mode: Mapped[BookingMode] = mapped_column(
        pg_enum(BookingMode), nullable=False, server_default="slots"
    )
    fallback_to_negotiation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    negotiation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    auto_confirm_slots: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    slot_hold_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="15")
    pending_expiry_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="48")
    cancel_window_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="24")
    # An empty array disables reminders. Replaces v1.0's two booleans, which
    # could not express a third offset or survive a reschedule.
    reminder_offsets_min: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default="{1440,60}"
    )
    retention_months: Mapped[int] = mapped_column(Integer, nullable=False, server_default="12")
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


# --- Clients and identity ---------------------------------------------------


class Client(Base):
    """A person, separate from the credential that reaches them (DESIGN.md §5).

    v1.0 keyed clients by Telegram ID, which left nowhere to put someone who
    arrives through the web.
    """

    __tablename__ = "client"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str | None] = mapped_column(Text)  # IANA, nullable
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    erased_at: Mapped[datetime | None] = _tstz()  # set by the erasure operation


class Identity(Base):
    """Binds a client to one channel: (telegram, 123456789) or (email, a@b.c).

    One client may hold several. Merging them is what the `link_<token>` deep
    link does, at a cost of one tap.
    """

    __tablename__ = "identity"
    __table_args__ = (
        UniqueConstraint("practice_id", "channel", "external_id"),
        Index("ix_identity_client_id", "client_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[Channel] = mapped_column(pg_enum(Channel), nullable=False)
    # Telegram user ID as text, or a lowercased email address.
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[datetime | None] = _tstz()
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


class AuthToken(Base):
    """Single-use, short-lived, stored only as a hash.

    Valid iff `used_at IS NULL AND expires_at > now()`. Consuming one sets
    `used_at` in the same transaction as the action it authorises.
    """

    __tablename__ = "auth_token"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    client_id: Mapped[UUID | None] = mapped_column(ForeignKey("client.id", ondelete="CASCADE"))
    purpose: Mapped[TokenPurpose] = mapped_column(pg_enum(TokenPurpose), nullable=False)
    # sha256 of the raw token. Raw tokens MUST NOT be stored.
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = _tstz(nullable=False)
    used_at: Mapped[datetime | None] = _tstz()
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


# --- Admin ------------------------------------------------------------------


class AdminUser(Base):
    __tablename__ = "admin_user"
    __table_args__ = (UniqueConstraint("practice_id", "username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)  # Argon2id
    email: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    last_login_at: Mapped[datetime | None] = _tstz()


class AdminSession(Base):
    __tablename__ = "admin_session"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(
        ForeignKey("admin_user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = _tstz(nullable=False)
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    revoked_at: Mapped[datetime | None] = _tstz()


# --- Session types and slots ------------------------------------------------


class SessionType(Base):
    """A bookable product.

    Rows rather than a code enum, so adding "supervision" is an insert rather
    than a migration and a deploy.
    """

    __tablename__ = "session_type"
    __table_args__ = (UniqueConstraint("practice_id", "code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)  # 'individual', 'couple'
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    # Structured amount plus currency, not free text: nearly free now, and
    # required the moment payments appear.
    price_amount_minor: Mapped[int | None] = mapped_column(Integer)  # 5000 = 50.00
    price_currency: Mapped[str | None] = mapped_column(Text)  # ISO 4217
    price_display_override: Mapped[str | None] = mapped_column(Text)  # wins when set
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class Slot(Base):
    """An offered start time.

    `hold` and `book` MUST take a row lock before checking status (§7.2). This
    is the only place in the system where a lost update would double-book.
    """

    __tablename__ = "slot"
    __table_args__ = (
        Index("ix_slot_practice_id_status_starts_at", "practice_id", "status", "starts_at"),
        # NULLS NOT DISTINCT is required: modality IS NULL means "either", and
        # without it PostgreSQL treats every NULL as distinct, allowing
        # unlimited duplicate slots at the same instant.
        Index(
            "slot_unique_offer",
            "practice_id",
            "starts_at",
            "modality",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "(status = 'held') = (hold_expires_at IS NOT NULL AND held_by_request IS NOT NULL)",
            name="held_implies_hold_fields",
        ),
        CheckConstraint(
            "(status = 'booked') = (booked_request IS NOT NULL)",
            name="booked_implies_booked_request",
        ),
        CheckConstraint(
            "status NOT IN ('available', 'blocked') OR ("
            "hold_expires_at IS NULL AND held_by_request IS NULL AND booked_request IS NULL)",
            name="free_implies_no_reservation",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    starts_at: Mapped[datetime] = _tstz(nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    modality: Mapped[Modality | None] = mapped_column(pg_enum(Modality))  # NULL = either
    status: Mapped[SlotStatus] = mapped_column(
        pg_enum(SlotStatus), nullable=False, server_default="available"
    )
    hold_expires_at: Mapped[datetime | None] = _tstz()
    # The foreign keys to booking_request are added at the end of the first
    # migration, once that table exists -- the dependency is circular.
    held_by_request: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("booking_request.id", ondelete="SET NULL", use_alter=True)
    )
    booked_request: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("booking_request.id", ondelete="SET NULL", use_alter=True)
    )
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


class SlotSessionType(Base):
    """An empty set for a slot means it accepts **all** active session types."""

    __tablename__ = "slot_session_type"

    slot_id: Mapped[int] = mapped_column(
        ForeignKey("slot.id", ondelete="CASCADE"), primary_key=True
    )
    session_type_id: Mapped[int] = mapped_column(
        ForeignKey("session_type.id", ondelete="CASCADE"), primary_key=True
    )


# --- Booking requests -------------------------------------------------------


class BookingRequest(Base):
    """A client's ask for a session. Its status is the heart of the system (§7.1)."""

    __tablename__ = "booking_request"
    __table_args__ = (
        Index(
            "ix_booking_request_practice_id_status_created_at",
            "practice_id",
            "status",
            text("created_at DESC"),
        ),
        Index("ix_booking_request_client_id", "client_id"),
        Index(
            "ix_booking_request_scheduled_start",
            "scheduled_start",
            postgresql_where=text("status = 'confirmed'"),
        ),
        CheckConstraint(
            "status <> 'confirmed' OR (scheduled_start IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="confirmed_requires_schedule",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The client-visible identifier. MUST appear in every admin notification.
    uuid: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, unique=True, server_default=func.gen_random_uuid()
    )
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    client_id: Mapped[UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    session_type_id: Mapped[int] = mapped_column(ForeignKey("session_type.id"), nullable=False)
    modality: Mapped[Modality] = mapped_column(pg_enum(Modality), nullable=False)
    status: Mapped[RequestStatus] = mapped_column(
        pg_enum(RequestStatus), nullable=False, server_default="pending"
    )
    source_channel: Mapped[Channel] = mapped_column(pg_enum(Channel), nullable=False)
    slot_id: Mapped[int | None] = mapped_column(ForeignKey("slot.id"))
    # One source of truth for the agreed time. v1.0 carried a `final_time`
    # string alongside it; free text survives only as the client's *request*.
    scheduled_start: Mapped[datetime | None] = _tstz()
    scheduled_duration_min: Mapped[int | None] = mapped_column(Integer)
    client_timezone: Mapped[str | None] = mapped_column(Text)  # IANA at time of request
    meeting_url: Mapped[str | None] = mapped_column(Text)  # overrides the practice default
    desired_time_text: Mapped[str | None] = mapped_column(Text)  # free-text path only
    # Health-related information about an identifiable person. Never logged,
    # never in an email payload, never in audit meta (hard rule 8).
    problem_text: Mapped[str | None] = mapped_column(Text)
    contact_note: Mapped[str | None] = mapped_column(Text)  # preferred means of contact
    display_name: Mapped[str | None] = mapped_column(Text)  # as typed, unverified
    expires_at: Mapped[datetime | None] = _tstz()  # pending expiry
    confirmed_at: Mapped[datetime | None] = _tstz()
    cancelled_at: Mapped[datetime | None] = _tstz()
    cancelled_by: Mapped[ActorType | None] = mapped_column(pg_enum(ActorType))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


class NegotiationMessage(Base):
    """One turn in the back-and-forth about when to meet.

    Whose turn it is is **derived** from the last message's sender and MUST NOT
    be stored (§6.6).
    """

    __tablename__ = "negotiation_message"
    __table_args__ = (
        Index("ix_negotiation_message_request_id_created_at", "request_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("booking_request.id", ondelete="CASCADE"), nullable=False
    )
    sender: Mapped[SenderType] = mapped_column(pg_enum(SenderType), nullable=False)
    kind: Mapped[NegotiationKind] = mapped_column(pg_enum(NegotiationKind), nullable=False)
    proposed_start: Mapped[datetime | None] = _tstz()
    body_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


class WaitlistEntry(Base):
    """Its own table, not `type='waitlist'` on a request.

    A waitlist entry has no slot, no negotiation, and no reminders; folding it
    in would make half the request columns nullable.
    """

    __tablename__ = "waitlist_entry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uuid: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, unique=True, server_default=func.gen_random_uuid()
    )
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    client_id: Mapped[UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    problem_text: Mapped[str | None] = mapped_column(Text)
    contact_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[WaitlistStatus] = mapped_column(
        pg_enum(WaitlistStatus), nullable=False, server_default="new"
    )
    admin_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    contacted_at: Mapped[datetime | None] = _tstz()


# --- Content, translations, timezones ---------------------------------------


class ContentTopic(Base):
    """A page.

    Titles are **not** a column -- they come from the translation key
    `content.topic.<code>.title`.
    """

    __tablename__ = "content_topic"
    __table_args__ = (UniqueConstraint("practice_id", "code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)  # 'work_terms', ...
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    show_in_menu: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class ContentBlock(Base):
    """One Markdown fragment, in one language, at a position.

    Block granularity is the real answer to Telegram's 4096-character limit:
    splitting at boundaries the author chose produces a conversation, where
    splitting a long document automatically produces awkward breaks.
    """

    __tablename__ = "content_block"
    __table_args__ = (UniqueConstraint("topic_id", "lang", "position"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("content_topic.id", ondelete="CASCADE"), nullable=False
    )
    lang: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[ContentBlockKind] = mapped_column(
        pg_enum(ContentBlockKind), nullable=False, server_default="text"
    )
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    link_url: Mapped[str | None] = mapped_column(Text)  # kind='link_button'
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    updated_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


class ContentBlockRevision(Base):
    """Every write to a block inserts the previous body here and increments
    `version`, in one transaction. The most recent 20 per block are kept."""

    __tablename__ = "content_block_revision"
    __table_args__ = (UniqueConstraint("block_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    block_id: Mapped[int] = mapped_column(
        ForeignKey("content_block.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


class Translation(Base):
    """UI strings: developer-owned, seeded from locales/, editable afterwards.

    A deploy never overwrites an existing row -- the therapist's edits win.
    """

    __tablename__ = "translation"
    __table_args__ = (UniqueConstraint("practice_id", "lang", "key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    lang: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = _tstz(
        # `onupdate` and not just `server_default`: the seed inserts every key at
        # boot, so a therapist's edit is always an UPDATE. Without this the
        # column never moves after the first boot and `invalidate_if_stale`
        # (§15) would have nothing to watch.
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TimezoneOption(Base):
    """A therapist-curated list of IANA zones with friendly labels -- Telegram
    has no automatic source for a client's timezone."""

    __tablename__ = "timezone_option"
    __table_args__ = (UniqueConstraint("practice_id", "iana_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    iana_name: Mapped[str] = mapped_column(Text, nullable=False)  # 'Asia/Yerevan'
    display_name: Mapped[str] = mapped_column(Text, nullable=False)  # 'Yerevan, Tbilisi, Dubai'
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


# --- Outbox, reminders, audit -----------------------------------------------


class OutboxMessage(Base):
    """One thing to deliver, to one address, on one channel.

    Written in the same transaction as the domain change that caused it.
    Nothing calls bot.send_message() from a handler (hard rule 2).

    `payload` MUST NOT carry problem_text or negotiation bodies for the email
    channel (§13.4) -- email is the least private channel in this system.
    """

    __tablename__ = "outbox_message"
    __table_args__ = (
        Index("ix_outbox_message_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    channel: Mapped[Channel] = mapped_column(pg_enum(Channel), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)  # chat ID or email
    client_id: Mapped[UUID | None] = mapped_column(ForeignKey("client.id", ondelete="SET NULL"))
    admin_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    request_id: Mapped[int | None] = mapped_column(
        ForeignKey("booking_request.id", ondelete="SET NULL")
    )
    intent_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    locale: Mapped[str] = mapped_column(Text, nullable=False)
    # Idempotency: a worker restart mid-send cannot double-notify.
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[OutboxStatus] = mapped_column(
        pg_enum(OutboxStatus), nullable=False, server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    sent_at: Mapped[datetime | None] = _tstz()


class OutboxAttempt(Base):
    """The delivery log: what was sent, when, and what failed."""

    __tablename__ = "outbox_attempt"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("outbox_message.id", ondelete="CASCADE"), nullable=False
    )
    attempted_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class Reminder(Base):
    """Rows rather than booleans: booleans cannot express a third offset, cannot
    survive a reschedule cleanly, and cannot record why one was skipped."""

    __tablename__ = "reminder"
    __table_args__ = (
        UniqueConstraint("request_id", "offset_min"),
        Index("ix_reminder_state_due_at", "state", "due_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("booking_request.id", ondelete="CASCADE"), nullable=False
    )
    offset_min: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = _tstz(nullable=False)
    state: Mapped[ReminderState] = mapped_column(
        pg_enum(ReminderState), nullable=False, server_default="scheduled"
    )
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    fired_at: Mapped[datetime | None] = _tstz()


class FlowState(Base):
    """Multi-step input state for a client, per channel (§13.1).

    §13.1 requires this to live in the database rather than in aiogram FSM
    memory, so restarting `web` does not lose a half-finished booking. §6 does
    not define the table; its shape was agreed rather than invented.

    Keyed per channel on purpose: a client mid-booking in Telegram and another
    tab on the web are two independent flows, and a future WhatsApp adapter
    reuses this without a migration.

    `data` is transient UI scratch -- half-typed answers, the slot under
    consideration. It may hold problem text, so it is purged with the rest
    (hard rule 8) and never logged.
    """

    __tablename__ = "flow_state"
    __table_args__ = (UniqueConstraint("client_id", "channel"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[Channel] = mapped_column(pg_enum(Channel), nullable=False)
    step: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    updated_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


class AuditLog(Base):
    """`meta` MUST NOT carry problem_text or message bodies. Log identifiers."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # 'request.confirm', ...
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


class ErrorEvent(Base):
    """An exception that left no other trace (§6.9, DESIGN.md §22.2).

    Every other failure in this system is already a row: an undelivered
    message is an outbox row, a missed reminder is a reminder row. An
    unhandled exception in a request is not -- the client sees a 500 and
    leaves -- and a worker job that raises is caught on purpose so one bad job
    cannot stop the others (§14). Without this table those two are visible
    only in logs, which do not survive `docker compose down`.

    `kind` and `location` only. The exception's **message and traceback are
    deliberately not stored**: either can carry an email address or a fragment
    of problem text, and hard rule 8 has no exception for tracebacks. The logs
    keep the detail, where detail belongs.
    """

    __tablename__ = "error_event"
    __table_args__ = (Index("ix_error_event_at", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice.id"), nullable=False)
    source: Mapped[ErrorSource] = mapped_column(pg_enum(ErrorSource), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # exception class
    location: Mapped[str] = mapped_column(Text, nullable=False)  # 'module:line' or a job name
    at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())


__all__ = [
    "AdminSession",
    "AdminUser",
    "AuditLog",
    "AuthToken",
    "Base",
    "BookingRequest",
    "Client",
    "ContentBlock",
    "ContentBlockRevision",
    "ContentTopic",
    "ErrorEvent",
    "FlowState",
    "Identity",
    "NegotiationMessage",
    "OutboxAttempt",
    "OutboxMessage",
    "Practice",
    "Reminder",
    "SessionType",
    "Slot",
    "SlotSessionType",
    "TimezoneOption",
    "Translation",
    "WaitlistEntry",
]
