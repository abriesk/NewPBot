"""NP4b: the client site's warm, personal pages through the real ASGI app (IMPLEMENTATION.md §12.1,
§12.2, §15, §17).

Her content is hers: the photo, languages, bio, price and cancellation rule come from the admin and
are never invented -- with none of it the pages still read well and say nothing false.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.core.models import ContentTopic, PracticePhoto
from app.main import create_app

# Fixtures and helpers shared with the admin and booking tests.
from tests.e2e.test_admin_web import (  # noqa: F401
    _csrf,
    _sign_in,
    committed,
    restore_practice,
)


@pytest.fixture
def web() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


def _jpeg(size: tuple[int, int] = (900, 700)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (180, 140, 100)).save(out, "JPEG")
    return out.getvalue()


def _upload(web: TestClient, raw: bytes, name: str = "me.jpg", ctype: str = "image/jpeg"):
    web.get("/admin/settings")
    return web.post(
        "/admin/settings/photo",
        data={"csrf_token": _csrf(web)},
        files={"photo": (name, raw, ctype)},
        follow_redirects=False,
    )


@pytest_asyncio.fixture
async def no_photo(committed) -> AsyncIterator[None]:  # noqa: F811
    async def clear() -> None:
        await committed.rollback()
        for row in (await committed.execute(select(PracticePhoto))).scalars():
            await committed.delete(row)
        await committed.commit()

    await clear()
    yield
    await clear()


# --- item 1: her photo ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_admin_uploads_a_photo_and_the_site_serves_a_small_cached_copy(
    web: TestClient, no_photo: None
) -> None:
    _sign_in(web)
    response = _upload(web, _jpeg((2400, 1800)))
    assert response.status_code == 303 and "Photo+saved" in response.headers["location"]

    page = web.get("/admin/settings")
    assert "/media/photo?v=" in page.text
    version = page.text.split("/media/photo?v=")[1].split('"')[0]

    # Public: no sign-in needed, as the client site shows it.
    with TestClient(create_app(), base_url="https://testserver") as visitor:
        served = visitor.get(f"/media/photo?v={version}")
        assert served.status_code == 200
        assert served.headers["content-type"] == "image/jpeg"
        assert "immutable" in served.headers["cache-control"]
        assert len(served.content) < 200_000
        assert max(Image.open(io.BytesIO(served.content)).size) == 640
        # Without the current version the copy is kept only briefly.
        assert "immutable" not in visitor.get("/media/photo").headers["cache-control"]
        # A browser that already has it is told so.
        assert (
            visitor.get(
                "/media/photo", headers={"If-None-Match": served.headers["etag"]}
            ).status_code
            == 304
        )


@pytest.mark.asyncio
async def test_no_photo_is_a_404_not_a_stock_face(web: TestClient, no_photo: None) -> None:
    assert web.get("/media/photo").status_code == 404


@pytest.mark.asyncio
async def test_a_photo_that_is_not_a_picture_is_refused_whatever_its_name(
    web: TestClient, no_photo: None
) -> None:
    _sign_in(web)
    for raw, name in ((b"<svg/>", "me.jpg"), (b"GIF89a....", "me.png"), (b"%PDF-1.4", "me.webp")):
        response = _upload(web, raw, name, "image/jpeg")
        assert "rejected" in response.headers["location"]
    assert web.get("/media/photo").status_code == 404


@pytest.mark.asyncio
async def test_a_photo_over_the_cap_is_refused_before_it_is_read(
    web: TestClient, no_photo: None
) -> None:
    from app.core.services.photo import MAX_UPLOAD_BYTES

    _sign_in(web)
    web.get("/admin/settings")
    response = web.post(
        "/admin/settings/photo",
        data={"csrf_token": _csrf(web)},
        files={
            "photo": (
                "big.jpg",
                b"\xff\xd8\xff" + b"0" * (MAX_UPLOAD_BYTES + 200_000),
                "image/jpeg",
            )
        },
        follow_redirects=False,
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_the_upload_needs_the_admin_and_a_csrf_token(web: TestClient, no_photo: None) -> None:
    anonymous = web.post(
        "/admin/settings/photo",
        data={"csrf_token": "x"},
        files={"photo": ("me.jpg", _jpeg(), "image/jpeg")},
        follow_redirects=False,
    )
    assert anonymous.headers["location"].startswith("/admin/login")
    _sign_in(web)
    web.get("/admin/settings")
    forged = web.post(
        "/admin/settings/photo",
        data={"csrf_token": "forged"},
        files={"photo": ("me.jpg", _jpeg(), "image/jpeg")},
        follow_redirects=False,
    )
    assert forged.status_code == 403


@pytest.mark.asyncio
async def test_the_photo_can_be_removed(web: TestClient, no_photo: None) -> None:
    _sign_in(web)
    _upload(web, _jpeg())
    assert web.get("/media/photo").status_code == 200
    web.get("/admin/settings")
    response = web.post(
        "/admin/settings/photo/delete", data={"csrf_token": _csrf(web)}, follow_redirects=False
    )
    assert response.status_code == 303
    assert web.get("/media/photo").status_code == 404


def test_the_languages_field_is_on_the_settings_form_and_saves(web: TestClient) -> None:
    _sign_in(web)
    page = web.get("/admin/settings")
    assert 'name="languages"' in page.text
    web.post(
        "/admin/settings",
        data={"csrf_token": _csrf(web), "languages": "Русский, English"},
        follow_redirects=False,
    )
    assert 'value="Русский, English"' in web.get("/admin/settings").text


@pytest.mark.asyncio
async def test_bio_cancellation_and_privacy_are_content_topics_she_can_write(committed) -> None:  # noqa: F811
    codes = set((await committed.execute(select(ContentTopic.code))).scalars())
    assert {"about_me", "cancellation", "privacy"} <= codes
    hidden = (
        await committed.execute(
            select(ContentTopic.show_in_menu).where(ContentTopic.code == "about_me")
        )
    ).scalar_one()
    assert hidden is False  # not a menu entry: nothing to link to while it is empty
