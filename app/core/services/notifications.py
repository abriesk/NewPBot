"""Domain events -> outbox rows (IMPLEMENTATION.md §10, §13.3, §13.4).

This is the only place that decides *who* hears about a domain change and *on
which channel*. It writes rows; it never formats text and never touches a
socket. Rendering happens in the worker's dispatch job, which is why nothing
here imports app.render -- that module pulls in nh3, which core may not have.

Rows are written in the same transaction as the domain change that caused them,
which is what makes "a confirmed booking and its notification either both happen
or neither does" true (DESIGN.md §12).

Payloads carry identifiers and instants, never rendered text (§10), and for the
email channel never `problem_text` or negotiation bodies (§13.4) -- email is the
least private channel in this system.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.enums import Channel, Modality, OutboxStatus
from app.core.events import (
    DomainEvent,
    RequestCancelled,
    RequestConfirmed,
    RequestCounter,
    RequestExpired,
    RequestProposal,
    RequestRejected,
    RequestSubmitted,
    WaitlistJoined,
    drain,
)
from app.core.models import (
    AdminUser,
    BookingRequest,
    Client,
    Identity,
    OutboxMessage,
    SessionType,
    WaitlistEntry,
)
from app.core.services.settings import get_practice

logger = logging.getLogger(__name__)


class Recipient(StrEnum):
    admin = "admin"
    client = "client"


#: §13.3. Both identities are used for these two, when both exist -- a session
#: reminder is worth arriving twice rather than not at all.
BOTH_CHANNELS = frozenset({"reminder.client", "request.confirmed.client"})

#: §13.4. Stripped from any payload bound for the email channel. `problem`
#: appears in the admin submission intent; `note` and `thread` carry negotiation
#: bodies.
EMAIL_FORBIDDEN_FIELDS = frozenset({"problem", "problem_text", "note", "thread", "contact_note"})


@dataclass(frozen=True, slots=True)
class Envelope:
    """One intent bound for one recipient, before channel routing."""

    intent_key: str
    recipient: Recipient
    payload: dict[str, Any]
    request_id: int | None = None
    dedupe_scope: str | None = None


def _iso(value: datetime | None) -> str | None:
    """Payloads MUST be JSON-serialisable (§10)."""
    return value.isoformat() if value is not None else None


async def _session_type_code(session: AsyncSession, session_type_id: int) -> str:
    return str(
        (
            await session.execute(select(SessionType.code).where(SessionType.id == session_type_id))
        ).scalar_one()
    )


async def envelopes_for(session: AsyncSession, event: DomainEvent) -> list[Envelope]:
    """Map one domain event onto the intents in §10's catalogue."""
    if isinstance(event, RequestSubmitted):
        request = await _request(session, event.request_id)
        return [
            Envelope(
                "request.submitted.admin",
                Recipient.admin,
                {
                    "uuid": str(request.uuid),
                    "name": request.display_name,
                    "session_type": await _session_type_code(session, request.session_type_id),
                    "modality": request.modality.value,
                    "time": _iso(request.scheduled_start) or request.desired_time_text,
                    "problem": request.problem_text,
                },
                request_id=request.id,
                dedupe_scope=f"submitted:{request.id}",
            ),
            Envelope(
                "request.submitted.client",
                Recipient.client,
                {
                    "uuid": str(request.uuid),
                    "session_type": await _session_type_code(session, request.session_type_id),
                    "time": _iso(request.scheduled_start) or request.desired_time_text,
                },
                request_id=request.id,
                dedupe_scope=f"submitted-client:{request.id}",
            ),
        ]

    if isinstance(event, RequestConfirmed):
        request = await _request(session, event.request_id)
        join = await join_info(session, request)
        return [
            Envelope(
                "request.confirmed.client",
                Recipient.client,
                {
                    "uuid": str(request.uuid),
                    "time": _iso(event.scheduled_start),
                    "session_type": await _session_type_code(session, request.session_type_id),
                    "modality": request.modality.value,
                    "join_url": join,
                },
                request_id=request.id,
                dedupe_scope=f"confirmed:{request.id}",
            ),
            Envelope(
                "request.confirmed.admin",
                Recipient.admin,
                {
                    "uuid": str(request.uuid),
                    "time": _iso(event.scheduled_start),
                    "name": request.display_name,
                },
                request_id=request.id,
                dedupe_scope=f"confirmed-admin:{request.id}",
            ),
        ]

    if isinstance(event, RequestProposal):
        request = await _request(session, event.request_id)
        return [
            Envelope(
                "request.proposal.client",
                Recipient.client,
                {
                    "uuid": str(request.uuid),
                    "time": _iso(event.proposed_start),
                    "note": None,  # negotiation bodies stay in the admin UI
                },
                request_id=request.id,
            )
        ]

    if isinstance(event, RequestCounter):
        request = await _request(session, event.request_id)
        return [
            Envelope(
                "request.counter.admin",
                Recipient.admin,
                {
                    "uuid": str(request.uuid),
                    "time": _iso(event.proposed_start),
                    "note": None,
                },
                request_id=request.id,
            )
        ]

    if isinstance(event, RequestRejected):
        request = await _request(session, event.request_id)
        return [
            Envelope(
                "request.rejected.client",
                Recipient.client,
                {"uuid": str(request.uuid), "reason": request.rejected_reason},
                request_id=request.id,
                dedupe_scope=f"rejected:{request.id}",
            )
        ]

    if isinstance(event, RequestExpired):
        request = await _request(session, event.request_id)
        return [
            Envelope(
                "request.expired.client",
                Recipient.client,
                {"uuid": str(request.uuid)},
                request_id=request.id,
                dedupe_scope=f"expired:{request.id}",
            )
        ]

    if isinstance(event, RequestCancelled):
        request = await _request(session, event.request_id)
        return [
            Envelope(
                "request.cancelled.client",
                Recipient.client,
                {
                    "uuid": str(request.uuid),
                    "time": _iso(event.scheduled_start),
                    "reason": request.cancellation_reason,
                },
                request_id=request.id,
                dedupe_scope=f"cancelled:{request.id}",
            )
        ]

    if isinstance(event, WaitlistJoined):
        entry = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == event.entry_id))
        ).scalar_one()
        return [
            Envelope(
                "waitlist.joined.client",
                Recipient.client,
                {"uuid": str(entry.uuid)},
                dedupe_scope=f"waitlist-client:{entry.id}",
            ),
            Envelope(
                "waitlist.joined.admin",
                Recipient.admin,
                {
                    "uuid": str(entry.uuid),
                    "problem": entry.problem_text,
                    "contact_note": entry.contact_note,
                },
                dedupe_scope=f"waitlist-admin:{entry.id}",
            ),
        ]

    logger.warning("no intent mapping for %s", type(event).__name__)
    return []


async def _request(session: AsyncSession, request_id: int) -> BookingRequest:
    return (
        await session.execute(select(BookingRequest).where(BookingRequest.id == request_id))
    ).scalar_one()


async def join_info(session: AsyncSession, request: BookingRequest) -> str | None:
    """§10: request.meeting_url, else practice.online_meeting_url, else omitted.

    Only for online sessions. For on-site, the clinic address takes its place --
    and unlike a meeting link, an address MAY be sent by email.
    """
    practice = await get_practice(session)
    if request.modality == Modality.onsite:
        return practice.clinic_onsite_url
    return request.meeting_url or practice.online_meeting_url


def scrub_for_email(intent_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """§13.4. Strip anything that must not reach an inbox.

    Email is the least private channel here -- shared inboxes, lock-screen
    previews, a partner reading over a shoulder. The join link goes too: for
    email the client is sent to /r/{uuid} and sees it after authenticating.
    """
    scrubbed = {k: v for k, v in payload.items() if k not in EMAIL_FORBIDDEN_FIELDS}
    scrubbed.pop("join_url", None)
    return scrubbed


async def _client_targets(
    session: AsyncSession, client_id: UUID, intent_key: str
) -> list[tuple[Channel, str]]:
    """§13.3: Telegram if one exists; otherwise the *verified* email identity;
    both for reminders and confirmations when both exist.

    An unverified email is never a target -- otherwise the service becomes a way
    to send unsolicited mail in the therapist's name (DESIGN.md §5.1).
    """
    settings = get_settings()
    identities = (
        (await session.execute(select(Identity).where(Identity.client_id == client_id)))
        .scalars()
        .all()
    )

    telegram = next((i for i in identities if i.channel == Channel.telegram), None)
    email = next(
        (i for i in identities if i.channel == Channel.email and i.verified_at is not None),
        None,
    )

    targets: list[tuple[Channel, str]] = []
    if telegram is not None:
        targets.append((Channel.telegram, telegram.external_id))

    # §4: with SMTP_HOST unset, no email rows are created at all.
    if email is not None and settings.email_enabled:
        if telegram is None or intent_key in BOTH_CHANNELS:
            targets.append((Channel.email, email.external_id))

    return targets


async def _admin_targets(session: AsyncSession) -> list[tuple[Channel, str, int | None]]:
    """The therapist's own channels.

    Telegram admin ids come from configuration -- the bot has no other way to
    authenticate her (DESIGN.md §5.2).
    """
    settings = get_settings()
    admin = (
        await session.execute(select(AdminUser).order_by(AdminUser.id).limit(1))
    ).scalar_one_or_none()
    admin_id = admin.id if admin is not None else None

    targets: list[tuple[Channel, str, int | None]] = [
        (Channel.telegram, str(chat_id), admin_id)
        for chat_id in sorted(settings.admin_telegram_ids)
    ]
    if admin is not None and admin.email and settings.email_enabled:
        targets.append((Channel.email, admin.email, admin_id))
    return targets


async def enqueue(session: AsyncSession, envelope: Envelope) -> list[OutboxMessage]:
    """Write the outbox rows for one envelope."""
    practice = await get_practice(session)
    rows: list[OutboxMessage] = []

    targets: list[tuple[Channel, str, int | None]]
    if envelope.recipient is Recipient.client:
        client_id = await _client_for(session, envelope)
        if client_id is None:
            return []
        client = (await session.execute(select(Client).where(Client.id == client_id))).scalar_one()
        locale = client.language
        targets = [
            (channel, address, None)
            for channel, address in await _client_targets(session, client_id, envelope.intent_key)
        ]
    else:
        client_id = None
        # The admin surface is English by design (DESIGN.md §11).
        locale = "en"
        targets = await _admin_targets(session)

    for channel, address, admin_user_id in targets:
        payload = (
            scrub_for_email(envelope.intent_key, envelope.payload)
            if channel == Channel.email
            else envelope.payload
        )
        dedupe = (
            f"{envelope.dedupe_scope}:{channel.value}:{address}" if envelope.dedupe_scope else None
        )
        rows.append(
            OutboxMessage(
                practice_id=practice.id,
                channel=channel,
                address=address,
                client_id=client_id,
                admin_user_id=admin_user_id,
                request_id=envelope.request_id,
                intent_key=envelope.intent_key,
                payload=payload,
                locale=locale,
                dedupe_key=dedupe,
                status=OutboxStatus.pending,
            )
        )

    for row in rows:
        session.add(row)
    await session.flush()
    return rows


async def _client_for(session: AsyncSession, envelope: Envelope) -> UUID | None:
    if envelope.request_id is not None:
        request = await _request(session, envelope.request_id)
        return request.client_id

    entry_uuid = envelope.payload.get("uuid")
    if entry_uuid:
        entry = (
            await session.execute(
                select(WaitlistEntry).where(WaitlistEntry.uuid == UUID(str(entry_uuid)))
            )
        ).scalar_one_or_none()
        if entry is not None:
            return entry.client_id
    return None


async def publish(session: AsyncSession, events: Sequence[DomainEvent] | None = None) -> int:
    """Turn queued domain events into outbox rows.

    Called before the transaction commits, so the rows and the domain change
    land together or not at all.
    """
    pending_events = list(events) if events is not None else drain(session)
    written = 0
    for event in pending_events:
        for envelope in await envelopes_for(session, event):
            written += len(await enqueue(session, envelope))
    return written


async def alert_admin_delivery_failed(
    session: AsyncSession, *, intent_key: str, address: str, error: str
) -> None:
    """§14: after the final attempt, tell the therapist on her own channel.

    The failing address is included; the message body is not -- the point is
    "she did not get it", not what it said.
    """
    await enqueue(
        session,
        Envelope(
            "system.delivery_failed.admin",
            Recipient.admin,
            {"intent": intent_key, "address": address, "error": error[:500]},
        ),
    )


async def enqueue_raw(
    session: AsyncSession,
    *,
    intent_key: str,
    recipient: Recipient,
    payload: dict[str, Any],
    request_id: int | None = None,
    dedupe_key: str | None = None,
) -> list[OutboxMessage]:
    """Escape hatch for rows the worker creates directly, notably reminders,
    whose dedupe key §14 specifies exactly."""
    envelope = Envelope(intent_key, recipient, payload, request_id=request_id)
    if dedupe_key is None:
        return await enqueue(session, envelope)

    practice = await get_practice(session)
    client_id = await _client_for(session, envelope)
    if client_id is None:
        return []
    client = (await session.execute(select(Client).where(Client.id == client_id))).scalar_one()

    rows: list[OutboxMessage] = []
    for channel, address in await _client_targets(session, client_id, intent_key):
        scoped = f"{dedupe_key}:{channel.value}:{address}"
        stmt = (
            pg_insert(OutboxMessage)
            .values(
                practice_id=practice.id,
                channel=channel,
                address=address,
                client_id=client_id,
                request_id=request_id,
                intent_key=intent_key,
                payload=(
                    scrub_for_email(intent_key, payload) if channel == Channel.email else payload
                ),
                locale=client.language,
                dedupe_key=scoped,
                status=OutboxStatus.pending,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(OutboxMessage)
        )
        rows.extend((await session.execute(stmt)).scalars().all())
    await session.flush()
    return rows
