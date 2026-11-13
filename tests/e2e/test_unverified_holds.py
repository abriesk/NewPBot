"""A web booking from an unproved address (IMPLEMENTATION.md §12.1, §17; NP2, REVIEW F3).

Short hold until verified: the slot is held for `unverified_hold_minutes`, the
therapist hears only when the login link is followed, an unfollowed booking
lapses and frees the slot, and a stranger typing a proved address files nothing
on that person's record.
"""

from __future__ import annotations

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
    EMAIL,
    _csrf,
    _hold_a_slot,
    committed,
    web,
    web_slot,
)


async def _book(web: TestClient, committed: AsyncSession, slot_id: int, email: str = EMAIL):
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
            "contact": "",
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
                    OutboxMessage.intent_key == "auth.login_link.client",
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
    assert _follow_login(web, token).status_code == 303


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
    await committed.execute(
        update(Slot).where(Slot.id == web_slot).values(hold_expires_at=past)
    )
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


async def test_post_book_is_limited_per_ip(
    web: TestClient, committed: AsyncSession
) -> None:
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
