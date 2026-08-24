"""Clients, identities, and tokens (DESIGN.md §5, IMPLEMENTATION.md §8)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel, TokenPurpose
from app.core.errors import TokenInvalid
from app.core.models import AuthToken, Identity, Practice
from app.core.policies import now_utc
from app.core.services import clients, waitlist
from app.core.services.clients import consume_token, issue_token, link_identity, resolve_client


async def test_resolve_client_is_get_or_create(db: AsyncSession) -> None:
    first = await resolve_client(db, Channel.telegram, "555", verified=True)
    again = await resolve_client(db, Channel.telegram, "555", verified=True)
    assert first.id == again.id


async def test_a_telegram_identity_is_verified_by_construction(db: AsyncSession) -> None:
    """Telegram vouches for the user id (DESIGN.md §5.1)."""
    client = await resolve_client(db, Channel.telegram, "777", verified=True)
    identity = (
        await db.execute(select(Identity).where(Identity.client_id == client.id))
    ).scalar_one()
    assert identity.verified_at is not None


async def test_an_email_identity_starts_unverified(db: AsyncSession) -> None:
    """An unverified email must not be able to book, or the service becomes a
    way to send unsolicited mail in the therapist's name."""
    client = await resolve_client(db, Channel.email, "someone@example.test")
    identity = (
        await db.execute(select(Identity).where(Identity.client_id == client.id))
    ).scalar_one()
    assert identity.verified_at is None


async def test_email_addresses_are_matched_case_insensitively(db: AsyncSession) -> None:
    first = await resolve_client(db, Channel.email, "Person@Example.Test")
    again = await resolve_client(db, Channel.email, "person@example.test")
    assert first.id == again.id


async def test_a_client_defaults_to_the_practice_language(
    db: AsyncSession, practice: Practice
) -> None:
    client = await resolve_client(db, Channel.telegram, "888")
    assert client.language == practice.default_language


async def test_linking_a_second_channel_merges_onto_one_client(db: AsyncSession) -> None:
    """The deep-link merge path: one tap, rather than asking the client to type
    their email into the bot."""
    client = await resolve_client(db, Channel.email, "merge@example.test")
    await link_identity(db, client.id, Channel.telegram, "999", verified=True)

    found = await resolve_client(db, Channel.telegram, "999")
    assert found.id == client.id
    assert len(await clients.identities_for(db, client.id)) == 2


async def test_an_identity_is_never_silently_reassigned(db: AsyncSession) -> None:
    owner = await resolve_client(db, Channel.telegram, "1111", verified=True)
    other = await resolve_client(db, Channel.email, "other@example.test")

    with pytest.raises(TokenInvalid):
        await link_identity(db, other.id, Channel.telegram, "1111")

    identity = (
        await db.execute(select(Identity).where(Identity.external_id == "1111"))
    ).scalar_one()
    assert identity.client_id == owner.id


# --- Tokens -----------------------------------------------------------------


async def test_only_the_hash_of_a_token_is_stored(db: AsyncSession) -> None:
    raw = await issue_token(db, TokenPurpose.login)
    stored = (await db.execute(select(AuthToken.token_hash))).scalars().all()
    assert raw not in stored


async def test_a_token_can_be_consumed_once(db: AsyncSession) -> None:
    raw = await issue_token(db, TokenPurpose.login, payload={"email": "a@b.test"})

    result = await consume_token(db, raw, TokenPurpose.login)
    assert result.payload["email"] == "a@b.test"

    with pytest.raises(TokenInvalid):
        await consume_token(db, raw, TokenPurpose.login)


async def test_a_token_is_rejected_for_the_wrong_purpose(db: AsyncSession) -> None:
    raw = await issue_token(db, TokenPurpose.login)
    with pytest.raises(TokenInvalid):
        await consume_token(db, raw, TokenPurpose.link_channel)


async def test_an_expired_token_is_rejected(db: AsyncSession) -> None:
    from datetime import timedelta

    from app.core.services.clients import _hash

    raw = await issue_token(db, TokenPurpose.login)
    # By hash, not "the last row": other tokens exist once more than one test
    # has run, and an unordered SELECT would expire somebody else's.
    token = (
        await db.execute(select(AuthToken).where(AuthToken.token_hash == _hash(raw)))
    ).scalar_one()
    token.expires_at = now_utc() - timedelta(seconds=1)
    await db.flush()

    with pytest.raises(TokenInvalid):
        await consume_token(db, raw, TokenPurpose.login)


async def test_an_unknown_token_is_rejected(db: AsyncSession) -> None:
    with pytest.raises(TokenInvalid):
        await consume_token(db, "not-a-real-token", TokenPurpose.login)


async def test_login_and_link_tokens_have_the_lifetimes_from_the_design(
    db: AsyncSession,
) -> None:
    from datetime import timedelta

    assert clients.TOKEN_LIFETIMES[TokenPurpose.login] == timedelta(minutes=30)
    assert clients.TOKEN_LIFETIMES[TokenPurpose.link_channel] == timedelta(hours=24)


# --- Client preferences -----------------------------------------------------


async def test_timezone_must_be_an_iana_name(db: AsyncSession) -> None:
    client = await resolve_client(db, Channel.telegram, "2222")

    await clients.set_client_timezone(db, client.id, "Europe/Moscow")
    assert client.timezone == "Europe/Moscow"

    with pytest.raises(ValueError, match="IANA"):
        await clients.set_client_timezone(db, client.id, "UTC+3")


async def test_language_can_be_set_to_hy_and_never_am(db: AsyncSession) -> None:
    """Hard rule 5: `am` is Amharic."""
    client = await resolve_client(db, Channel.telegram, "3333")
    await clients.set_client_language(db, client.id, "hy")
    assert client.language == "hy"


# --- Waitlist ---------------------------------------------------------------


async def test_joining_the_waitlist_works_while_availability_is_off(
    db: AsyncSession, practice: Practice
) -> None:
    """The waitlist is precisely what a client is offered when bookings are
    closed, so it must not be gated on availability_on."""
    practice.availability_on = False
    await db.flush()

    client = await resolve_client(db, Channel.web, "wl@example.test")
    entry = await waitlist.join_waitlist(db, client_id=client.id, problem_text="private")
    assert entry.status.value == "new"


async def test_the_waitlist_lifecycle(db: AsyncSession) -> None:
    from app.core.errors import InvalidTransition

    client = await resolve_client(db, Channel.web, "wl2@example.test")
    entry = await waitlist.join_waitlist(db, client_id=client.id)

    contacted = await waitlist.mark_contacted(db, entry.id, admin_note="rang them")
    assert contacted.contacted_at is not None

    converted = await waitlist.mark_converted(db, entry.id)
    assert converted.status.value == "converted"

    # Terminal.
    with pytest.raises(InvalidTransition):
        await waitlist.close_entry(db, entry.id)


async def test_a_new_waitlist_entry_cannot_skip_straight_to_converted(
    db: AsyncSession,
) -> None:
    from app.core.errors import InvalidTransition

    client = await resolve_client(db, Channel.web, "wl3@example.test")
    entry = await waitlist.join_waitlist(db, client_id=client.id)

    with pytest.raises(InvalidTransition):
        await waitlist.mark_converted(db, entry.id)
