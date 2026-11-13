"""A web booking from an unproved address (IMPLEMENTATION.md §12.1, §17; NP2, REVIEW F3).

Short hold until verified: the slot is held for `unverified_hold_minutes`, the
therapist hears only when the login link is followed, an unfollowed booking
lapses and frees the slot, and a stranger typing a proved address files nothing
on that person's record.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel, RequestStatus, SlotStatus
from app.core.models import (
    BookingRequest,
    Identity,
    OutboxMessage,
    Practice,
    SessionType,
    Slot,
)
from app.core.services import booking
from app.core.services import slots as slot_service
from tests.e2e.test_web_booking import (  # noqa: F401  (fixtures)
    CLIENT_COOKIE,
    EMAIL,
    _csrf,
    _hold_a_slot,
    committed,
    web,
    web_slot,
)


async def _book(
    web: TestClient,
    committed: AsyncSession,
    slot_id: int,
    email: str = EMAIL,
    contact: str = "",
):
    type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()
    await _hold_a_slot(web, slot_id, type_id)
    return web.post(
        "/book",
        data={
            "csrf_token": _csrf(web),
            "problem": "p",
            "name": "N",
            "contact": contact,
            "email": email,
        },
    )


async def _request_for(committed: AsyncSession, slot_id: int) -> BookingRequest | None:
    await committed.rollback()
    committed.expunge_all()
    return (
        await committed.execute(select(BookingRequest).where(BookingRequest.slot_id == slot_id))
    ).scalar_one_or_none()


async def _slot(committed: AsyncSession, slot_id: int) -> Slot:
    await committed.rollback()
    committed.expunge_all()
    return (await committed.execute(select(Slot).where(Slot.id == slot_id))).scalar_one()


async def _admin_rows(committed: AsyncSession, request_id: int) -> list[OutboxMessage]:
    return list(
        (
            await committed.execute(
                select(OutboxMessage).where(
                    OutboxMessage.request_id == request_id,
                    OutboxMessage.intent_key == "request.submitted.admin",
                )
            )
        )
        .scalars()
        .all()
    )


async def _login_url(committed: AsyncSession, email: str) -> str:
    await committed.rollback()
    rows = (
        (
            await committed.execute(
                select(OutboxMessage)
                .where(
                    OutboxMessage.intent_key.like("auth.login%"),
                    OutboxMessage.address == email,
                )
                .order_by(OutboxMessage.id.desc())
            )
        )
        .scalars()
        .all()
    )
    assert rows, "no login link was queued"
    return str(rows[0].payload["url"])


def _follow(web: TestClient, url: str) -> None:
    from urllib.parse import parse_qs, urlparse

    from tests.e2e.test_web_booking import _follow_login

    token = parse_qs(urlparse(url).query)["token"][0]
    assert _follow_login(web, token, mode="book").status_code == 303


async def test_an_unverified_booking_holds_briefly_and_does_not_notify(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    response = await _book(web, committed, web_slot)
    assert response.status_code == 200

    request = await _request_for(committed, web_slot)
    assert request is not None and request.awaiting_verification
    request_id = request.id
    slot = await _slot(committed, web_slot)
    assert slot.status is SlotStatus.held
    left = slot.hold_expires_at - datetime.now(UTC)
    assert timedelta(minutes=13) < left <= timedelta(minutes=15, seconds=5), left
    assert not await _admin_rows(committed, request_id), "the therapist was told at submit"


async def test_following_the_link_notifies_and_extends_to_the_normal_hold(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    await _book(web, committed, web_slot)
    request = await _request_for(committed, web_slot)
    assert request is not None

    _follow(web, await _login_url(committed, EMAIL))

    request = await _request_for(committed, web_slot)
    assert request is not None and not request.awaiting_verification
    assert await _admin_rows(committed, request.id), "the therapist was not told"
    slot = await _slot(committed, web_slot)
    assert slot.hold_expires_at - datetime.now(UTC) > timedelta(hours=1)


async def test_an_unfollowed_booking_lapses_and_frees_the_slot(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    await _book(web, committed, web_slot)
    request = await _request_for(committed, web_slot)
    assert request is not None
    request_id = request.id
    past = datetime.now(UTC) - timedelta(minutes=1)
    await committed.execute(update(Slot).where(Slot.id == web_slot).values(hold_expires_at=past))
    await committed.execute(
        update(BookingRequest).where(BookingRequest.id == request_id).values(expires_at=past)
    )
    await committed.commit()

    # What the two sweeps do, in their order, on this session (the sweeps use
    # the app's own engine, which belongs to the TestClient's event loop).
    await slot_service.expire_hold(committed, web_slot)
    await booking.expire_request(committed, request_id)
    await committed.commit()

    slot = await _slot(committed, web_slot)
    assert slot.status is SlotStatus.available
    ident = (
        await committed.execute(select(Identity).where(Identity.external_id == EMAIL))
    ).scalar_one()
    request = (
        await committed.execute(
            select(BookingRequest).where(BookingRequest.client_id == ident.client_id)
        )
    ).scalar_one()
    assert request.status is RequestStatus.expired
    assert not await _admin_rows(committed, request.id)


async def test_a_proved_address_gets_nothing_filed_by_a_stranger(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    from app.core.services.clients import resolve_client

    owner_id = (await resolve_client(committed, Channel.email, EMAIL, verified=True)).id
    await committed.commit()
    before = await _book(web, committed, web_slot)
    assert before.status_code == 200

    await committed.rollback()
    filed = (
        (
            await committed.execute(
                select(BookingRequest).where(BookingRequest.client_id == owner_id)
            )
        )
        .scalars()
        .all()
    )
    assert not filed, "a request was filed on a client who never followed a link"
    assert (await _slot(committed, web_slot)).status is SlotStatus.available
    # The owner is told by email and can book for themselves; the page is the
    # same one a new address gets.
    assert 'class="uuid"' not in before.text


async def test_post_book_is_limited_per_ip(web: TestClient, committed: AsyncSession) -> None:
    await committed.execute(update(Practice).values(book_per_ip_per_hour=2))
    await committed.commit()
    try:
        web.get("/book")
        codes = [
            web.post(
                "/book",
                data={"csrf_token": _csrf(web), "email": f"x{n}@example.test"},
                follow_redirects=False,
            ).status_code
            for n in range(3)
        ]
    finally:
        await committed.rollback()
        await committed.execute(update(Practice).values(book_per_ip_per_hour=10))
        await committed.commit()
    assert codes[:2] != [429, 429] and 429 not in codes[:2]
    assert codes[2] == 429


async def test_a_new_practice_starts_with_the_gate_on_and_a_fifteen_minute_hold(
    committed: AsyncSession,
) -> None:
    defaults = dict(
        (
            await committed.execute(
                text(
                    "SELECT column_name, column_default FROM information_schema.columns "
                    "WHERE table_name = 'practice' AND column_name IN "
                    "('captcha_on', 'unverified_hold_minutes', 'book_per_ip_per_hour')"
                )
            )
        )
        .tuples()
        .all()
    )
    assert defaults["captcha_on"] == "true"
    assert defaults["unverified_hold_minutes"] == "15"
    assert defaults["book_per_ip_per_hour"] == "10"


async def test_a_line_break_in_the_booking_email_is_refused_and_nothing_is_held(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    response = await _book(web, committed, web_slot, email="a@b.example\nBcc: x@y.example")
    assert response.status_code == 400
    assert (await _slot(committed, web_slot)).status is SlotStatus.available


# --- Review round 1 ----------------------------------------------------------------


@pytest.fixture
def web_nomail(email_disabled: None) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


async def test_without_smtp_a_booking_is_filed_normally_and_the_therapist_is_told(
    web_nomail: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """No link can be sent, so a 15-minute hold nothing can release would just
    lose the booking."""
    response = await _book(web_nomail, committed, web_slot)
    assert response.status_code == 200
    request = await _request_for(committed, web_slot)
    assert request is not None and not request.awaiting_verification
    assert await _admin_rows(committed, request.id), "the therapist was not told"
    slot = await _slot(committed, web_slot)
    assert slot.hold_expires_at - datetime.now(UTC) > timedelta(hours=1)


async def test_an_address_out_of_login_links_is_refused_not_silently_lost(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    from app.core.enums import TokenPurpose
    from app.core.services.clients import issue_token, resolve_client

    client = await resolve_client(committed, Channel.email, EMAIL)
    for _ in range(3):
        await issue_token(
            committed, TokenPurpose.login, client_id=client.id, payload={"email": EMAIL}
        )
    await committed.commit()

    response = await _book(web, committed, web_slot)
    assert response.status_code == 429
    assert await _request_for(committed, web_slot) is None
    assert (await _slot(committed, web_slot)).status is SlotStatus.available


async def test_a_proved_owner_on_a_new_browser_has_the_booking_filed_when_the_link_is_followed(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    from app.core.services.clients import resolve_client

    owner_id = (await resolve_client(committed, Channel.email, EMAIL, verified=True)).id
    await committed.commit()

    await _book(web, committed, web_slot)
    assert await _request_for(committed, web_slot) is None
    url = await _login_url(committed, EMAIL)

    _follow(web, url)

    request = await _request_for(committed, web_slot)
    assert request is not None, "the booking the owner chose was lost"
    assert request.client_id == owner_id
    assert not request.awaiting_verification
    assert await _admin_rows(committed, request.id)
    assert (await _slot(committed, web_slot)).status is SlotStatus.held


# --- Review round 2 ----------------------------------------------------------------


async def _spend_login_links(committed: AsyncSession, client_id: object) -> None:
    from app.core.enums import TokenPurpose
    from app.core.services.clients import issue_token

    for _ in range(3):
        await issue_token(
            committed, TokenPurpose.login, client_id=client_id, payload={"email": EMAIL}
        )
    await committed.commit()


def _message(html: str) -> str:
    import re

    found = re.search(r"<p>(.*?)</p>", html, re.S)
    return found.group(1).strip() if found else html


async def test_a_proved_owner_out_of_links_gets_the_same_refusal_as_a_new_address(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    from app.core.services.clients import resolve_client

    client = await resolve_client(committed, Channel.email, EMAIL)
    client_id = client.id
    await committed.commit()
    await _spend_login_links(committed, client_id)

    new_address = await _book(web, committed, web_slot)
    await committed.execute(
        update(Identity)
        .where(Identity.client_id == client_id)
        .values(verified_at=datetime.now(UTC))
    )
    await committed.commit()
    owner = await _book(web, committed, web_slot)

    assert new_address.status_code == owner.status_code == 429
    assert _message(new_address.text) == _message(owner.text)
    assert await _request_for(committed, web_slot) is None


async def _owner_with_a_carried_booking(
    web: TestClient, web_slot: int, committed: AsyncSession, contact: str = ""
) -> tuple[object, str]:
    from app.core.services.clients import resolve_client

    owner_id = (await resolve_client(committed, Channel.email, EMAIL, verified=True)).id
    await committed.commit()
    await _book(web, committed, web_slot, contact=contact)
    url = await _login_url(committed, EMAIL)
    from urllib.parse import parse_qs, urlparse

    return owner_id, parse_qs(urlparse(url).query)["token"][0]


async def test_the_confirm_page_shows_the_name_and_contact_that_one_click_files(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    """NP3c/5: the owner sees what the button files, not only when and what kind."""
    _owner, token = await _owner_with_a_carried_booking(
        web, web_slot, committed, contact="<b>+374 99 111222</b>"
    )
    page = web.get("/auth/callback", params={"token": token})
    assert page.status_code == 200
    assert "Имя в заявке: N" in page.text  # the test client reads Russian
    assert "Контакт: &lt;b&gt;+374 99 111222&lt;/b&gt;" in page.text  # escaped, not markup
    assert "<b>+374" not in page.text


async def test_the_confirm_page_for_a_carried_booking_names_it_and_offers_both_choices(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    _owner, token = await _owner_with_a_carried_booking(web, web_slot, committed)

    page = web.get("/auth/callback", params={"token": token})
    assert page.status_code == 200
    assert 'name="mode" value="book"' in page.text
    assert 'name="mode" value="signin"' in page.text
    # Named in words and the client's zone, not as an ISO stamp: "пт 4 дек, 23:54 (Москва, GMT+3)".
    assert re.search(
        r"\d+ [а-я]{3}, \d\d:\d\d \([^)]+, GMT[+-]\d",  # noqa: RUF001 - Cyrillic is the assertion
        page.text,
    )

    row = (
        (
            await committed.execute(
                select(OutboxMessage)
                .where(OutboxMessage.address == EMAIL, OutboxMessage.intent_key.like("auth.login%"))
                .order_by(OutboxMessage.id.desc())
            )
        )
        .scalars()
        .first()
    )
    assert row is not None and row.intent_key == "auth.login_link.client"
    assert row.payload.get("when")


async def test_signing_in_without_booking_files_nothing_and_a_replay_files_nothing_more(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:

    _owner, token = await _owner_with_a_carried_booking(web, web_slot, committed)
    web.get("/auth/callback", params={"token": token})
    signed = web.post(
        "/auth/callback",
        data={"csrf_token": _csrf(web), "token": token, "mode": "signin"},
        follow_redirects=False,
    )
    assert signed.status_code == 303
    assert web.cookies.get(CLIENT_COOKIE)
    assert await _request_for(committed, web_slot) is None

    # The spent token cannot be used to book afterwards.
    again = web.post(
        "/auth/callback",
        data={"csrf_token": _csrf(web), "token": token, "mode": "book"},
        follow_redirects=False,
    )
    assert again.status_code == 400
    assert await _request_for(committed, web_slot) is None


async def test_following_a_carried_booking_twice_files_it_once(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    _owner, token = await _owner_with_a_carried_booking(web, web_slot, committed)
    data = {"csrf_token": _csrf(web), "token": token, "mode": "book"}
    first = web.post("/auth/callback", data=data, follow_redirects=False)
    second = web.post("/auth/callback", data=data, follow_redirects=False)
    assert first.status_code == 303
    # The replay is refused: 400 (token spent) or 403 (the first answer started
    # the session, and the CSRF token the form carried is bound to the anonymous
    # one). Either way it files nothing, which is what the count below proves.
    assert second.status_code in (400, 403)
    await committed.rollback()
    count = len(
        (await committed.execute(select(BookingRequest).where(BookingRequest.slot_id == web_slot)))
        .scalars()
        .all()
    )
    assert count == 1


async def test_a_carried_booking_whose_slot_has_gone_signs_in_and_files_nothing(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:

    _owner, token = await _owner_with_a_carried_booking(web, web_slot, committed)
    await committed.execute(
        update(Slot).where(Slot.id == web_slot).values(status=SlotStatus.blocked)
    )
    await committed.commit()

    gone = web.post(
        "/auth/callback",
        data={"csrf_token": _csrf(web), "token": token, "mode": "book"},
        follow_redirects=False,
    )
    assert gone.status_code == 409
    assert web.cookies.get(CLIENT_COOKIE)
    assert await _request_for(committed, web_slot) is None


async def test_erasing_the_owner_removes_a_carried_booking_token(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    from app.core.models import AuthToken
    from app.core.services.clients import erase_client

    owner_id, _token = await _owner_with_a_carried_booking(web, web_slot, committed)
    await erase_client(committed, owner_id)  # type: ignore[arg-type]
    await committed.commit()
    left = (
        (await committed.execute(select(AuthToken).where(AuthToken.client_id == owner_id)))
        .scalars()
        .all()
    )
    assert not left
