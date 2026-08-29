"""Clients, identities, and tokens (DESIGN.md §5, IMPLEMENTATION.md §8).

One person may arrive over Telegram and over email and has to end up as one
client, which puts the weight on identity. Resolution is get-or-create,
Telegram vouches for its own ids while an email address starts unverified,
addresses match case-insensitively, and an identity already pointing at a
client is never silently reassigned to another.

Tokens carry the same weight from the other side: only a hash is stored, a
token spends exactly once, and it is refused for the wrong purpose or past
its lifetime. The login and link lifetimes from DESIGN.md §5.1 are asserted
as values rather than behaviours, because shortening one silently is easy.

The merge is the largest group here. It must move every client-owned table --
there is a test that the list of them still matches the schema, so a new
table cannot be forgotten -- keep the survivor's own fields while filling
only its blanks, leave a map from the row it deleted, and refuse outright
while a flow is live or when either side has been erased.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel, Modality, RequestStatus, TokenPurpose
from app.core.errors import MergeRefused, TokenInvalid
from app.core.models import (
    AuditLog,
    AuthToken,
    BookingRequest,
    Client,
    FlowState,
    Identity,
    OutboxMessage,
    Practice,
    SessionType,
    WaitlistEntry,
)
from app.core.policies import now_utc
from app.core.services import clients, flow, waitlist
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


@pytest.mark.parametrize(
    "value",
    ["anna@example.test", "  Anna.B+tag@mail.example.co.uk  ", "a@b.cd"],
)
def test_an_address_shaped_like_an_address_is_accepted(value: str) -> None:
    assert clients.looks_like_email(value)


@pytest.mark.parametrize(
    "value",
    ["", "anna", "anna at example dot test", "anna@example", "anna@@example.test", "a b@c.de"],
)
def test_anything_else_is_refused(value: str) -> None:
    """A typo check for §13.1 step 7. The login link is what actually proves an
    address, so this only has to catch the obvious."""
    assert not clients.looks_like_email(value)


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


async def test_the_waitlist_is_rate_limited_like_any_other_submission(
    db: AsyncSession,
) -> None:
    """§17: 5 submissions an hour per client. The waitlist was the one path with
    nothing in front of it, and it is the path offered when bookings are closed
    -- so it is the one most available to somebody holding down the button.
    """
    from app.core.errors import RateLimited

    client = await resolve_client(db, Channel.web, "wl-flood@example.test")
    for _ in range(waitlist.JOINS_PER_HOUR):
        await waitlist.join_waitlist(db, client_id=client.id)

    with pytest.raises(RateLimited):
        await waitlist.join_waitlist(db, client_id=client.id)


async def test_the_waitlist_limit_is_per_client(db: AsyncSession) -> None:
    """The other half: a limit that counted every entry in the practice would
    close the waitlist to everyone as soon as one person filled it."""
    from app.core.errors import RateLimited

    noisy = await resolve_client(db, Channel.web, "wl-noisy@example.test")
    for _ in range(waitlist.JOINS_PER_HOUR):
        await waitlist.join_waitlist(db, client_id=noisy.id)
    with pytest.raises(RateLimited):
        await waitlist.join_waitlist(db, client_id=noisy.id)

    quiet = await resolve_client(db, Channel.web, "wl-quiet@example.test")
    entry = await waitlist.join_waitlist(db, client_id=quiet.id)

    assert entry.status.value == "new"


async def test_a_new_waitlist_entry_cannot_skip_straight_to_converted(
    db: AsyncSession,
) -> None:
    from app.core.errors import InvalidTransition

    client = await resolve_client(db, Channel.web, "wl3@example.test")
    entry = await waitlist.join_waitlist(db, client_id=client.id)

    with pytest.raises(InvalidTransition):
        await waitlist.mark_converted(db, entry.id)


# --- Merging two rows that are one person (DESIGN.md §5.1) ------------------


def test_the_client_owned_table_list_matches_the_schema() -> None:
    """A seventh table holding client data must fail here, loudly.

    `merge_clients` moves every table in `CLIENT_OWNED_TABLES` and
    `erase_client` empties every one of them. A table added to the schema
    without being added to that list is a client half-moved or half-forgotten,
    and neither leaves a trace at runtime.
    """
    from app.core.models import Base

    with_client = {
        name
        for name, table in Base.metadata.tables.items()
        for column in table.columns
        if any(fk.column.table.name == "client" for fk in column.foreign_keys)
    }
    assert with_client == set(clients.CLIENT_OWNED_TABLES)


async def _person_with_history(
    db: AsyncSession, practice: Practice, external_id: str, channel: Channel
) -> Client:
    """A client carrying a row in every table the merge has to move."""
    client = await resolve_client(db, channel, external_id, verified=True)
    session_type_id = (
        await db.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()

    db.add(
        BookingRequest(
            practice_id=practice.id,
            client_id=client.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            status=RequestStatus.pending,
            source_channel=Channel.web,
        )
    )
    db.add(
        WaitlistEntry(
            practice_id=practice.id, client_id=client.id, problem_text="something"
        )
    )
    db.add(
        OutboxMessage(
            practice_id=practice.id,
            client_id=client.id,
            channel=channel,
            address=external_id,
            intent_key="request.rejected.client",
            locale="ru",
            payload={},
        )
    )
    await issue_token(db, TokenPurpose.view_request, client_id=client.id)
    await flow.set_step(db, client.id, Channel.web, flow.Step.entering_problem)
    await db.flush()
    return client


async def test_a_merge_moves_every_table_and_deletes_the_absorbed_row(
    db: AsyncSession, practice: Practice
) -> None:
    survivor = await _person_with_history(db, practice, "merge-web@example.test", Channel.email)
    absorbed = await _person_with_history(db, practice, "918273645", Channel.telegram)

    # The flow rows are what the merge refuses on while they are live, so age
    # them past `slot_hold_minutes` first -- this test is about the moving.
    stale = now_utc() - timedelta(minutes=practice.slot_hold_minutes + 5)
    await db.execute(update(FlowState).values(updated_at=stale))
    await db.flush()

    result = await clients.merge_clients(db, into=survivor.id, absorbing=absorbed.id)
    assert result.id == survivor.id

    for model in (Identity, AuthToken, BookingRequest, WaitlistEntry, OutboxMessage):
        left = (
            await db.execute(select(model).where(model.client_id == absorbed.id))
        ).scalars().all()
        assert not left, f"{model.__tablename__} still points at the absorbed row"

    # The Telegram identity now reaches the survivor, which is what makes the
    # next update from that chat find the right person.
    identity = (
        await db.execute(select(Identity).where(Identity.external_id == "918273645"))
    ).scalar_one()
    assert identity.client_id == survivor.id

    assert (
        await db.execute(select(Client).where(Client.id == absorbed.id))
    ).scalar_one_or_none() is None


async def test_the_survivor_keeps_what_it_has_and_takes_only_blanks(
    db: AsyncSession, practice: Practice
) -> None:
    """A name typed into a web form must not overwrite the one Telegram
    vouches for, and the reverse is equally true."""
    survivor = await resolve_client(db, Channel.email, "keeps@example.test")
    survivor.display_name = "Anna"
    survivor.timezone = None
    absorbed = await resolve_client(db, Channel.telegram, "918273646", verified=True)
    absorbed.display_name = "anna from telegram"
    absorbed.timezone = "Asia/Yerevan"
    await db.flush()

    result = await clients.merge_clients(db, into=survivor.id, absorbing=absorbed.id)

    assert result.display_name == "Anna", "a set field is never overwritten"
    assert result.timezone == "Asia/Yerevan", "a blank one is filled"


async def test_a_live_flow_refuses_the_merge_and_spends_nothing(
    db: AsyncSession, practice: Practice
) -> None:
    """The client is told to finish what they started (§13.1).

    `flow_state` is UNIQUE per (client, channel), so a merge would have to drop
    one of two rows -- and the one it would drop holds half of something
    somebody is typing.
    """
    survivor = await resolve_client(db, Channel.email, "busy@example.test")
    absorbed = await resolve_client(db, Channel.telegram, "918273647", verified=True)
    await flow.set_step(db, absorbed.id, Channel.telegram, flow.Step.entering_problem)

    with pytest.raises(MergeRefused) as refused:
        await clients.merge_clients(db, into=survivor.id, absorbing=absorbed.id)
    assert refused.value.reason == "busy"

    # Nothing moved.
    assert (
        await db.execute(select(Client).where(Client.id == absorbed.id))
    ).scalar_one_or_none() is not None


async def test_a_flow_nobody_came_back_to_does_not_block_forever(
    db: AsyncSession, practice: Practice
) -> None:
    """Nothing sweeps `flow_state`, so without a bound "finish what you
    started" would be an instruction nobody could follow."""
    survivor = await resolve_client(db, Channel.email, "stale@example.test")
    absorbed = await resolve_client(db, Channel.telegram, "918273648", verified=True)
    await flow.set_step(db, absorbed.id, Channel.telegram, flow.Step.entering_problem)

    stale = now_utc() - timedelta(minutes=practice.slot_hold_minutes + 1)
    await db.execute(
        update(FlowState).where(FlowState.client_id == absorbed.id).values(updated_at=stale)
    )
    await db.flush()

    await clients.merge_clients(db, into=survivor.id, absorbing=absorbed.id)

    left = (
        await db.execute(select(FlowState).where(FlowState.client_id == absorbed.id))
    ).scalars().all()
    assert not left


async def test_an_erased_client_is_never_merged(db: AsyncSession, practice: Practice) -> None:
    """§16 is a promise, and a link minted before it must not undo it."""
    survivor = await resolve_client(db, Channel.email, "erased-a@example.test")
    absorbed = await resolve_client(db, Channel.telegram, "918273649", verified=True)
    absorbed.erased_at = now_utc()
    await db.flush()

    with pytest.raises(MergeRefused) as refused:
        await clients.merge_clients(db, into=survivor.id, absorbing=absorbed.id)
    assert refused.value.reason == "erased"

    # And in the other direction.
    absorbed.erased_at = None
    survivor.erased_at = now_utc()
    await db.flush()
    with pytest.raises(MergeRefused):
        await clients.merge_clients(db, into=survivor.id, absorbing=absorbed.id)


async def test_a_merge_leaves_the_map_from_the_deleted_row(
    db: AsyncSession, practice: Practice
) -> None:
    """Audit entries written before the merge still name the absorbed id, and
    `client` has no foreign key from `audit_log` to dangle on."""
    survivor = await resolve_client(db, Channel.email, "audit@example.test")
    absorbed = await resolve_client(db, Channel.telegram, "918273650", verified=True)

    await clients.merge_clients(db, into=survivor.id, absorbing=absorbed.id)

    entry = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == "client.merge", AuditLog.entity_id == str(survivor.id))
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entry.meta["absorbed"] == str(absorbed.id)


async def test_merging_a_client_into_itself_is_refused(db: AsyncSession) -> None:
    client = await resolve_client(db, Channel.email, "self@example.test")
    with pytest.raises(MergeRefused):
        await clients.merge_clients(db, into=client.id, absorbing=client.id)


async def test_a_token_can_be_read_without_being_spent(db: AsyncSession) -> None:
    """The confirmation screen has to name what it is joining before the client
    answers, and burning the token to draw it would leave "Not me" with a dead
    link (§6.2)."""
    client = await resolve_client(db, Channel.email, "peek@example.test")
    raw = await issue_token(db, TokenPurpose.link_channel, client_id=client.id)

    peeked = await clients.token_target(db, raw, TokenPurpose.link_channel)
    assert peeked is not None and peeked.client_id == client.id

    # Still spendable, and only once.
    assert (await consume_token(db, raw, TokenPurpose.link_channel)).client_id == client.id
    assert await clients.token_target(db, raw, TokenPurpose.link_channel) is None


async def test_peeking_refuses_the_wrong_purpose_like_consuming_does(
    db: AsyncSession,
) -> None:
    client = await resolve_client(db, Channel.email, "peek2@example.test")
    raw = await issue_token(db, TokenPurpose.login, client_id=client.id)
    assert await clients.token_target(db, raw, TokenPurpose.link_channel) is None
