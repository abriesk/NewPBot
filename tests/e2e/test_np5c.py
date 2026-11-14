"""NP5c: the admin on a phone -- the five-destination bar and the More page, the week grid's one
day at a time, the Slots page folded by day, the stacked tables.

Enforces IMPLEMENTATION.md §12.2 (the admin console).
"""

# ruff: noqa: F811 - the admin suite's fixtures are imported and then named as parameters
from __future__ import annotations

import re
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.web.admin import _day_back, _grid_url
from app.core.enums import SlotStatus
from tests.e2e.test_admin_web import (  # noqa: F401 - fixtures and helpers of the admin suite
    committed,
    restore_practice,
    scratch,
    waitlist_scratch,
    web,
)
from tests.e2e.test_np5b import MONDAY, _clean, _post, _sign_in, _slots  # noqa: F401


def _tabs(html: str) -> str:
    return html.split('<nav class="tabs"', 1)[1].split("</nav>", 1)[0]


def _current(html: str) -> list[str]:
    return re.findall(r'<a href="[^"]*" aria-current="page">([^<]*)</a>', _tabs(html))


# --- navigation -----------------------------------------------------------------------------------


def test_the_bar_has_five_destinations_and_more_and_marks_the_current_one(web: TestClient) -> None:
    _sign_in(web)
    html = web.get("/admin").text
    links = re.findall(r"<a href=\"([^\"]*)\"[^>]*>([^<]*)</a>", _tabs(html))
    assert [label for _, label in links] == [
        "Today",
        "Requests",
        "Schedule",
        "Slots",
        "Waitlist",
        "More",
    ]
    assert {label: href for href, label in links}["More"] == "/admin/more"
    for path, label in (
        ("/admin", "Today"),
        ("/admin/requests", "Requests"),
        ("/admin/requests?status=all", "Requests"),
        ("/admin/requests?view=grid", "Schedule"),
        ("/admin/slots", "Slots"),
        ("/admin/waitlist", "Waitlist"),
        ("/admin/more", "More"),
        ("/admin/settings", "More"),
        ("/admin/delivery", "More"),
        ("/admin/status", "More"),
    ):
        assert _current(web.get(path).text) == [label], path


def test_more_lists_every_other_page_and_needs_the_sign_in(web: TestClient) -> None:
    gone = web.get("/admin/more", follow_redirects=False)
    assert gone.status_code in (302, 303) and gone.headers["location"].startswith("/admin/login")
    _sign_in(web)
    page = web.get("/admin/more")
    assert page.status_code == 200
    main = page.text.split("<main", 1)[1]
    for href in (
        "/admin/clients",
        "/admin/delivery",
        "/admin/session-types",
        "/admin/content",
        "/admin/translations",
        "/admin/timezones",
        "/admin/settings",
        "/admin/privacy",
        "/admin/maintenance",
        "/admin/status",
        "/admin/help",
    ):
        assert f'<a href="{href}">' in main, href


# --- the week grid, one day -----------------------------------------------------------------------


async def test_the_grid_marks_one_day_as_shown_and_a_bad_day_never_raises(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    wednesday = MONDAY + timedelta(days=2)
    page = web.get(f"/admin/requests?view=grid&start={MONDAY}&day={wednesday}").text
    assert page.count(" shown") == 1 and f'id="d-{wednesday}"' in page
    assert re.search(rf'<section class="wday shown" id="d-{wednesday}"', page)
    assert f'day={wednesday}" aria-current="date"' in page
    # Not given, outside the week, not a date, or an enormous string: the Monday (a past week has
    # no today), and a 200 each time.
    for day in ("", str(MONDAY + timedelta(days=9)), "not-a-date", "9" * 5000, "2028-13-45"):
        response = web.get(f"/admin/requests?view=grid&start={MONDAY}&day={day}")
        assert response.status_code == 200, day[:20]
        assert re.search(rf'<section class="wday shown" id="d-{MONDAY}"', response.text), day[:20]
    # Previous and next day cross the week edge by naming the week they land in.
    assert f"start={MONDAY - timedelta(days=1)}&amp;day={MONDAY - timedelta(days=1)}" in page or (
        f"start={wednesday - timedelta(days=1)}&amp;day={wednesday - timedelta(days=1)}" in page
    )
    assert f"start={wednesday + timedelta(days=1)}&amp;day={wednesday + timedelta(days=1)}" in page


async def test_a_cell_action_comes_back_to_the_day_it_was_on(
    web: TestClient, committed: AsyncSession
) -> None:
    thursday = MONDAY + timedelta(days=3)
    (slot_id,) = await _slots(committed, thursday, ["10:00"])
    _sign_in(web)
    page = web.get(f"/admin/requests?view=grid&start={MONDAY}&day={thursday}").text
    cell = page.split(f'id="slot-{slot_id}"', 1)[1].split("</div>", 1)[0]
    assert f'name="week" value="{thursday}"' in cell
    location = _post(web, f"/admin/slots/{slot_id}/block", week=str(thursday))
    assert location.startswith(f"/admin/requests?view=grid&start={MONDAY}&day={thursday}&flash=")
    location = _post(
        web, "/admin/slots/block-range", date_from=str(thursday), do="unblock", week=str(thursday)
    )
    assert f"start={MONDAY}&day={thursday}" in location


def test_the_day_helpers() -> None:
    assert _day_back("2028-06-08") == date(2028, 6, 8)
    assert _day_back("") is None and _day_back("junk") is None
    assert _grid_url(MONDAY) == f"/admin/requests?view=grid&start={MONDAY}"
    assert _grid_url(MONDAY, "x", MONDAY).endswith(f"&day={MONDAY}&flash=x")


# --- Slots, folded by day -------------------------------------------------------------------------


async def test_slots_are_folded_by_day_with_a_count_and_only_the_next_days_open(
    web: TestClient, committed: AsyncSession
) -> None:
    for offset in range(4):
        await _slots(committed, MONDAY + timedelta(days=offset), ["10:00", "11:00", "12:00"])
    await _slots(committed, MONDAY + timedelta(days=1), ["15:00"], SlotStatus.blocked)
    _sign_in(web)
    page = web.get("/admin/slots").text
    tags = re.findall(r'<details class="slot-day" id="day-([\d-]+)"( open)?>', page)
    assert len(tags) >= 4
    assert sum(1 for _, opened in tags if opened) <= 2  # today and the next day with slots
    mine = {day: bool(opened) for day, opened in tags if day.startswith("2028-")}
    assert mine[str(MONDAY + timedelta(days=3))] is False  # never one of the first two days
    assert f'id="day-{MONDAY + timedelta(days=1)}"' in page
    assert "4 slots · 3 free · 1 blocked" in page and "3 slots · 3 free" in page
    # A single slot reads "1 slot", and the slot rows still carry their anchors.
    assert "#slot-" in page or 'id="slot-' in page


# --- tables as cards ------------------------------------------------------------------------------


def test_requests_clients_and_delivery_tables_become_cards_on_a_phone(web: TestClient) -> None:
    _sign_in(web)
    for path in ("/admin/requests?status=all", "/admin/clients", "/admin/delivery"):
        html = web.get(path).text
        assert '<div class="scroll stacked">' in html, path
        assert ('role="table"' in html and 'data-label="' in html) or "Nothing" in html, path
    css = open("app/channels/web/static/admin.css", encoding="utf-8").read()
    assert ".stacked td[data-label]::before" in css and "max-width: 40rem" in css
    assert "nav.tabs { display: none; }" in css  # the phone bar is hidden on a wide screen
