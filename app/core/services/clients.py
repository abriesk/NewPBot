"""Clients, identities, and tokens (IMPLEMENTATION.md §8, DESIGN.md §5).

A client is a *person*; an identity is one way to reach them. v1.0 keyed clients
by Telegram ID, which left nowhere to put someone who arrives through the web
and no path to a second channel.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel, TokenPurpose
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
