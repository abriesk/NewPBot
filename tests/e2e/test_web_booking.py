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

    if doomed:
        await committed.execute(delete(BookingRequest).where(BookingRequest.id.in_(doomed)))
    await committed.execute(delete(Slot).where(Slot.id == slot_id))

    if client_ids:
        await committed.execute(delete(Identity).where(Identity.client_id.in_(client_ids)))
        await committed.execute(delete(Client).where(Client.id.in_(client_ids)))
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
    """DESIGN.md §5.1: one tap to attach Telegram, rather than asking the client
    to type their email into the bot."""
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
    assert "t.me/" in done.text
    assert "start=link_" in done.text


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
    # nothing is addressed to it yet (DESIGN.md §5.1). The admin still hears.
    assert all(row.channel is not Channel.email for row in rows)


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
