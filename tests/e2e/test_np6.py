"""NP6 stage A: the second client skin ("river") and its setting (IMPLEMENTATION.md §12.1, §12.2).

Clients get the practice's `client_skin`; `?skin=` is an admin-only preview that never changes what
clients get. Routes, forms and texts are the same in both skins: only templates and CSS differ.
"""

# ruff: noqa: F811
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Practice
from app.core.services.settings import CLIENT_SKINS, update_settings
from app.main import create_app
from tests.e2e.test_admin_web import (  # noqa: F401
    _csrf,
    _sign_in,
    committed,
    restore_practice,
)
from tests.e2e.test_web_booking import web_slot  # noqa: F401

STATIC = Path("app/channels/web/static")


@pytest.fixture
def web() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


async def _set_skin(committed: AsyncSession, skin: str) -> None:
    await committed.execute(update(Practice).values(client_skin=skin))
    await committed.commit()


def _is_river(html: str) -> bool:
    return "/static/river.css" in html and 'class="skin-river"' in html


@pytest.mark.asyncio
async def test_the_default_skin_is_what_a_new_practice_has(
    web: TestClient, committed: AsyncSession
) -> None:
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.client_skin == "default" and CLIENT_SKINS == ("default", "river")
    for path in ("/", "/book"):
        assert not _is_river(web.get(path).text)


@pytest.mark.asyncio
async def test_clients_get_the_skin_the_setting_names(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    await _set_skin(committed, "river")
    for path in ("/", "/book", "/book/slots"):
        page = web.get(path)
        assert page.status_code == 200
        if path != "/book/slots":
            assert _is_river(page.text), path
    # the htmx partial is the river's too: a pool for the day, stones for the times, same forms
    partial = web.get("/book/slots").text
    assert 'class="pool"' in partial and 'class="times stones"' in partial
    assert f'name="slot_id" value="{web_slot}"' in partial
    # a page the river has not been drawn for yet is the default one, whole
    assert not _is_river(web.get("/privacy").text)

    await _set_skin(committed, "default")
    assert not _is_river(web.get("/").text)
    assert 'class="pool"' not in web.get("/book/slots").text


@pytest.mark.asyncio
async def test_the_river_booking_page_has_the_same_controls_as_the_default(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    def controls(html: str) -> list[str]:
        return sorted(set(re.findall(r'name="([a-z_]+)"', html)))

    plain = web.get("/book").text
    await _set_skin(committed, "river")
    river = web.get("/book").text
    assert controls(river) == controls(plain)
    for needle in ('id="booking"', 'id="slots"', 'id="tz-field"', 'id="picker-card"', "byName"):
        assert needle in plain and needle in river
    # never one control name twice inside one form, apart from a radio group
    form = river.split('id="booking"')[1].split("</form>")[0]
    names = re.findall(r'<(?:input|select)[^>]*name="([a-z_]+)"[^>]*>', form)
    assert names.count("tz") == 1 and names.count("csrf_token") == 0


@pytest.mark.asyncio
async def test_the_preview_needs_an_admin_session_and_changes_nothing_for_clients(
    web: TestClient, committed: AsyncSession
) -> None:
    # anybody: the query and the cookie are ignored
    assert not _is_river(web.get("/?skin=river").text)
    web.cookies.set("pb_skin_preview", "river")
    assert not _is_river(web.get("/").text)
    web.cookies.clear()

    _sign_in(web)
    page = web.get("/?skin=river")
    assert _is_river(page.text) and "Preview for you only" in page.text
    # the preview sticks for this admin (the pages and swaps that follow) ...
    assert _is_river(web.get("/book").text)
    # ... and is not what the next visitor gets
    web.cookies.clear()
    assert not _is_river(web.get("/").text)
    assert not _is_river(web.get("/book").text)
    _sign_in(web)
    assert _is_river(web.get("/?skin=river").text)
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.client_skin == "default"

    # `live` ends it
    ended = web.get("/?skin=live")
    assert not _is_river(ended.text) and "Preview for you only" not in ended.text
    assert not _is_river(web.get("/book").text)
    # an unknown name does nothing
    assert not _is_river(web.get("/?skin=lava").text)


@pytest.mark.asyncio
async def test_an_admin_can_preview_the_default_while_clients_get_the_river(
    web: TestClient, committed: AsyncSession
) -> None:
    await _set_skin(committed, "river")
    _sign_in(web)
    assert not _is_river(web.get("/?skin=default").text)
    assert _is_river(web.get("/?skin=live").text)


@pytest.mark.asyncio
async def test_the_setting_is_saved_from_the_settings_page_and_refuses_unknown_names(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    page = web.get("/admin/settings").text
    assert 'name="client_skin"' in page and 'value="river"' in page
    # the form posts every field it has; send the minimum the handler reads
    saved = web.post(
        "/admin/settings",
        data={"csrf_token": _csrf(web), "client_skin": "river"},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and "rejected" not in saved.headers["location"]
    await committed.rollback()
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.client_skin == "river"

    bad = web.post(
        "/admin/settings",
        data={"csrf_token": _csrf(web), "client_skin": "lava"},
        follow_redirects=False,
    )
    assert "rejected" in bad.headers["location"]
    with pytest.raises(ValueError, match="client_skin"):
        await update_settings(committed, client_skin="lava")
    await committed.rollback()


def test_the_river_stylesheet_is_self_contained_and_stills_under_reduced_motion() -> None:
    css = (STATIC / "river.css").read_text()
    assert "http://" not in css.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in css and "@import" not in css and "@font-face" not in css
    # every animation sits in the no-preference block; the reduce block switches the rest off
    assert "@media (prefers-reduced-motion: reduce)" in css
    reduce = css.split("@media (prefers-reduced-motion: reduce)")[1]
    assert "animation: none !important" in reduce
    before_motion = css.split("@media (prefers-reduced-motion: no-preference)")[0]
    assert "animation:" not in before_motion
