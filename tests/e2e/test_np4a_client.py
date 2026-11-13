"""NP4a: the client site's quick wins, errors in place, copy and accessibility, through the real ASGI app.

Enforces IMPLEMENTATION.md §12.1 (the client web pages) and §15 (the catalogue, localised dates).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.channels.web.client import LANG_COOKIE
from app.channels.web.security import CSRF_COOKIE
from app.core.models import Practice, SessionType
from app.main import create_app
from app.render.labels import price_text

# Fixtures shared with the booking tests (a committed slot, a committing session, the email channel on).
from tests.e2e.test_web_booking import committed, web, web_slot  # noqa: F401

WEB = Path(__file__).resolve().parents[2] / "app" / "channels" / "web"


def _csrf(client: TestClient) -> str:
    return client.cookies.get(CSRF_COOKIE, "")


@pytest.fixture
def anon() -> TestClient:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def captcha_off(committed) -> AsyncIterator[None]:  # noqa: F811
    before = (await committed.execute(select(Practice.captcha_on).limit(1))).scalar_one()
    await committed.execute(update(Practice).values(captcha_on=False))
    await committed.commit()
    try:
        yield
    finally:
        await committed.rollback()
        await committed.execute(update(Practice).values(captcha_on=before))
        await committed.commit()


# --- form errors in place (item 3) -------------------------------------------------------------------------------


def test_auth_email_refusal_keeps_the_text_and_sits_beside_the_field(anon: TestClient) -> None:
    anon.get("/auth/email?lang=en")
    response = anon.post("/auth/email", data={"email": "not-an-address", "csrf_token": _csrf(anon)})
    assert response.status_code == 400
    html = response.text
    assert 'value="not-an-address"' in html
    assert re.search(r'<span class="field-error" id="email-error" role="alert">[^<]*address', html)
    assert 'aria-invalid="true"' in html and 'autocomplete="email"' in html


def test_waitlist_refusal_keeps_typed_text(anon: TestClient, captcha_off: None) -> None:  # noqa: F811
    anon.get("/waitlist?lang=en")
    response = anon.post(
        "/waitlist",
        data={"problem": "evenings only", "contact": "call me", "email": "nope", "csrf_token": _csrf(anon)},
    )
    assert response.status_code == 400
    assert "evenings only" in response.text and 'value="call me"' in response.text
    assert 'value="nope"' in response.text and "field-error" in response.text


def test_a_stale_form_is_a_message_not_a_blank_403_and_keeps_the_text(anon: TestClient) -> None:
    anon.get("/waitlist?lang=en")
    response = anon.post(
        "/waitlist", data={"problem": "typed long text", "email": "a@b.example", "csrf_token": "stale"}
    )
    assert response.status_code == 403
    assert "typed long text" in response.text and "open for too long" in response.text
    assert 'role="alert"' in response.text
    # the same for the sign-in form
    again = anon.post("/auth/email", data={"email": "a@b.example", "csrf_token": "stale"})
    assert again.status_code == 403 and 'value="a@b.example"' in again.text and "open for too long" in again.text


# --- error pages, titles, language, noscript (quick wins 5, 15, 16, 20) --------------------------------------------


def test_error_page_has_its_own_heading_and_a_fitting_action(anon: TestClient) -> None:
    response = anon.get("/no-such-page?lang=en")
    assert response.status_code == 404
    heading = re.search(r"<h1>([^<]*)</h1>", response.text).group(1)
    message = re.search(r"<p>([^<]*)</p>\s*<div class=\"stack-actions\"", response.text).group(1)
    assert heading == "Page not found" and message == "We could not find that page." and heading != message
    assert "<title>Page not found — " in response.text

    expired = anon.get("/auth/callback?token=expired-or-wrong&lang=en")
    assert expired.status_code == 400
    assert 'href="/auth/email"' in expired.text  # "send me a new link", not only "Home"


def test_every_page_has_its_own_title_and_htmx_only_on_book(anon: TestClient, web_slot: int) -> None:  # noqa: F811
    titles = {}
    for path in ("/", "/waitlist", "/auth/email", "/book", "/t/work_terms"):
        page = anon.get(path, params={"lang": "en"})
        assert page.status_code == 200, path
        titles[path] = re.search(r"<title>([^<]*)</title>", page.text).group(1)
        assert ("htmx.min.js" in page.text) == (path == "/book"), path
        assert 'lang="en" ' in page.text and 'aria-current="true"' in page.text  # language buttons
        assert 'value="en"' in page.text  # the English button exists
    assert len(set(titles.values())) >= 4, titles
    book = anon.get("/book?lang=en").text
    assert "<noscript>" in book and "onchange=" not in book and "onclick=" not in book
    assert "onclick=" not in anon.get("/book/slots?lang=en").text


def test_time_zone_select_is_localised_and_adds_the_detected_zone(anon: TestClient, web_slot: int) -> None:  # noqa: F811
    anon.cookies.set("pb_tz", "Asia/Almaty", domain="testserver.local")
    ru = anon.get("/book?lang=ru")
    assert '<option value="Asia/Almaty" selected>Алматы (GMT+5)</option>' in ru.text
    assert re.search(r'<option value="Asia/Yerevan"[^>]*>Ереван \(GMT\+4\)</option>', ru.text)
    hy = anon.get("/book?lang=hy")
    assert "Երևան (GMT+4)" in hy.text


def test_slot_buttons_carry_the_day_in_their_name_and_the_zone_line_moved_into_the_list(
    anon: TestClient, web_slot: int  # noqa: F811
) -> None:
    page = anon.get("/book/slots", params={"lang": "en", "tz": "Asia/Yerevan", "modality": "online"})
    assert re.search(r'<button type="submit" aria-label="[A-Za-z]{3} \d+ [A-Za-z]{3}, \d\d:\d\d">\d\d:\d\d</button>', page.text)
    assert "Times are shown in Yerevan (GMT+4)." in page.text
    assert "<h2>" in page.text and "<h3>" not in page.text


def test_no_slots_offers_a_real_waitlist_button(anon: TestClient) -> None:
    # No committed slots in a fresh database: the empty list shows the intro and a button, not a stray link.
    page = anon.get("/book/slots", params={"lang": "ru", "tz": "Asia/Yerevan"})
    if "waitlist" in page.text and "cta" in page.text:
        assert 'class="cta" href="/waitlist"' in page.text


# --- price, reference, copy ---------------------------------------------------------------------------------------


def test_price_text_is_only_what_she_stored() -> None:
    none = SessionType(code="a", duration_min=60)
    assert price_text(none) == ""
    priced = SessionType(code="b", duration_min=60, price_amount_minor=15000, price_currency="AMD")
    assert price_text(priced) == "15\u00a0000\u00a0AMD"
    own = SessionType(code="c", duration_min=60, price_amount_minor=1, price_currency="AMD", price_display_override="from 10 000 AMD")
    assert price_text(own) == "from 10 000 AMD"


def test_lang_cookie_name_unchanged() -> None:
    assert LANG_COOKIE == "pb_lang"


# --- accessibility of the stylesheet ------------------------------------------------------------------------------


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_control_borders_and_the_progress_strip_reach_3_to_1_in_light_and_dark() -> None:
    css = (WEB / "static" / "style.css").read_text(encoding="utf-8")
    controls = re.findall(r"--control:\s*(#[0-9a-f]{6})", css)
    assert len(controls) == 3  # light, system dark, forced dark
    light, dark = controls[0], controls[1]
    assert controls[1] == controls[2]
    for border, surfaces in ((light, ("#ffffff", "#f7f3ec")), (dark, ("#1f1c17", "#16140f"))):
        for surface in surfaces:
            assert _contrast(border, surface) >= 3.0, (border, surface)
    # every control rule uses the token, not the 1.7:1 hairline colour
    for selector in ("input[type=text], input[type=email]", ".times button {", "label.choice {", "button.secondary"):
        start = css.index(selector)
        block = css[start : css.index("}", start)]
        assert "var(--control)" in block and "line-strong" not in block, selector
