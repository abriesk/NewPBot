"""NP3d items 5 and 7a: the on-site link without {url} in the text, the /book stale re-render.

Enforces IMPLEMENTATION.md §12.1 (the client pages) and §17 (CSRF).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Practice, SessionType, Translation
from app.core.services.translations import invalidate_cache
from tests.e2e.test_web_booking import _hold_a_slot, committed, web, web_slot  # noqa: F401


@pytest_asyncio.fixture
async def onsite_text_without_url(committed: AsyncSession) -> AsyncIterator[str]:
    url = "https://clinic.example/where"
    before_url = (await committed.execute(select(Practice.clinic_onsite_url).limit(1))).scalar_one()
    old = (
        await committed.execute(
            select(Translation.practice_id, Translation.value).where(
                Translation.lang == "en", Translation.key == "booking.onsite_info"
            )
        )
    ).one()
    await committed.execute(update(Practice).values(clinic_onsite_url=url))
    await committed.execute(
        update(Translation)
        .where(Translation.lang == "en", Translation.key == "booking.onsite_info")
        .values(value="I see clients at the clinic.")
    )
    await committed.commit()
    invalidate_cache()
    try:
        yield url
    finally:
        await committed.rollback()
        await committed.execute(update(Practice).values(clinic_onsite_url=before_url))
        await committed.execute(
            update(Translation)
            .where(
                Translation.practice_id == old[0],
                Translation.lang == "en",
                Translation.key == "booking.onsite_info",
            )
            .values(value=old[1])
        )
        await committed.commit()
        invalidate_cache()


def test_an_edited_onsite_text_without_the_placeholder_still_shows_the_link(
    web: TestClient,
    onsite_text_without_url: str,
) -> None:
    page = web.get("/book?lang=en")
    assert page.status_code == 200
    assert "I see clients at the clinic." in page.text
    assert f'href="{onsite_text_without_url}"' in page.text


async def test_a_failed_csrf_post_to_book_does_not_echo_the_typed_values(
    web: TestClient,
    web_slot: int,
    committed: AsyncSession,
) -> None:
    type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()
    await _hold_a_slot(web, web_slot, type_id)
    response = web.post(
        "/book",
        data={
            "csrf_token": "bad",
            "problem": "typed-problem-xyz",
            "name": "typed-name-xyz",
            "contact": "typed-contact-xyz",
            "email": "typed-xyz@b.example",
        },
    )
    assert response.status_code == 403
    for typed in (
        "typed-problem-xyz",
        "typed-name-xyz",
        "typed-contact-xyz",
        "typed-xyz@b.example",
    ):
        assert typed not in response.text
