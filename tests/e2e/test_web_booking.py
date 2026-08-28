"""End-to-end web booking (IMPLEMENTATION.md §12.1, M6 acceptance).

The acceptance criterion is that a client with no Telegram can book end to end
when SMTP is configured. These drive the real ASGI app through the real routes,
so they commit like production does -- everything created is cleaned up at the
end rather than rolled back.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import NullPool, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.channels.web.client import LANG_COOKIE
from app.channels.web.security import CLIENT_COOKIE, CSRF_COOKIE
from app.config import get_settings
from app.core.enums import Channel, RequestStatus, SlotStatus, TokenPurpose
from app.core.models import (
    AuthToken,
    BookingRequest,
    Client,
    Identity,
    OutboxMessage,
    Practice,
    SessionType,
    Slot,
    WaitlistEntry,
)
from app.main import create_app

EMAIL = "web-e2e@example.test"


@pytest_asyncio.fixture
async def committed() -> AsyncIterator[AsyncSession]:
    """A session that really commits, for setting up and tearing down."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    # expire_on_commit=False: reading an attribute after commit would otherwise
    # trigger a lazy refresh, and a lazy refresh outside the async context is
    # exactly what MissingGreenlet means.
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def web_slot(committed: AsyncSession) -> AsyncIterator[int]:
    """One available slot, committed so the ASGI app can see it."""
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    slot = Slot(
        practice_id=practice.id,
        starts_at=datetime.now(UTC) + timedelta(days=21, microseconds=7),
        duration_min=60,
        status=SlotStatus.available,
    )
    committed.add(slot)
    await committed.commit()
    slot_id = int(slot.id)

    yield slot_id

    # Tear down in dependency order. `booking_request.slot_id` has no ON DELETE,
    # so the request has to go before the slot it points at -- reminders,
    # negotiation messages, and flow_state cascade from it.
    client_ids = (
        (await committed.execute(select(Identity.client_id).where(Identity.external_id == EMAIL)))
        .scalars()
        .all()
    )

    doomed = set(
        (
            await committed.execute(
                select(BookingRequest.id).where(BookingRequest.slot_id == slot_id)
            )
        )
        .scalars()
        .all()
    )
    if client_ids:
        doomed |= set(
            (
                await committed.execute(
                    select(BookingRequest.id).where(BookingRequest.client_id.in_(client_ids))
                )
            )
            .scalars()
            .all()
        )

    if doomed:
        # Release every slot those requests hold, first. §6.5 makes
        # `slot.held_by_request` ON DELETE SET NULL, but §6.4's CHECK says a
        # `held` slot must have an owner -- so deleting a request out from
        # under a held slot violates the constraint. Production never
        # hard-deletes requests (retention nulls the text and keeps the row),
        # so this only ever bites cleanup like this.
        await committed.execute(
            update(Slot)
            .where(Slot.held_by_request.in_(doomed) | Slot.booked_request.in_(doomed))
            .values(
                status=SlotStatus.available,
                hold_expires_at=None,
                held_by_request=None,
                booked_request=None,
            )
        )
        await committed.execute(delete(OutboxMessage).where(OutboxMessage.request_id.in_(doomed)))

    if client_ids:
        await committed.execute(
            delete(OutboxMessage).where(OutboxMessage.client_id.in_(client_ids))
        )
        await committed.execute(delete(AuthToken).where(AuthToken.client_id.in_(client_ids)))
        # §12.1's way out leaves one of these, and `waitlist_entry.client_id`
        # has no ON DELETE -- so a client who took it cannot be removed until
        # the entry is. Without this the *next* test finds two clients on the
        # shared address and fails somewhere unrelated.
        entry_ids = (
            (
                await committed.execute(
                    select(WaitlistEntry.id).where(WaitlistEntry.client_id.in_(client_ids))
                )
            )
            .scalars()
            .all()
        )
        # The admin's copy of the join goes to TELEGRAM_ADMIN_IDS, so it carries
        # neither `client_id` nor `request_id` -- every other delete here filters
        # on one of those and misses it. Left behind, it is a `dead` row against
        # a chat id that exists only in conftest: the health page turns red and
        # the worker tells the real therapist a message could not be delivered.
        for entry_id in entry_ids:
            await committed.execute(
                delete(OutboxMessage).where(
                    OutboxMessage.dedupe_key.like(f"waitlist-admin:{entry_id}:%")
                )
            )
        await committed.execute(
            delete(WaitlistEntry).where(WaitlistEntry.client_id.in_(client_ids))
        )

    if doomed:
        await committed.execute(delete(BookingRequest).where(BookingRequest.id.in_(doomed)))
    await committed.execute(delete(Slot).where(Slot.id == slot_id))

    if client_ids:
        await committed.execute(delete(Identity).where(Identity.client_id.in_(client_ids)))
        await committed.execute(delete(Client).where(Client.id.in_(client_ids)))
    await committed.commit()


@pytest_asyncio.fixture
async def free_text_off(committed: AsyncSession) -> AsyncIterator[None]:
    """§12.1's gate closed, and reliably reopened.

    Restored in teardown rather than at the end of a test body: these tests
    commit like production, so a test that fails before its own cleanup leaves
    the practice switched over for every test after it -- which then fails
    somewhere with no connection to the setting at all.
    """
    await committed.execute(update(Practice).values(fallback_to_negotiation=False))
    await committed.commit()
    try:
        yield
    finally:
        await committed.rollback()
        await committed.execute(update(Practice).values(fallback_to_negotiation=True))
        await committed.commit()


@pytest.fixture
def web(email_enabled: None) -> Iterator[TestClient]:
    """The real app, with the email channel on -- M6's acceptance says "when
    SMTP is configured"."""
    # https base_url: BASE_URL is https in the test environment, so the session
    # and CSRF cookies are Secure and a plain-http client would silently drop
    # them -- exactly the "login does nothing" failure Secure cookies cause.
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


def _csrf(client: TestClient) -> str:
    """The double-submit token, read from the cookie the last page set."""
    return client.cookies.get(CSRF_COOKIE, "")


# --- The acceptance path ----------------------------------------------------


async def test_a_client_with_no_telegram_can_book_end_to_end(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """M6 acceptance, stated as §19 puts it."""
    session_type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()

    # Step 1: session type and modality.
    assert web.get("/book").status_code == 200

    # Step 2: the HTMX slot partial, in the client's timezone.
    partial = web.get(
        "/book/slots",
        params={
            "session_type_id": session_type_id,
            "modality": "online",
            "tz": "Europe/Moscow",
        },
    )
    assert partial.status_code == 200
    assert str(web_slot) in partial.text

    # Hold, then details.
    held = web.post(
        "/book/hold",
        data={
            "csrf_token": _csrf(web),
            "slot_id": web_slot,
            "session_type_id": session_type_id,
            "modality": "online",
            "tz": "Europe/Moscow",
        },
        follow_redirects=False,
    )
    assert held.status_code == 303
    assert web.get("/book/details").status_code == 200

    # Step 3 and submit. No Telegram anywhere in this flow.
    done = web.post(
        "/book",
        data={
            "csrf_token": _csrf(web),
            "problem": "I would like to talk about work stress",
            "name": "Anna",
            "contact": "email is fine",
            "email": EMAIL,
        },
    )
    assert done.status_code == 200

    # The app committed on its own connection. End this session's open
    # transaction so the next read starts from a fresh snapshot -- expire_all()
    # alone drops the cached objects but keeps the transaction.
    await committed.rollback()

    identity = (
        await committed.execute(select(Identity).where(Identity.external_id == EMAIL))
    ).scalar_one()
    request = (
        await committed.execute(
            select(BookingRequest).where(BookingRequest.client_id == identity.client_id)
        )
    ).scalar_one()

    assert request.status is RequestStatus.pending
    assert request.source_channel is Channel.web
    assert request.slot_id == web_slot
    assert request.problem_text == "I would like to talk about work stress"
    assert request.client_timezone == "Europe/Moscow"

    slot = (await committed.execute(select(Slot).where(Slot.id == web_slot))).scalar_one()
    assert slot.status is SlotStatus.held
    assert slot.held_by_request == request.id

    # §12.1: the confirmation carries the request UUID.
    assert str(request.uuid) in done.text


async def test_the_confirmation_offers_the_telegram_merge_link(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """DESIGN.md §5.1: one tap to attach Telegram -- but only once the address
    has proved itself.

    The deep link attaches a Telegram account to this client permanently, and
    §13.3 then routes every notification to it. Offered on the confirmation page
    of an unverified submission, typing a stranger's address would have handed
    over their account: `resolve_client` returns the *existing* client when the
    address is already known. It goes in the sign-in email instead, which only
    the address owner can read.
    """
    session_type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()
    web.get("/book")
    web.post(
        "/book/hold",
        data={
            "csrf_token": _csrf(web),
            "slot_id": web_slot,
            "session_type_id": session_type_id,
            "modality": "online",
            "tz": "Europe/Moscow",
        },
    )
    done = web.post(
        "/book",
        data={"csrf_token": _csrf(web), "problem": "", "name": "", "contact": "", "email": EMAIL},
    )
    assert "t.me/" not in done.text
    assert "start=link_" not in done.text

    # It is in the sign-in email the submission queues instead.
    await committed.rollback()
    identity = (
        await committed.execute(select(Identity).where(Identity.external_id == EMAIL))
    ).scalar_one()
    link = (
        await committed.execute(
            select(OutboxMessage).where(
                OutboxMessage.client_id == identity.client_id,
                OutboxMessage.intent_key == "auth.login_link.client",
            )
        )
    ).scalar_one()

    assert "start=link_" in link.payload["telegram_url"]
    assert link.address == EMAIL


async def test_submitting_does_not_sign_the_browser_in(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """The address is typed, not proved. `resolve_client` hands back the client
    that already owns it, so a session issued here would sign the browser in as
    whoever that is -- no link followed, no code entered.
    """
    session_type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()
    web.get("/book")
    web.post(
        "/book/hold",
        data={
            "csrf_token": _csrf(web),
            "slot_id": web_slot,
            "session_type_id": session_type_id,
            "modality": "online",
            "tz": "Europe/Moscow",
        },
    )
    web.post(
        "/book",
        data={"csrf_token": _csrf(web), "problem": "", "name": "", "contact": "", "email": EMAIL},
    )

    await committed.rollback()
    request = (
        await committed.execute(select(BookingRequest).order_by(BookingRequest.id.desc()).limit(1))
    ).scalar_one()

    # The booking still happened -- §12.1 is explicit that verification is never
    # a precondition for it. Only the sign-in is withheld.
    assert request.status is RequestStatus.pending
    assert web.get(f"/r/{request.uuid}", follow_redirects=False).status_code == 303


async def test_submitting_writes_outbox_rows_rather_than_sending(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """Hard rule 2, through the real route."""
    session_type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()
    web.get("/book")
    web.post(
        "/book/hold",
        data={
            "csrf_token": _csrf(web),
            "slot_id": web_slot,
            "session_type_id": session_type_id,
            "modality": "online",
            "tz": "Europe/Moscow",
        },
    )
    web.post(
        "/book",
        data={"csrf_token": _csrf(web), "problem": "", "name": "", "contact": "", "email": EMAIL},
    )

    identity = (
        await committed.execute(select(Identity).where(Identity.external_id == EMAIL))
    ).scalar_one()
    rows = (
        (
            await committed.execute(
                select(OutboxMessage).where(OutboxMessage.client_id == identity.client_id)
            )
        )
        .scalars()
        .all()
    )
    # The email identity is unverified until the magic link is followed, so
    # nothing about the *booking* is addressed to it (DESIGN.md §5.1). The admin
    # still hears. The one exception is the sign-in link itself, which §13.3
    # allows precisely because following it is what verifies the address -- and
    # without it an unverified client has no way back to their own request.
    assert all(
        row.channel is not Channel.email or row.intent_key == "auth.login_link.client"
        for row in rows
    )
    assert any(row.intent_key == "auth.login_link.client" for row in rows)


# --- Magic-link auth --------------------------------------------------------


async def test_the_magic_link_flow_signs_a_client_in(
    web: TestClient, committed: AsyncSession
) -> None:
    web.get("/auth/email")
    response = web.post("/auth/email", data={"csrf_token": _csrf(web), "email": EMAIL})
    assert response.status_code == 200

    identity = (
        await committed.execute(select(Identity).where(Identity.external_id == EMAIL))
    ).scalar_one()
    assert identity.verified_at is None  # not yet

    # The raw token never leaves the database, so mint one the same way the
    # route did and consume it through the callback.
    from app.core.services.clients import issue_token

    raw = await issue_token(
        committed, TokenPurpose.login, client_id=identity.client_id, payload={"email": EMAIL}
    )
    await committed.commit()

    callback = web.get("/auth/callback", params={"token": raw}, follow_redirects=False)
    assert callback.status_code == 303
    assert web.cookies.get(CLIENT_COOKIE)

    await committed.refresh(identity)
    # Following the link is what proves the address.
    assert identity.verified_at is not None

    await committed.execute(
        delete(OutboxMessage).where(OutboxMessage.client_id == identity.client_id)
    )
    await committed.execute(delete(AuthToken).where(AuthToken.client_id == identity.client_id))
    await committed.execute(delete(Identity).where(Identity.id == identity.id))
    await committed.execute(delete(Client).where(Client.id == identity.client_id))
    await committed.commit()


async def test_a_client_can_open_a_request_from_a_link_and_add_information(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """§12.1: the emailed link opens the request, and §7.1's note is postable
    from the page it opens."""
    from app.core.enums import NegotiationKind
    from app.core.models import NegotiationMessage
    from app.core.services.clients import issue_token

    session_type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()
    web.get("/book")
    web.post(
        "/book/hold",
        data={
            "csrf_token": _csrf(web),
            "slot_id": web_slot,
            "session_type_id": session_type_id,
            "modality": "online",
            "tz": "Europe/Moscow",
        },
        follow_redirects=False,
    )
    web.get("/book/details")
    web.post("/book", data={"csrf_token": _csrf(web), "problem": "x", "email": EMAIL})
    await committed.rollback()

    identity = (
        await committed.execute(select(Identity).where(Identity.external_id == EMAIL))
    ).scalar_one()
    request = (
        await committed.execute(
            select(BookingRequest).where(BookingRequest.client_id == identity.client_id)
        )
    ).scalar_one()

    # Read off what the assertions need: the rollback below expires the objects.
    request_id, request_uuid = request.id, request.uuid
    assert request.scheduled_start is None  # not approved, so no scheduled time

    # The link as the notification builds it: a view token, no session yet.
    raw = await issue_token(
        committed, TokenPurpose.view_request, client_id=identity.client_id
    )
    await committed.commit()

    web.cookies.delete(CLIENT_COOKIE)
    page = web.get(f"/r/{request_uuid}", params={"token": raw})
    assert page.status_code == 200
    assert f"/r/{request_uuid}/note" in page.text

    # The time is the point of the page, and it is shown before approval too --
    # `scheduled_start` is not set yet, so it comes from the held slot (§7.1).
    slot = (await committed.execute(select(Slot).where(Slot.id == web_slot))).scalar_one()
    local = slot.starts_at.astimezone(ZoneInfo("Europe/Moscow"))
    assert local.strftime("%Y-%m-%d %H:%M") in page.text
    # In the zone the client booked in, and saying which zone that is.
    assert "Europe/Moscow" in page.text
    # Consuming the token started a session, so the form on the page can post.
    assert web.cookies.get(CLIENT_COOKIE)

    posted = web.post(
        f"/r/{request_uuid}/note",
        data={"csrf_token": _csrf(web), "body": "I may be five minutes late"},
        follow_redirects=False,
    )
    assert posted.status_code == 303

    await committed.rollback()
    note = (
        await committed.execute(
            select(NegotiationMessage).where(
                NegotiationMessage.request_id == request_id,
                NegotiationMessage.kind == NegotiationKind.note,
            )
        )
    ).scalar_one()
    assert note.body_text == "I may be five minutes late"
    assert web.get(f"/r/{request_uuid}").text.count("five minutes late") == 1

    # The status did not move (§7.1).
    still = (
        await committed.execute(select(BookingRequest).where(BookingRequest.id == request_id))
    ).scalar_one()
    assert still.status is RequestStatus.pending
    web.cookies.delete(CLIENT_COOKIE)


async def test_the_login_link_is_queued_to_the_address_it_is_about(
    web: TestClient, committed: AsyncSession, email_enabled: None
) -> None:
    """§13.3: this intent addresses itself. Under the general policy the row is
    never written, because the address it proves is unverified by definition."""
    address = "link-target@example.test"
    web.get("/auth/email")
    assert web.post("/auth/email", data={"csrf_token": _csrf(web), "email": address}).status_code

    row = (
        await committed.execute(
            select(OutboxMessage)
            .where(
                OutboxMessage.intent_key == "auth.login_link.client",
                OutboxMessage.address == address,
            )
            .order_by(OutboxMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert row.channel is Channel.email

    identity = (
        await committed.execute(select(Identity).where(Identity.external_id == address))
    ).scalar_one()
    await committed.execute(delete(OutboxMessage).where(OutboxMessage.address == address))
    await committed.execute(delete(AuthToken).where(AuthToken.client_id == identity.client_id))
    await committed.execute(delete(Identity).where(Identity.id == identity.id))
    await committed.execute(delete(Client).where(Client.id == identity.client_id))
    await committed.commit()


async def test_a_link_for_an_address_owned_by_someone_else_is_refused(
    web: TestClient, committed: AsyncSession
) -> None:
    """§13.1 step 7 lets a Telegram client name any address, so the callback has
    to answer for one that turns out to belong to another person."""
    from app.core.services.clients import issue_token, resolve_client

    owner = await resolve_client(committed, Channel.email, "owned@example.test")
    stranger = await resolve_client(committed, Channel.telegram, "909090", verified=True)
    raw = await issue_token(
        committed,
        TokenPurpose.login,
        client_id=stranger.id,
        payload={"email": "owned@example.test"},
    )
    await committed.commit()

    response = web.get("/auth/callback", params={"token": raw}, follow_redirects=False)
    assert response.status_code == 400

    identity = (
        await committed.execute(
            select(Identity).where(Identity.external_id == "owned@example.test")
        )
    ).scalar_one()
    assert identity.client_id == owner.id  # not reassigned

    await committed.execute(delete(AuthToken).where(AuthToken.client_id == stranger.id))
    await committed.execute(delete(Identity).where(Identity.client_id.in_([owner.id, stranger.id])))
    await committed.execute(delete(Client).where(Client.id.in_([owner.id, stranger.id])))
    await committed.commit()


def test_an_invalid_login_token_is_refused(web: TestClient) -> None:
    response = web.get("/auth/callback", params={"token": "not-a-real-token"})
    assert response.status_code == 400


async def test_the_sign_in_page_says_the_same_thing_for_any_address(
    web: TestClient, committed: AsyncSession
) -> None:
    """Whether an address is known here is not something an unauthenticated
    caller should learn."""
    stranger = "nobody@example.test"
    web.get("/auth/email")
    known = web.post("/auth/email", data={"csrf_token": _csrf(web), "email": EMAIL})
    unknown = web.post("/auth/email", data={"csrf_token": _csrf(web), "email": stranger})
    assert known.status_code == unknown.status_code == 200

    # Asking for a link now really does queue one, so the stranger this test
    # invents has to be cleared up like any other committed row.
    rows = await committed.execute(
        select(Identity.client_id).where(Identity.external_id == stranger)
    )
    client_ids = rows.scalars().all()
    if client_ids:
        await committed.execute(delete(OutboxMessage).where(OutboxMessage.address == stranger))
        await committed.execute(delete(AuthToken).where(AuthToken.client_id.in_(client_ids)))
        await committed.execute(delete(Identity).where(Identity.client_id.in_(client_ids)))
        await committed.execute(delete(Client).where(Client.id.in_(client_ids)))
        await committed.commit()


# --- Authorisation ----------------------------------------------------------


def test_the_request_page_requires_authentication(web: TestClient) -> None:
    """§12.1: auth required."""
    response = web.get("/r/11111111-1111-1111-1111-111111111111", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/email"


# --- CSRF (§17) -------------------------------------------------------------


def test_a_mutating_post_without_a_csrf_token_is_refused(web: TestClient, web_slot: int) -> None:
    web.get("/book")
    for path, payload in (
        ("/book/hold", {"slot_id": web_slot, "session_type_id": 1}),
        ("/book", {"email": EMAIL}),
        ("/waitlist", {"email": EMAIL}),
        ("/auth/email", {"email": EMAIL}),
    ):
        response = web.post(path, data=payload)
        assert response.status_code == 403, f"{path} accepted a request with no token"


def test_a_forged_csrf_token_is_refused(web: TestClient, web_slot: int) -> None:
    web.get("/book")
    response = web.post(
        "/book/hold",
        data={"csrf_token": "forged.value", "slot_id": web_slot, "session_type_id": 1},
    )
    assert response.status_code == 403


# --- Pages ------------------------------------------------------------------


def test_the_home_page_lists_the_menu_topics(web: TestClient) -> None:
    response = web.get("/")
    assert response.status_code == 200
    assert "/t/work_terms" in response.text
    # `references` is not in the menu (§20).
    assert "/t/references" not in response.text


def test_an_unknown_topic_is_a_404(web: TestClient) -> None:
    assert web.get("/t/no_such_topic").status_code == 404


def test_the_language_switcher_offers_ru_and_hy_never_am(web: TestClient) -> None:
    """Hard rule 5."""
    body = web.get("/").text
    assert 'value="ru"' in body
    assert 'value="hy"' in body
    assert 'value="am"' not in body


def test_the_page_language_can_be_switched(web: TestClient) -> None:
    assert 'lang="hy"' in web.get("/", params={"lang": "hy"}).text
    assert 'lang="ru"' in web.get("/", params={"lang": "ru"}).text


def test_a_switched_language_survives_the_next_link(web: TestClient) -> None:
    """The switcher is a setting. No link on a page carries `?lang=`, so
    without this the choice lasted exactly one request."""
    web.get("/", params={"lang": "hy"})
    assert web.cookies.get(LANG_COOKIE) == "hy"

    assert 'lang="hy"' in web.get("/").text
    assert 'lang="hy"' in web.get("/t/qualification").text

    # And an explicit choice still wins over the remembered one.
    assert 'lang="ru"' in web.get("/t/qualification", params={"lang": "ru"}).text
    assert 'lang="ru"' in web.get("/t/qualification").text
    web.cookies.delete(LANG_COOKIE)


def test_an_unasked_for_page_does_not_pin_a_language(web: TestClient) -> None:
    """Only an explicit switch writes the cookie; otherwise changing the
    practice default would never reach anyone who had visited before."""
    assert web.get("/").status_code == 200
    assert web.cookies.get(LANG_COOKIE) is None


async def test_a_signed_in_client_keeps_the_language_they_chose(
    web: TestClient, committed: AsyncSession
) -> None:
    """One person, one language, whichever channel they are on (§13.1 step 2)."""
    from app.core.services.clients import issue_token, resolve_client

    address = "lang-pref@example.test"
    client = await resolve_client(committed, Channel.email, address)
    client.language = "hy"
    raw = await issue_token(
        committed, TokenPurpose.login, client_id=client.id, payload={"email": address}
    )
    await committed.commit()

    assert web.get("/auth/callback", params={"token": raw}).status_code == 200
    try:
        # No cookie, no query: the client's own language is the default now.
        assert 'lang="hy"' in web.get("/").text

        # Switching on the web writes it back, so Telegram agrees next time.
        web.get("/", params={"lang": "ru"})
        await committed.refresh(client)
        assert client.language == "ru"
    finally:
        web.cookies.delete(CLIENT_COOKIE)
        web.cookies.delete(LANG_COOKIE)
        await committed.execute(delete(AuthToken).where(AuthToken.client_id == client.id))
        await committed.execute(delete(Identity).where(Identity.client_id == client.id))
        await committed.execute(delete(Client).where(Client.id == client.id))
        await committed.commit()


def test_htmx_is_served_locally_not_from_a_cdn(web: TestClient) -> None:
    """No build step, and no third-party origin on the client page."""
    body = web.get("/book").text
    assert "/static/htmx.min.js" in body
    assert "unpkg.com" not in body
    assert "cdn." not in body
    assert web.get("/static/htmx.min.js").status_code == 200


def test_the_booking_page_detects_and_offers_a_timezone(web: TestClient) -> None:
    """§12.1: detected client-side, with a visible selector to override."""
    body = web.get("/book").text
    assert "resolvedOptions().timeZone" in body
    assert 'name="tz"' in body
    assert "Europe/Moscow" in body


def test_the_slot_picker_heads_its_days_in_the_clients_language(
    web: TestClient, web_slot: int
) -> None:
    """§15: `strftime` speaks the process locale, so these read "Thursday 27
    August" to a Russian client until the names came from the catalogue."""
    partial = web.get(
        "/book/slots",
        params={"session_type_id": 1, "modality": "online", "tz": "Europe/Moscow"},
    )

    assert partial.status_code == 200
    assert re.search(r"(янв|фев|мар|апр|мая|июн|июл|авг|сен|окт|ноя|дек)", partial.text)
    assert not re.search(r"(January|February|August|September|October)", partial.text)


async def test_the_request_page_names_the_status_in_words(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """§15: `pending` is a database word, not something to show a client."""
    from app.core.services.clients import issue_token
    from app.core.services.translations import get_text

    session_type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()
    web.get("/book")
    web.post(
        "/book/hold",
        data={
            "csrf_token": _csrf(web),
            "slot_id": web_slot,
            "session_type_id": session_type_id,
            "modality": "online",
            "tz": "Europe/Moscow",
        },
        follow_redirects=False,
    )
    web.get("/book/details")
    web.post("/book", data={"csrf_token": _csrf(web), "problem": "x", "email": EMAIL})
    await committed.rollback()

    identity = (
        await committed.execute(select(Identity).where(Identity.external_id == EMAIL))
    ).scalar_one()
    request = (
        await committed.execute(
            select(BookingRequest).where(BookingRequest.client_id == identity.client_id)
        )
    ).scalar_one()
    raw = await issue_token(
        committed, TokenPurpose.view_request, client_id=identity.client_id
    )
    await committed.commit()

    web.cookies.delete(CLIENT_COOKIE)
    page = web.get(f"/r/{request.uuid}", params={"token": raw})

    expected = await get_text(committed, "ru", "request.status.pending")
    assert expected in page.text
    assert ">pending<" not in page.text
    web.cookies.delete(CLIENT_COOKIE)


def test_the_booking_page_fills_its_placeholders(web: TestClient) -> None:
    """§15: a placeholder must never reach a client's screen unfilled."""
    body = web.get("/book", params={"tz": "Europe/Berlin"}).text
    legends = re.findall(r"<legend>(.*?)</legend>", body, re.S)
    assert any("Europe/Berlin" in legend for legend in legends)
    assert "{timezone}" not in body


async def test_the_hold_notice_names_the_hold_window(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """The same rule on step 3: {minutes} is the window the client is racing."""
    session_type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()

    web.get("/book")
    web.post(
        "/book/hold",
        data={
            "csrf_token": _csrf(web),
            "slot_id": web_slot,
            "session_type_id": session_type_id,
            "modality": "online",
            "tz": "Europe/Moscow",
        },
        follow_redirects=False,
    )
    body = web.get("/book/details").text
    notice = re.search(r'<p class="held">(.*?)</p>', body, re.S)
    assert notice is not None
    assert str(practice.slot_hold_minutes) in notice.group(1)
    assert "{minutes}" not in body


async def test_the_free_text_fields_carry_the_cap_the_core_enforces(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """§17. The number has to reach the markup, not merely be referenced by it:
    an undefined name renders as the empty string in Jinja, so a broken context
    key would leave `maxlength=""` behind and every test would still pass.
    """
    from app.core.policies import CLIENT_TEXT_MAX_CHARS

    session_type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()

    web.get("/book")
    web.post(
        "/book/hold",
        data={
            "csrf_token": _csrf(web),
            "slot_id": web_slot,
            "session_type_id": session_type_id,
            "modality": "online",
            "tz": "Europe/Moscow",
        },
        follow_redirects=False,
    )
    body = web.get("/book/details").text

    # Both free-text fields: the problem description and the contact note.
    assert body.count(f'maxlength="{CLIENT_TEXT_MAX_CHARS}"') == 2
    assert 'maxlength=""' not in body
    assert "[data-counter]" in body, "the counter script has to be on the page too"


async def test_onsite_selection_shows_the_clinic_address(
    web: TestClient, committed: AsyncSession
) -> None:
    """§12.1 states this explicitly."""
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    practice.clinic_onsite_url = "https://maps.example.test/clinic"
    await committed.commit()
    try:
        body = web.get("/book").text
        assert "https://maps.example.test/clinic" in body
        assert 'id="onsite-info"' in body
    finally:
        practice.clinic_onsite_url = None
        await committed.commit()


# --- Answering a proposal on the web (§12.1) --------------------------------


async def _proposed(web: TestClient, committed: AsyncSession, web_slot: int) -> BookingRequest:
    """A booked request the therapist has proposed another time for, with the
    browser signed in as the client who made it."""
    from app.core.services import booking
    from app.core.services.clients import issue_token

    session_type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()
    web.get("/book")
    web.post(
        "/book/hold",
        data={
            "csrf_token": _csrf(web),
            "slot_id": web_slot,
            "session_type_id": session_type_id,
            "modality": "online",
            "tz": "Europe/Moscow",
        },
        follow_redirects=False,
    )
    web.get("/book/details")
    web.post("/book", data={"csrf_token": _csrf(web), "problem": "x", "email": EMAIL})
    await committed.rollback()

    identity = (
        await committed.execute(select(Identity).where(Identity.external_id == EMAIL))
    ).scalar_one()
    request = (
        await committed.execute(
            select(BookingRequest).where(BookingRequest.client_id == identity.client_id)
        )
    ).scalar_one()

    # A proposal for another time, which releases the held slot (§7.1) and puts
    # it back among the times the client can be offered.
    await booking.admin_propose(
        committed, request.id, proposed_start=datetime.now(UTC) + timedelta(days=9)
    )
    raw = await issue_token(
        committed, TokenPurpose.view_request, client_id=identity.client_id
    )
    await committed.commit()

    web.get(f"/r/{request.uuid}", params={"token": raw})
    return request


async def test_a_client_answering_a_proposal_is_offered_the_free_slots(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """The reported fault: one text input and no hint, so a client with no
    reason to guess `2026-09-02 18:00` wrote a sentence, no instant was
    recorded, and the therapist had to turn the words into a time by hand."""
    request = await _proposed(web, committed, web_slot)

    page = web.get(f"/r/{request.uuid}")
    assert page.status_code == 200
    assert f'name="slot_id" value="{web_slot}"' in page.text
    # And a picker for a time of their own, since the practice accepts one.
    assert 'type="datetime-local"' in page.text


async def test_countering_with_a_slot_records_its_instant(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    from app.core.enums import NegotiationKind
    from app.core.models import NegotiationMessage

    request = await _proposed(web, committed, web_slot)

    posted = web.post(
        f"/r/{request.uuid}/counter",
        data={"csrf_token": _csrf(web), "slot_id": web_slot},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert "refused" not in (posted.headers.get("location") or "")

    await committed.rollback()
    slot = (await committed.execute(select(Slot).where(Slot.id == web_slot))).scalar_one()
    last = (
        await committed.execute(
            select(NegotiationMessage)
            .where(NegotiationMessage.request_id == request.id)
            .order_by(NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert last.kind is NegotiationKind.counter
    assert last.proposed_start == slot.starts_at
    # §12.1: nothing is held by a counter.
    assert slot.status is SlotStatus.available


async def test_countering_with_the_picker_records_the_clients_own_time(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """A `datetime-local` is read in the client's own zone, not the practice's:
    the picker sits under times already rendered in it (DESIGN.md §8)."""
    from app.core.models import NegotiationMessage

    request = await _proposed(web, committed, web_slot)

    web.post(
        f"/r/{request.uuid}/counter",
        data={
            "csrf_token": _csrf(web),
            "proposed_start": "2027-05-14T16:30",
            "body": "afternoons do not work for me",
        },
        follow_redirects=False,
    )

    await committed.rollback()
    last = (
        await committed.execute(
            select(NegotiationMessage)
            .where(NegotiationMessage.request_id == request.id)
            .order_by(NegotiationMessage.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert last.proposed_start is not None
    assert last.proposed_start == datetime(2027, 5, 14, 16, 30, tzinfo=ZoneInfo("Europe/Moscow"))
    # §9 keeps the words either way.
    assert last.body_text == "afternoons do not work for me"


async def test_a_slot_taken_since_the_page_was_drawn_is_refused(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """§12.1: a proposal for a time that is no longer an opening is worse than
    no proposal, and §12.2 requires a refusal to say so."""
    from sqlalchemy import func

    from app.core.models import NegotiationMessage

    request = await _proposed(web, committed, web_slot)
    # Read off what the assertions need: the rollback below expires the object,
    # and an expired attribute lazy-loads, which outside the async context is
    # what MissingGreenlet means.
    request_id, request_uuid = request.id, request.uuid

    await committed.execute(
        update(Slot).where(Slot.id == web_slot).values(status=SlotStatus.blocked)
    )
    await committed.commit()

    counted = (
        select(func.count())
        .select_from(NegotiationMessage)
        .where(NegotiationMessage.request_id == request_id)
    )
    before = (await committed.execute(counted)).scalar_one()

    posted = web.post(
        f"/r/{request_uuid}/counter",
        data={"csrf_token": _csrf(web), "slot_id": web_slot},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert "refused=1" in (posted.headers.get("location") or "")

    await committed.rollback()
    assert (await committed.execute(counted)).scalar_one() == before


async def test_with_free_text_off_the_page_offers_the_waitlist_instead(
    web: TestClient, web_slot: int, committed: AsyncSession, free_text_off: None
) -> None:
    """§12.1: accept and decline must not be the only replies, or a client who
    cannot make the proposed time has to reject their own request to say so."""
    request = await _proposed(web, committed, web_slot)

    page = web.get(f"/r/{request.uuid}")
    assert f"/r/{request.uuid}/waitlist" in page.text
    assert 'type="datetime-local"' not in page.text
    # The slots are still offered; only the words are gated.
    assert f'name="slot_id" value="{web_slot}"' in page.text

    posted = web.post(
        f"/r/{request.uuid}/waitlist",
        data={"csrf_token": _csrf(web)},
        follow_redirects=False,
    )
    assert posted.status_code == 303

    # `expire_on_commit=False` means the identity map would hand back the same
    # object with the status it had before the app committed -- `expunge_all`
    # is what makes the next read a read.
    await committed.rollback()
    committed.expunge_all()
    closed = (
        await committed.execute(select(BookingRequest).where(BookingRequest.id == request.id))
    ).scalar_one()
    assert closed.status is RequestStatus.rejected
    entries = (
        (
            await committed.execute(
                select(WaitlistEntry).where(WaitlistEntry.client_id == closed.client_id)
            )
        )
        .scalars()
        .all()
    )
    assert entries, "they asked to be told when something opens"
