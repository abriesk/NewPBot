"""Clients, identities, and tokens (IMPLEMENTATION.md §8, DESIGN.md §5).

A client is a *person*; an identity is one way to reach them. v1.0 keyed clients
by Telegram ID, which left nowhere to put someone who arrives through the web
and no path to a second channel.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ActorType, Channel, TokenPurpose
from app.core.errors import NotFound, TokenInvalid
from app.core.models import AuthToken, Client, Identity
from app.core.policies import now_utc
from app.core.services.settings import get_practice

#: DESIGN.md §5.1. Login tokens are short because they arrive by email;
#: channel-link tokens are longer because they ride along in a notification the
#: client may not read immediately.
TOKEN_LIFETIMES = {
    TokenPurpose.login: timedelta(minutes=30),
    TokenPurpose.link_channel: timedelta(hours=24),
    TokenPurpose.view_request: timedelta(hours=24),
}


def _hash(raw_token: str) -> str:
    """Raw tokens MUST NOT be stored (§6.2). Only this digest is."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _normalise(channel: Channel, external_id: str) -> str:
    """Email addresses are matched case-insensitively; Telegram ids are not
    text the user typed, so they are left alone."""
    return external_id.strip().lower() if channel == Channel.email else external_id.strip()


#: Deliberately loose: one @, something either side, a dot in the domain, no
#: whitespace. Anything stricter rejects real addresses, and the only claim
#: that matters -- that the person reads this mailbox -- is settled by the
#: login link, not by a pattern (DESIGN.md §5.1).
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def looks_like_email(value: str) -> bool:
    """A typo check, not a validator. See `_EMAIL_SHAPE`."""
    candidate = value.strip()
    return len(candidate) <= 254 and _EMAIL_SHAPE.match(candidate) is not None


@dataclass(frozen=True, slots=True)
class TokenResult:
    client_id: UUID | None
    purpose: TokenPurpose
    payload: dict[str, Any]


async def resolve_client(
    session: AsyncSession,
    channel: Channel,
    external_id: str,
    *,
    language: str | None = None,
    display_name: str | None = None,
    verified: bool = False,
) -> Client:
    """Get or create the client behind a channel identity.

    A Telegram identity is verified by construction -- Telegram vouches for the
    user id. An email identity is not, and an unverified email must not be able
    to book, or the service becomes a way to send unsolicited mail in the
    therapist's name (DESIGN.md §5.1).
    """
    practice = await get_practice(session)
    key = _normalise(channel, external_id)

    identity = (
        await session.execute(
            select(Identity).where(
                Identity.practice_id == practice.id,
                Identity.channel == channel,
                Identity.external_id == key,
            )
        )
    ).scalar_one_or_none()

    if identity is not None:
        client = (
            await session.execute(select(Client).where(Client.id == identity.client_id))
        ).scalar_one()
        if verified and identity.verified_at is None:
            identity.verified_at = now_utc()
            await session.flush()
        return client

    client = Client(
        practice_id=practice.id,
        language=language or practice.default_language,
        display_name=display_name,
    )
    session.add(client)
    await session.flush()

    session.add(
        Identity(
            practice_id=practice.id,
            client_id=client.id,
            channel=channel,
            external_id=key,
            verified_at=now_utc() if verified else None,
        )
    )
    await session.flush()
    return client


async def link_identity(
    session: AsyncSession,
    client_id: UUID,
    channel: Channel,
    external_id: str,
    *,
    verified: bool = False,
) -> Identity:
    """Attach another channel to an existing client -- the merge path.

    If the identity already belongs to this client it is returned unchanged; if
    it belongs to someone else it is left alone rather than reassigned, since
    silently moving an identity between people is not a recoverable mistake.
    """
    practice = await get_practice(session)
    key = _normalise(channel, external_id)

    existing = (
        await session.execute(
            select(Identity).where(
                Identity.practice_id == practice.id,
                Identity.channel == channel,
                Identity.external_id == key,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.client_id != client_id:
            raise TokenInvalid("that identity already belongs to another client")
        if verified and existing.verified_at is None:
            existing.verified_at = now_utc()
            await session.flush()
        return existing

    identity = Identity(
        practice_id=practice.id,
        client_id=client_id,
        channel=channel,
        external_id=key,
        verified_at=now_utc() if verified else None,
    )
    session.add(identity)
    await session.flush()
    return identity


async def issue_token(
    session: AsyncSession,
    purpose: TokenPurpose,
    *,
    client_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Mint a single-use token and return the **raw** value.

    The raw value is returned once, here, for the caller to put in a link. Only
    its hash is persisted, so a database leak does not hand out live tokens.
    """
    practice = await get_practice(session)
    raw = secrets.token_urlsafe(32)

    session.add(
        AuthToken(
            practice_id=practice.id,
            client_id=client_id,
            purpose=purpose,
            token_hash=_hash(raw),
            payload=payload or {},
            expires_at=now_utc() + TOKEN_LIFETIMES[purpose],
        )
    )
    await session.flush()
    return raw


async def consume_token(
    session: AsyncSession, raw_token: str, purpose: TokenPurpose
) -> TokenResult:
    """Validate and burn a token.

    Valid only if unused, unexpired, and issued for this purpose. `used_at` is
    set in the same transaction as the action the token authorises, so a
    rollback cannot leave a token spent for an action that did not happen.
    """
    token = (
        await session.execute(select(AuthToken).where(AuthToken.token_hash == _hash(raw_token)))
    ).scalar_one_or_none()

    # One error for unknown, used, expired, and wrong-purpose: distinguishing
    # them tells a caller whether a token existed.
    if (
        token is None
        or token.used_at is not None
        or token.expires_at <= now_utc()
        or token.purpose != purpose
    ):
        raise TokenInvalid("token is not valid")

    token.used_at = now_utc()
    await session.flush()
    return TokenResult(
        client_id=token.client_id, purpose=token.purpose, payload=token.payload or {}
    )


async def _get_client(session: AsyncSession, client_id: UUID) -> Client:
    client = (
        await session.execute(select(Client).where(Client.id == client_id))
    ).scalar_one_or_none()
    if client is None:
        raise NotFound(f"client {client_id}")
    return client


async def set_client_language(session: AsyncSession, client_id: UUID, lang: str) -> Client:
    client = await _get_client(session, client_id)
    client.language = lang
    await session.flush()
    return client


async def set_client_timezone(session: AsyncSession, client_id: UUID, iana: str) -> Client:
    """IANA names only. An offset string breaks at every DST transition."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(iana)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{iana!r} is not an IANA timezone name") from exc

    client = await _get_client(session, client_id)
    client.timezone = iana
    await session.flush()
    return client


async def identities_for(session: AsyncSession, client_id: UUID) -> list[Identity]:
    """Every way this client can be reached. The delivery policy in §13.3 picks
    among them."""
    return list(
        (await session.execute(select(Identity).where(Identity.client_id == client_id)))
        .scalars()
        .all()
    )


async def identities_for_many(
    session: AsyncSession, client_ids: Collection[UUID]
) -> dict[UUID, list[Identity]]:
    """The same, for a page of clients, in one query.

    §12.2 forbids a list whose query count depends on how many rows it draws,
    and `identities_for` in a loop is exactly that.
    """
    if not client_ids:
        return {}
    rows = (
        (await session.execute(select(Identity).where(Identity.client_id.in_(client_ids))))
        .scalars()
        .all()
    )
    grouped: dict[UUID, list[Identity]] = {client_id: [] for client_id in client_ids}
    for identity in rows:
        grouped[identity.client_id].append(identity)
    return grouped


@dataclass(frozen=True, slots=True)
class ClientSummary:
    """One person on §12.2's clients list.

    Everything the row draws, resolved by the query rather than by the caller:
    a channel that goes back for a count or a next session is the per-row page
    the requests list was already found to be.
    """

    client_id: UUID
    display_name: str
    created_at: datetime
    requests: int
    #: The most recent session that has already happened, if there is one.
    last_session: datetime | None
    #: The soonest confirmed session still ahead, if there is one.
    next_session: datetime | None
    identities: tuple[Identity, ...] = ()


async def list_clients_for_admin(
    session: AsyncSession, *, limit: int = 200
) -> list[ClientSummary]:
    """§12.2's clients list: the practice's people, busiest first.

    Not every `client` row. A person who asked for a magic link and never booked
    is not a client of the practice, and an erased one asked to be forgotten --
    both are on `/admin/privacy`, which is the page whose population has to be
    complete. Listing them here is what made one page misleading as the other.

    Ordered by the newest request rather than by `created_at`: a practice cares
    who it is seeing now, not who registered first.
    """
    from sqlalchemy import case, func

    from app.core.enums import RequestStatus
    from app.core.models import BookingRequest

    now = now_utc()
    practice = await get_practice(session)

    # `filter` is PostgreSQL's aggregate FILTER: last and next session in the
    # same pass as the count, so the list is one query and not one per person.
    past = case(
        (
            (BookingRequest.scheduled_start < now)
            & BookingRequest.status.in_((RequestStatus.confirmed, RequestStatus.completed)),
            BookingRequest.scheduled_start,
        ),
        else_=None,
    )
    ahead = case(
        (
            (BookingRequest.scheduled_start >= now)
            & (BookingRequest.status == RequestStatus.confirmed),
            BookingRequest.scheduled_start,
        ),
        else_=None,
    )

    rows = (
        await session.execute(
            select(
                Client.id,
                Client.display_name,
                Client.created_at,
                func.count(BookingRequest.id),
                func.max(past),
                func.min(ahead),
                func.max(BookingRequest.created_at).label("latest"),
            )
            .join(BookingRequest, BookingRequest.client_id == Client.id)
            .where(Client.practice_id == practice.id, Client.erased_at.is_(None))
            .group_by(Client.id, Client.display_name, Client.created_at)
            .order_by(func.max(BookingRequest.created_at).desc())
            .limit(limit)
        )
    ).all()

    identities = await identities_for_many(session, [row[0] for row in rows])
    return [
        ClientSummary(
            client_id=row[0],
            display_name=str(row[1]) if row[1] else "",
            created_at=row[2],
            requests=int(row[3]),
            last_session=row[4],
            next_session=row[5],
            identities=tuple(identities.get(row[0], ())),
        )
        for row in rows
    ]


# --- Rate limiting, from rows that already exist (§17) -----------------------

#: §17: magic-link issuance, 3 per hour per email.
MAGIC_LINK_PER_EMAIL = 3
MAGIC_LINK_WINDOW = timedelta(hours=1)


async def magic_link_allowance_left(session: AsyncSession, email: str) -> int:
    """How many more links this address may be sent this hour.

    Counted from `auth_token` rows rather than a counter table: the tokens are
    the thing being limited, they already carry a timestamp, and the count
    survives a restart.
    """
    from sqlalchemy import func

    practice = await get_practice(session)
    key = _normalise(Channel.email, email)

    issued = (
        await session.execute(
            select(func.count())
            .select_from(AuthToken)
            .join(Identity, Identity.client_id == AuthToken.client_id)
            .where(
                AuthToken.practice_id == practice.id,
                AuthToken.purpose == TokenPurpose.login,
                AuthToken.created_at >= now_utc() - MAGIC_LINK_WINDOW,
                Identity.channel == Channel.email,
                Identity.external_id == key,
            )
        )
    ).scalar_one()
    return max(0, MAGIC_LINK_PER_EMAIL - int(issued))


# --- Export and erasure (DESIGN.md §16) -------------------------------------


async def export_client(session: AsyncSession, client_id: UUID) -> dict[str, Any]:
    """Everything held about one person, as JSON-serialisable data.

    DESIGN.md §16: a request for a copy should be answerable without a database
    console. Problem text *is* included -- this goes to the person it is about,
    which is the one direction it may travel.
    """
    from app.core.models import BookingRequest, NegotiationMessage, WaitlistEntry

    client = await _get_client(session, client_id)

    requests = (
        (
            await session.execute(
                select(BookingRequest)
                .where(BookingRequest.client_id == client_id)
                .order_by(BookingRequest.created_at)
            )
        )
        .scalars()
        .all()
    )

    threads: dict[str, list[dict[str, Any]]] = {}
    for request in requests:
        messages = (
            (
                await session.execute(
                    select(NegotiationMessage)
                    .where(NegotiationMessage.request_id == request.id)
                    .order_by(NegotiationMessage.created_at)
                )
            )
            .scalars()
            .all()
        )
        threads[str(request.uuid)] = [
            {
                "sender": m.sender.value,
                "kind": m.kind.value,
                "proposed_start": m.proposed_start.isoformat() if m.proposed_start else None,
                "body": m.body_text,
                "at": m.created_at.isoformat(),
            }
            for m in messages
        ]

    waitlist_entries = (
        (
            await session.execute(
                select(WaitlistEntry)
                .where(WaitlistEntry.client_id == client_id)
                .order_by(WaitlistEntry.created_at)
            )
        )
        .scalars()
        .all()
    )

    return {
        "client": {
            "id": str(client.id),
            "display_name": client.display_name,
            "language": client.language,
            "timezone": client.timezone,
            "created_at": client.created_at.isoformat(),
            "erased_at": client.erased_at.isoformat() if client.erased_at else None,
        },
        "identities": [
            {
                "channel": i.channel.value,
                "address": i.external_id,
                "verified_at": i.verified_at.isoformat() if i.verified_at else None,
            }
            for i in await identities_for(session, client_id)
        ],
        "requests": [
            {
                "uuid": str(r.uuid),
                "status": r.status.value,
                "modality": r.modality.value,
                "created_at": r.created_at.isoformat(),
                "scheduled_start": r.scheduled_start.isoformat() if r.scheduled_start else None,
                "desired_time_text": r.desired_time_text,
                "problem_text": r.problem_text,
                "contact_note": r.contact_note,
                "cancellation_reason": r.cancellation_reason,
                "rejected_reason": r.rejected_reason,
                "thread": threads[str(r.uuid)],
            }
            for r in requests
        ],
        "waitlist": [
            {
                "uuid": str(w.uuid),
                "status": w.status.value,
                "created_at": w.created_at.isoformat(),
                "problem_text": w.problem_text,
                "contact_note": w.contact_note,
            }
            for w in waitlist_entries
        ],
    }


async def erase_client(session: AsyncSession, client_id: UUID) -> Client:
    """Honour a request to be forgotten (DESIGN.md §16).

    The person becomes unreachable and unidentifiable: identities, tokens and
    any half-finished flow go, and every free-text field they wrote is nulled.
    The rows themselves stay, with `erased_at` set, so the practice keeps its
    statistics -- deleting the bookings would silently change history.

    Confirmed future sessions are cancelled first, so the slot is released and
    no reminder fires at somebody who no longer exists.
    """
    from sqlalchemy import delete, update

    from app.core.enums import RequestStatus
    from app.core.models import (
        AuditLog,
        BookingRequest,
        FlowState,
        NegotiationMessage,
        WaitlistEntry,
    )
    from app.core.services import booking as booking_service

    client = await _get_client(session, client_id)

    confirmed = (
        (
            await session.execute(
                select(BookingRequest.id).where(
                    BookingRequest.client_id == client_id,
                    BookingRequest.status == RequestStatus.confirmed,
                )
            )
        )
        .scalars()
        .all()
    )
    for request_id in confirmed:
        await booking_service.admin_cancel(session, request_id, reason="client erased")

    request_ids = (
        (
            await session.execute(
                select(BookingRequest.id).where(BookingRequest.client_id == client_id)
            )
        )
        .scalars()
        .all()
    )
    if request_ids:
        await session.execute(
            update(NegotiationMessage)
            .where(NegotiationMessage.request_id.in_(request_ids))
            .values(body_text=None)
        )
        await session.execute(
            update(BookingRequest)
            .where(BookingRequest.id.in_(request_ids))
            .values(
                problem_text=None,
                contact_note=None,
                display_name=None,
                cancellation_reason=None,
                rejected_reason=None,
            )
        )

    await session.execute(
        update(WaitlistEntry)
        .where(WaitlistEntry.client_id == client_id)
        .values(problem_text=None, contact_note=None, admin_note=None)
    )

    await session.execute(delete(Identity).where(Identity.client_id == client_id))
    await session.execute(delete(AuthToken).where(AuthToken.client_id == client_id))
    await session.execute(delete(FlowState).where(FlowState.client_id == client_id))

    client.display_name = None
    client.timezone = None
    client.erased_at = now_utc()
    await session.flush()

    practice = await get_practice(session)
    session.add(
        AuditLog(
            practice_id=practice.id,
            actor_type=ActorType.admin,
            action="client.erase",
            entity_type="client",
            entity_id=str(client_id),
            # The identifier, never what was erased (hard rule 8).
            meta={"requests": len(request_ids)},
        )
    )
    await session.flush()
    return client
