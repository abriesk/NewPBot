"""NP3c items 11 and 12: failed-CSRF re-renders, the 429 page text, the on-site address.

Enforces IMPLEMENTATION.md §12.1 (the client pages) and §17 (CSRF, rate limits).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel
from app.core.models import Practice
from app.main import create_app
from tests.e2e.test_unverified_holds import (
    EMAIL,
    _book,
    _spend_login_links,
)
from tests.e2e.test_web_booking import committed, web, web_slot  # noqa: F401


@pytest.fixture
def anon() -> TestClient:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


# --- 11: a failed CSRF check repeats nothing that was posted -----------------------------------


def test_a_failed_csrf_post_to_the_waitlist_does_not_echo_the_posted_values(
    anon: TestClient,
) -> None:
    anon.get("/waitlist?lang=en")
    response = anon.post(
        "/waitlist",
        data={"problem": "typed-problem-xyz", "contact": "typed-contact-xyz", "csrf_token": "bad"},
    )
    assert response.status_code == 403
    assert "open for too long" in response.text
    assert "typed-problem-xyz" not in response.text
    assert "typed-contact-xyz" not in response.text


def test_a_failed_csrf_post_to_sign_in_does_not_echo_the_address(anon: TestClient) -> None:
    anon.get("/auth/email?lang=en")
    response = anon.post("/auth/email", data={"email": "typed-xyz@b.example", "csrf_token": "bad"})
    assert response.status_code == 403
    assert "open for too long" in response.text
    assert "typed-xyz@b.example" not in response.text


# --- 12a: a 429 page says a sentence, not a key ------------------------------------------------


async def test_the_429_page_is_a_sentence_not_a_bare_key(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    from app.core.services.clients import resolve_client

    client = await resolve_client(committed, Channel.email, EMAIL)
    await committed.commit()
    await _spend_login_links(committed, client.id)

    response = await _book(web, committed, web_slot)

    assert response.status_code == 429
    assert "rate_limited" not in response.text
    assert "Слишком много попыток" in response.text  # the test client reads Russian


async def test_the_per_ip_429_page_is_a_sentence_too(
    web: TestClient,
    web_slot: int,
    committed: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels.web import client as web_client

    monkeypatch.setattr(web_client.ratelimit, "check", lambda *a, **k: False)
    response = await _book(web, committed, web_slot)
    assert response.status_code == 429
    assert "rate_limited" not in response.text
    assert "Слишком много попыток" in response.text


# --- 12b: an in-person visitor sees the address, not `{url}` -----------------------------------


@pytest_asyncio.fixture
async def onsite_url(committed: AsyncSession) -> AsyncIterator[str]:
    url = "https://clinic.example/where"
    before = (await committed.execute(select(Practice.clinic_onsite_url).limit(1))).scalar_one()
    await committed.execute(update(Practice).values(clinic_onsite_url=url))
    await committed.commit()
    try:
        yield url
    finally:
        await committed.rollback()
        await committed.execute(update(Practice).values(clinic_onsite_url=before))
        await committed.commit()


@pytest.mark.parametrize("lang", ["en", "ru", "hy"])
def test_the_onsite_info_names_the_address(web: TestClient, onsite_url: str, lang: str) -> None:
    page = web.get(f"/book?lang={lang}")
    assert page.status_code == 200
    assert "{url}" not in page.text
    assert f'href="{onsite_url}"' in page.text
    assert page.text.count(onsite_url) >= 2  # the link target and the link text, once each
