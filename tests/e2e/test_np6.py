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

from app.core.enums import Channel, Modality, RequestStatus
from app.core.models import BookingRequest, Practice, SessionType
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
    # the other pages are the default ones, whole, in the waterfall's colours
    assert _is_wf(web.get("/privacy").text) and _is_wf(web.get("/").text)
    await _set_skin(committed, "default")
    assert not _is_wf(web.get("/book").text)


def _to_time(web: TestClient, html: str) -> str:
    """Answer the questions before the time, with the first answer each, by following links."""
    for _ in range(4):
        if _kind(html) == "time":
            return html
        html = web.get(_first(html)).text
    assert _kind(html) == "time"
    return html


def _hold_fields(html: str, slot_id: int | None = None) -> dict[str, str]:
    """The hidden fields of the time form (first one, or the one for `slot_id`)."""
    for form in html.split('action="/book/hold"')[1:]:
        form = form.split("</form>")[0]
        fields = dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"', form))
        if slot_id is None or fields["slot_id"] == str(slot_id):
            assert list(re.findall(r'name="([a-z_]+)"', form)).count("slot_id") == 1
            return fields
    raise AssertionError("no time form")


def _form_names(html: str, action: str) -> list[str]:
    form = html.split('id="wf-form"')[1].split("</form>")[0]
    assert f'action="{action}"' in html.split('id="wf-form"')[1].split(">")[0]
    return re.findall(r'<(?:input|textarea|select)[^>]*name="([a-z_]+)"', form)


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
    expected = (["type"] if len(types) > 1 else []) + ([] if practice.online_only else ["mode"])
    expected += ["day", "time"]
    seen = []
    for _ in expected:
        seen.append(_kind(page))
        assert page.count('id="q-current"') == 1  # one question on screen
        if seen[-1] != "time":
            page = web.get(_first(page)).text
            assert page.count('class="stone') >= len(seen)  # the answered ones stay, as stones
    assert seen == expected
    # the time is the default flow's own hold: one form per time, no control name twice
    assert _hold_fields(page, web_slot)["slot_id"] == str(web_slot)
    assert re.search(rf"Шаг {len(expected)} из \d", page)  # the step is said in words


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
        # (and the count covers day, time, name, contact, note, and the address of a visitor)
        assert _kind(page) == "day" and 'class="trail"' not in page
        assert "1 из 6" in page
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
    for day in ("2001-01-01", "tomorrow", "<b>"):
        page = web.get(base % type_id + f"&day={day}").text
        assert _kind(page) in ("day", "type", "mode") and "<b>" not in page
    # a slot in the address is not an answer any more: the time is chosen with its own form
    page = web.get(base % type_id).text
    if _kind(page) == "day":
        day_href = _first(page)
        assert _kind(web.get(day_href + "&slot=99999999").text) == "time"
    assert _kind(web.get("/book?session_type_id=999999").text) in ("type", "mode", "day")
    assert _kind(web.get(f"/book?session_type_id={type_id}&modality=teleport").text) in (
        "mode",
        "day",
    )
    # a zone that does not exist falls back to the visitor's
    assert _kind(web.get(base % type_id + "&tz=Mars/Base").text) in ("day", "type", "mode")


@pytest.mark.asyncio
async def test_going_back_through_a_stone_asks_that_question_again_and_drops_the_later_answers(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    await _set_skin(committed, "waterfall")
    page = _to_time(web, web.get("/book").text)
    stones = re.findall(r'<li class="stone[^"]*" data-kind="([a-z]+)">\s*<a href="([^"]+)"', page)
    kinds = [k for k, _ in stones]
    assert kinds[-1] == "day"
    day_stone = dict(stones)["day"].replace("&amp;", "&")
    again = web.get(day_stone).text
    # the day is asked again, the time is gone, the earlier answers are kept
    assert _kind(again) == "day"
    assert re.findall(r'data-kind="([a-z]+)"', again) == kinds[:-1]
    # the fragment the page swaps in is the same path, without the page around it
    fragment = web.get(
        day_stone.replace("/book?", "/book/q?"), headers={"HX-Request": "true", "X-WF": "back"}
    )
    assert fragment.status_code == 200 and "<html" not in fragment.text
    assert 'id="q-current"' in fragment.text and "is-new" not in fragment.text
    forward = web.get(
        day_stone.replace("/book?", "/book/q?").replace("&day=", "&x="),
        headers={"HX-Request": "true", "X-WF": "fwd"},
    )
    assert "is-new" in forward.text


async def _hold(web: TestClient, committed: AsyncSession, web_slot: int) -> str:
    """The pick flow to the time, then the hold: returns the first details page."""
    await _set_skin(committed, "waterfall")
    page = _to_time(web, web.get("/book").text)
    held = web.post("/book/hold", data=_hold_fields(page, web_slot), follow_redirects=False)
    assert held.status_code == 303 and held.headers["location"] == "/book/details"
    return web.get("/book/details").text


def _next(web: TestClient, page: str, **answers: str) -> str:
    """Answer the question on `page` the way the form does without a script: every hidden field
    comes back, the visible one is typed."""
    form = page.split('id="wf-form"')[1].split("</form>")[0]
    fields = dict(re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)"', form))
    fields.update({k: v for k, v in answers.items() if k != "act"})
    fields["act"] = answers.get("act", "next")
    fields = {k: v.replace("&#34;", '"').replace("&amp;", "&") for k, v in fields.items()}
    return web.post("/book/next", data=fields).text


@pytest.mark.asyncio
async def test_the_questions_after_the_time_are_one_page_each_and_carry_the_answers(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    page = await _hold(web, committed, web_slot)
    assert _kind(page) == "name"
    assert page.count('class="stone') >= 2
    assert _form_names(page, "/book/next") == [
        "csrf_token",
        "step",
        "email",
        "contact",
        "problem",
        "name",
    ]

    page = _next(web, page, name="Zoya\nIvanova")  # a text input strips line breaks
    assert _kind(page) == "email" and "Zoya Ivanova" in page
    # the address is required, and a refused one is an alert on the same question
    bad = _next(web, page, email="not-an-address")
    assert _kind(bad) == "email" and 'role="alert"' in bad
    page = _next(web, page, email="Waterfall@Example.test")
    assert _kind(page) == "contact" and "waterfall@example.test" in page
    page = _next(web, page, act="skip", contact="never kept")  # skipped: no stone, nothing kept
    assert _kind(page) == "problem" and "never kept" not in page
    # ... but a skipped question leaves a quiet stone that opens it again
    assert 'class="stone skipped" data-kind="contact"' in page
    page = _next(web, page, problem="Line one\nLine two")
    assert _kind(page) == "send"
    # the last page posts the default flow's own form, every control name once
    names = _form_names(page, "/book")
    assert names == ["csrf_token", "step", "name", "email", "contact", "problem"], names
    assert "Line one&#10;Line two" in page or "Line one\nLine two" in page

    # a typed stone asks its question again, with the other answers kept
    back = _next(web, page, act="goto:name")
    assert _kind(back) == "name" and 'value="Zoya Ivanova"' in back
    assert 'name="email" value="waterfall@example.test"' in back
    # a stale page is told so, and what was typed is not repeated
    stale = web.post("/book/next", data={"csrf_token": "wrong", "step": "name", "name": "SECRET"})
    assert stale.status_code == 403 and "SECRET" not in stale.text and 'role="alert"' in stale.text


@pytest.mark.asyncio
async def test_a_signed_in_client_is_not_asked_for_an_address(
    web: TestClient, committed: AsyncSession, web_slot: int
) -> None:
    from sqlalchemy import delete

    from app.channels.web.security import CLIENT_COOKIE, client_session_value
    from app.core.models import Client, FlowState

    await _set_skin(committed, "waterfall")
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    client = Client(practice_id=practice.id, language="ru", display_name="Мария")
    committed.add(client)
    await committed.commit()
    try:
        web.cookies.set(CLIENT_COOKIE, client_session_value(client.id))
        page = await _hold(web, committed, web_slot)
        assert _kind(page) == "name" and 'value="Мария"' in page  # prefilled when signed in
        page = _next(web, page, name="Мария")
        assert _kind(page) == "contact"  # the address question is skipped
    finally:
        await committed.execute(delete(FlowState).where(FlowState.client_id == client.id))
        await committed.execute(delete(Client).where(Client.id == client.id))
        await committed.commit()


@pytest.mark.parametrize("flow", ["default", "waterfall"])
@pytest.mark.asyncio
async def test_both_flows_file_the_same_request(
    web: TestClient, committed: AsyncSession, web_slot: int, flow: str
) -> None:
    """The same answers give the same request row, whichever flow was used."""
    from app.core.models import Identity

    email = "web-e2e@example.test"
    zone = (await committed.execute(select(Practice.timezone).limit(1))).scalar_one()
    if flow == "waterfall":
        page = await _hold(web, committed, web_slot)
        page = _next(web, page, name="Anna")
        page = _next(web, page, email=email)
        page = _next(web, page, contact="email is fine")
        page = _next(web, page, problem="work stress")
        names = _form_names(page, "/book")
        form = page.split('id="wf-form"')[1].split("</form>")[0]
        fields = dict(re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)"', form))
        assert names.count("problem") == 1 and names.count("name") == 1
        done = web.post("/book", data=fields)
    else:
        await _set_skin(committed, "default")
        page = web.get("/book").text
        held = web.post(
            "/book/hold",
            data={
                "csrf_token": _csrf(web),
                "slot_id": web_slot,
                "session_type_id": (
                    await committed.execute(select(SessionType.id).order_by(SessionType.id))
                )
                .scalars()
                .first(),
                "modality": "online",
                "tz": zone,
            },
            follow_redirects=False,
        )
        assert held.status_code == 303
        done = web.post(
            "/book",
            data={
                "csrf_token": _csrf(web),
                "problem": "work stress",
                "name": "Anna",
                "contact": "email is fine",
                "email": email,
            },
        )
    assert done.status_code == 200
    await committed.rollback()
    identity = (
        await committed.execute(select(Identity).where(Identity.external_id == email))
    ).scalar_one()
    request = (
        await committed.execute(
            select(BookingRequest).where(BookingRequest.client_id == identity.client_id)
        )
    ).scalar_one()
    assert (
        request.status,
        request.source_channel,
        request.slot_id,
        request.problem_text,
        request.contact_note,
        request.modality,
        request.client_timezone,
    ) == (
        RequestStatus.pending,
        Channel.web,
        web_slot,
        "work stress",
        "email is fine",
        Modality.online,
        zone,
    )


@pytest.mark.parametrize("skin", ["default", "waterfall"])
@pytest.mark.asyncio
async def test_the_other_client_pages_answer_in_both_flows(
    web: TestClient, committed: AsyncSession, skin: str
) -> None:
    """Landing, waitlist, sign-in, a missing page: same status and same words, either flow."""
    await _set_skin(committed, skin)
    for path, status, word in (
        ("/", 200, "<h1"),
        ("/waitlist", 200, 'action="/waitlist"'),
        ("/auth/email", 200, "<form"),
        ("/privacy", 200, "<h1"),
        ("/no-such-page", 404, "<h1"),
        ("/book/next", 200, "<form"),
    ):
        page = web.get(path)
        assert page.status_code == status and word in page.text, (skin, path)
        assert _is_wf(page.text) == (skin == "waterfall"), (skin, path)


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
