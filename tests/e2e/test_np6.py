"""NP6 stage A2: the waterfall booking flow and its setting (IMPLEMENTATION.md §12.1, §12.2).

Clients get the practice's `client_skin`; `?skin=` is an admin-only preview that never changes what
clients get. The waterfall asks one question at a time; every answer is a link carrying the answers
so far, so each question is a page of its own without a script and a fragment with one.
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

from app.core.models import Practice, SessionType
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


def _is_wf(html: str) -> bool:
    return "/static/waterfall.css" in html and 'class="skin-waterfall"' in html


def _kind(html: str) -> str:
    match = re.search(r'id="q-current" class="q q-([a-z]+)"', html)
    assert match, "no current question"
    return match.group(1)


def _first(html: str) -> str:
    """The href of the first answer of the question now on screen."""
    section = html.split('id="q-current"')[1]
    found = re.search(r'<a class="ans[^"]*"[^>]*href="(/book\?[^"]*)"', section)
    assert found
    return found.group(1).replace("&amp;", "&")


@pytest.mark.asyncio
async def test_the_default_skin_is_what_a_new_practice_has(
    web: TestClient, committed: AsyncSession
) -> None:
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.client_skin == "default" and CLIENT_SKINS == ("default", "waterfall")
    assert not _is_wf(web.get("/book").text)
    assert web.get("/book/q").status_code == 404  # the fragment is the waterfall's alone


@pytest.mark.asyncio
async def test_clients_get_the_flow_the_setting_names(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    await _set_skin(committed, "waterfall")
    page = web.get("/book")
    assert page.status_code == 200 and _is_wf(page.text)
    assert web.get("/book/q").status_code == 200
    # the pages the waterfall has not been drawn for are the default ones, whole
    assert not _is_wf(web.get("/privacy").text) and not _is_wf(web.get("/").text)
    await _set_skin(committed, "default")
    assert not _is_wf(web.get("/book").text)


@pytest.mark.asyncio
async def test_one_question_per_page_and_each_answer_moves_on(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    """No script: every answer is a link; a page is the stones so far and one question."""
    await _set_skin(committed, "waterfall")
    types = (
        (await committed.execute(select(SessionType).where(SessionType.is_active))).scalars().all()
    )
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    page = web.get("/book").text
    expected = []
    if len(types) > 1:
        expected.append("type")
    if not practice.online_only:
        expected.append("mode")
    expected += ["day", "time", "go"]
    seen = []
    for _ in range(len(expected)):
        kind = _kind(page)
        seen.append(kind)
        assert page.count('id="q-current"') == 1  # one question on screen
        if kind == "go":
            break
        href = _first(page)
        if kind == "mode":
            assert "modality=online" in href
        page = web.get(href).text
        assert page.count('class="stone') >= len(seen)  # the answered ones stay, as stones
    assert _kind(page) == "go" and f'name="slot_id" value="{web_slot}"' in page


@pytest.mark.asyncio
async def test_a_question_with_one_answer_is_skipped(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    await _set_skin(committed, "waterfall")
    await committed.execute(update(Practice).values(online_only=True))
    await committed.commit()
    others = (
        (await committed.execute(select(SessionType).where(SessionType.is_active))).scalars().all()
    )
    keep = others[0].id
    await committed.execute(
        update(SessionType).where(SessionType.id != keep).values(is_active=False)
    )
    await committed.commit()
    try:
        page = web.get("/book").text
        # one session type and online only: the first question is the day, with no stones above it
        assert _kind(page) == "day" and 'class="trail"' not in page
        assert "1 из 2" in page  # the count leaves the skipped questions out
    finally:
        await committed.execute(
            update(SessionType)
            .where(SessionType.id.in_([t.id for t in others]))
            .values(is_active=True)
        )
        await committed.commit()


@pytest.mark.asyncio
async def test_every_answer_is_validated_and_a_bad_one_is_not_given(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    await _set_skin(committed, "waterfall")
    base = "/book?session_type_id=%s&modality=online"
    type_id = (
        await committed.execute(select(SessionType.id).where(SessionType.is_active).limit(1))
    ).scalar_one()
    # a day with no times asks for the day again; so does a mangled one
    for day in ("2001-01-01", "tomorrow", "<b>"):
        page = web.get(base % type_id + f"&day={day}").text
        assert _kind(page) in ("day", "type", "mode") and "<b>" not in page
    # a slot that is not on the chosen day is not an answer: the time is asked again
    page = web.get(base % type_id).text
    day_href = _first(page) if _kind(page) == "day" else None
    if day_href:
        good = web.get(day_href).text
        assert _kind(good) == "time"
        bad = web.get(day_href + "&slot=99999999").text
        assert _kind(bad) == "time"
        junk = web.get(day_href + "&slot=%00x").text
        assert _kind(junk) == "time"
    # a session type that does not exist, a modality that does not exist
    assert _kind(web.get("/book?session_type_id=999999").text) in ("type", "mode", "day")
    assert _kind(web.get(f"/book?session_type_id={type_id}&modality=teleport").text) in (
        "mode",
        "day",
    )


@pytest.mark.asyncio
async def test_going_back_through_a_stone_asks_that_question_again_and_drops_the_later_answers(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    await _set_skin(committed, "waterfall")
    page = web.get("/book").text
    while _kind(page) != "go":
        page = web.get(_first(page)).text
    stones = re.findall(r'<li class="stone[^"]*" data-kind="([a-z]+)">\s*<a href="([^"]+)"', page)
    kinds = [k for k, _ in stones]
    assert kinds[-2:] == ["day", "time"]
    day_stone = dict(stones)["day"].replace("&amp;", "&")
    again = web.get(day_stone).text
    # the day is asked again, the time is gone, the earlier answers are kept
    assert _kind(again) == "day"
    assert re.findall(r'data-kind="([a-z]+)"', again) == kinds[:-2]
    # the fragment the page swaps in is the same path, without the page around it
    found = re.search(r'hx-get="(/book/q[^"]*)"', page)
    assert found
    fragment_url = found.group(1).replace("&amp;", "&")
    fragment = web.get(fragment_url, headers={"HX-Request": "true", "X-WF": "back"})
    assert (
        fragment.status_code == 200
        and "<html" not in fragment.text
        and 'id="q-current"' in fragment.text
    )
    assert "is-new" not in fragment.text  # going back is not a new answer
    forward = web.get(
        day_stone.replace("/book?", "/book/q?"), headers={"HX-Request": "true", "X-WF": "fwd"}
    )
    assert "is-new" in forward.text


@pytest.mark.asyncio
async def test_the_last_step_posts_the_same_hold_as_the_default_flow(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    await _set_skin(committed, "waterfall")
    page = web.get("/book").text
    while _kind(page) != "go":
        page = web.get(_first(page)).text
    form = page.split('action="/book/hold"')[1].split("</form>")[0]
    names = re.findall(r'<input type="hidden" name="([a-z_]+)"', form)
    assert names == ["csrf_token", "slot_id", "session_type_id", "modality", "tz"]  # no name twice
    fields = dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"', form))
    sent = web.post("/book/hold", data=fields, follow_redirects=False)
    assert sent.status_code == 303 and sent.headers["location"] == "/book/details"


@pytest.mark.asyncio
async def test_the_preview_needs_an_admin_session_and_changes_nothing_for_clients(
    web: TestClient, committed: AsyncSession
) -> None:
    # anybody: the query and the cookie are ignored
    assert not _is_wf(web.get("/book?skin=waterfall").text)
    web.cookies.set("pb_skin_preview", "waterfall")
    assert not _is_wf(web.get("/book").text)
    web.cookies.clear()

    _sign_in(web)
    page = web.get("/book?skin=waterfall")
    assert _is_wf(page.text) and "Preview for you only" in page.text
    assert _is_wf(web.get("/book").text)  # sticks for this admin
    assert web.get("/book/q").status_code == 200
    web.cookies.clear()
    assert not _is_wf(web.get("/book").text)  # not for the next visitor
    assert web.get("/book/q").status_code == 404
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.client_skin == "default"

    _sign_in(web)
    assert _is_wf(web.get("/book?skin=waterfall").text)
    ended = web.get("/book?skin=live")
    assert not _is_wf(ended.text) and "Preview for you only" not in ended.text
    assert not _is_wf(web.get("/book?skin=lava").text)


@pytest.mark.asyncio
async def test_an_admin_can_preview_the_default_while_clients_get_the_waterfall(
    web: TestClient, committed: AsyncSession
) -> None:
    await _set_skin(committed, "waterfall")
    _sign_in(web)
    assert not _is_wf(web.get("/book?skin=default").text)
    assert _is_wf(web.get("/book?skin=live").text)


@pytest.mark.asyncio
async def test_the_setting_is_saved_from_the_settings_page_and_refuses_unknown_names(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    page = web.get("/admin/settings").text
    assert 'name="client_skin"' in page and 'value="waterfall"' in page
    saved = web.post(
        "/admin/settings",
        data={"csrf_token": _csrf(web), "client_skin": "waterfall"},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and "rejected" not in saved.headers["location"]
    await committed.rollback()
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.client_skin == "waterfall"
    bad = web.post(
        "/admin/settings",
        data={"csrf_token": _csrf(web), "client_skin": "lava"},
        follow_redirects=False,
    )
    assert "rejected" in bad.headers["location"]
    with pytest.raises(ValueError, match="client_skin"):
        await update_settings(committed, client_skin="lava")
    await committed.rollback()


def test_the_stylesheet_is_self_contained_and_every_effect_stills_under_reduced_motion() -> None:
    css = (STATIC / "waterfall.css").read_text()
    assert "http://" not in css and "https://" not in css and "@import" not in css
    assert "@font-face" not in css and "url(" not in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    reduce = css.split("@media (prefers-reduced-motion: reduce)")[1]
    assert "animation: none !important" in reduce and "transition: none !important" in reduce
    # every animation and transition is declared inside the no-preference block
    before_motion = css.split("@media (prefers-reduced-motion: no-preference)")[0]
    assert "animation:" not in before_motion and "transition:" not in before_motion
    # no pressure mechanics: nothing counts down or scores
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert not re.search(r"countdown|timer|score|streak|level", rules, re.I)
    script = (STATIC / "waterfall.js").read_text()
    assert "defaultSwapDelay = calm() ? 0" in script and "behavior: calm()" in script
    assert "setTimeout" not in script and "setInterval" not in script
