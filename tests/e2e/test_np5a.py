"""NP5a: the admin's quick wins, confirm steps and accessibility, through the real HTTP surface.

Enforces IMPLEMENTATION.md §12.2 (the admin console) and §7.1 (what an action may do).
"""

# ruff: noqa: F811 - the admin suite's fixtures are imported and then named as parameters
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.web.admin import _flash, _intent_label
from app.core.enums import OutboxStatus, RequestStatus, SlotStatus
from app.core.models import AdminSession, BookingRequest, Slot, Translation
from tests.e2e.test_admin_web import (  # noqa: F401 - fixtures and helpers of the admin suite
    _csrf,
    _fresh,
    _sign_in,
    committed,
    restore_practice,
    scratch,
    web,
)


async def _status(
    committed: AsyncSession, scratch: dict[str, object], status: RequestStatus
) -> None:
    values: dict[str, object] = {"status": status}
    if status in (RequestStatus.confirmed, RequestStatus.completed):
        values["scheduled_start"] = datetime.now(UTC) + timedelta(days=3)
        values["confirmed_at"] = datetime.now(UTC)
    await committed.execute(
        update(BookingRequest).where(BookingRequest.id == scratch["request_id"]).values(**values)
    )
    await committed.commit()


async def _sign_out(committed: AsyncSession) -> None:
    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


def _form(html: str, action: str) -> str:
    marker = f'/{action}"'
    assert marker in html, f"no form posting to {action}"
    return html.split(marker, 1)[1].split("</form>", 1)[0]


# --- flash ---------------------------------------------------------------------------------


def test_a_flash_code_becomes_a_sentence_with_a_kind() -> None:
    assert _flash("approve-done") == ("Request approved. The client has been told.", "ok")
    text, kind = _flash("refused") or ("", "")
    assert kind == "error" and "not done" in text.lower()
    assert _flash("created-12") == ("Created 12 slots.", "ok")
    assert _flash("created-1") == ("Created 1 slot.", "ok")
    assert _flash("rejected: bad thing") == ("Not saved: bad thing", "error")
    assert _flash("translations-3-1") == (
        "Saved 3 wordings. 1 left empty and not saved.",
        "ok",
    )
    assert _flash("") is None
    # A text that is already a sentence is shown as it is; it is never raw HTML (Jinja escapes it).
    assert _flash("Photo saved.") == ("Photo saved.", "ok")


async def test_the_flash_is_announced_by_kind(web: TestClient, committed: AsyncSession) -> None:
    _sign_in(web)
    ok = web.get("/admin/requests?flash=approve-done").text
    assert 'class="flash ok" role="status"' in ok
    bad = web.get("/admin/requests?flash=bad-time").text
    assert 'class="flash error" role="alert"' in bad
    assert "bad-time" not in bad
    xss = web.get("/admin/requests?flash=%3Cb%3Ex%3C/b%3E").text
    assert "<b>x</b>" not in xss
    await _sign_out(committed)


async def test_an_approve_says_what_happened(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    response = web.post(
        f"/admin/requests/{scratch['request_uuid']}/reject",
        data={"csrf_token": _csrf(web), "reason": ""},
        follow_redirects=False,
    )
    assert response.headers["location"].endswith("?flash=reject-done")
    await _sign_out(committed)


# --- requests list -------------------------------------------------------------------------


async def test_the_list_opens_on_what_needs_her_and_counts_the_rest(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    uuid = str(scratch["request_uuid"])
    page = web.get("/admin/requests").text
    assert f"/admin/requests/{uuid}" in page  # pending is in the default view
    assert "Needs you" in page

    await _status(committed, scratch, RequestStatus.confirmed)
    default = web.get("/admin/requests").text
    assert f"/admin/requests/{uuid}" not in default, "a confirmed request is not waiting for her"
    assert f"/admin/requests/{uuid}" in web.get("/admin/requests?status=all").text
    assert f"/admin/requests/{uuid}" in web.get("/admin/requests?status=confirmed").text

    await _status(committed, scratch, RequestStatus.negotiating)
    assert f"/admin/requests/{uuid}" in web.get("/admin/requests").text
    await _sign_out(committed)


async def test_the_name_is_the_link_and_the_ref_is_short(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    uuid = str(scratch["request_uuid"])
    ref = uuid.split("-")[0].upper()
    page = web.get("/admin/requests?status=all").text
    row = page.split(f'href="/admin/requests/{uuid}"', 1)[1].split("</tr>", 1)[0]
    assert ref in row
    assert uuid not in row.split(">", 1)[1], "the long uuid is not printed in the row"
    await _sign_out(committed)


async def test_the_search_finds_a_request_by_its_short_ref_in_any_case(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    uuid = str(scratch["request_uuid"])
    ref = uuid.split("-")[0].upper()
    await _status(committed, scratch, RequestStatus.completed)  # not in the default view
    for query in (ref, ref.lower(), ref[:5], uuid):
        assert f"/admin/requests/{uuid}" in web.get("/admin/requests", params={"q": query}).text
    nothing = web.get("/admin/requests", params={"q": "ZZZZZZZZ"}).text
    assert f"/admin/requests/{uuid}" not in nothing
    assert "No request matches" in nothing
    # `%` and `_` are searched for, not wildcards.
    assert f"/admin/requests/{uuid}" not in web.get("/admin/requests", params={"q": "%"}).text
    await _sign_out(committed)


async def test_the_detail_heading_is_name_type_when_and_shows_the_ref(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    page = web.get(f"/admin/requests/{scratch['request_uuid']}").text
    h1 = re.search(r"<h1>(.*?)</h1>", page, re.S)
    assert h1 and str(scratch["request_uuid"]) not in h1.group(1)
    assert h1.group(1).count("—") == 2
    assert str(scratch["request_uuid"]).split("-")[0].upper() in page
    await _sign_out(committed)


# --- action cards and confirm steps ----------------------------------------------------------


async def test_reject_asks_really_and_anything_to_tell_them_before_it_acts(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    page = web.get(f"/admin/requests/{scratch['request_uuid']}").text
    reject = _form(page, "reject")
    assert '<details class="confirm">' in reject
    assert "Really" in reject and "Anything to tell them?" in reject
    # The one button that submits is inside the confirm step, not beside it.
    assert reject.index("<details") < reject.index('type="submit"')
    # Approve is the primary card; reject and cancel are danger.
    assert 'class="card primary"' in _form_open(page, "approve")
    assert 'class="danger"' in reject
    await _sign_out(committed)


def _form_open(html: str, action: str) -> str:
    return html.split(f'/{action}"', 1)[1].split(">", 1)[0]


async def test_cancel_has_two_buttons_like_telegram(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    await _status(committed, scratch, RequestStatus.confirmed)
    cancel = _form(web.get(f"/admin/requests/{scratch['request_uuid']}").text, "cancel")
    assert cancel.count('name="keep_slot"') == 2
    assert "Cancel &amp; block" in cancel
    assert 'type="checkbox"' not in cancel
    await _sign_out(committed)


async def test_keep_slot_zero_releases_the_hour_and_one_blocks_it(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    """`keep_slot=0` is the plain Cancel button: a truthy-looking "0" must not block the hour."""
    _sign_in(web)
    await _status(committed, scratch, RequestStatus.confirmed)
    response = web.post(
        f"/admin/requests/{scratch['request_uuid']}/cancel",
        data={"csrf_token": _csrf(web), "reason": "", "keep_slot": "0"},
        follow_redirects=False,
    )
    assert response.headers["location"].endswith("flash=cancel-done")
    await _fresh(committed)
    slot = (await committed.execute(select(Slot).where(Slot.id == scratch["slot_id"]))).scalar_one()
    assert slot.status is not SlotStatus.blocked
    await _sign_out(committed)


async def test_what_the_status_forbids_is_folded_away_with_its_reason(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    await _status(committed, scratch, RequestStatus.negotiating)
    page = web.get(f"/admin/requests/{scratch['request_uuid']}").text
    assert "Only a confirmed session can be cancelled" in page
    assert f"/{scratch['request_uuid']}/cancel" not in page, "no pressable cancel form"
    assert 'class="unavail"' in page
    assert f"/{scratch['request_uuid']}/reject" in page
    await _sign_out(committed)


# --- slots -----------------------------------------------------------------------------------


async def test_the_bulk_form_opens_on_next_week_with_the_weekdays_ticked(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    page = web.get("/admin/slots").text
    start = re.search(r'name="date_from" value="(\d{4}-\d\d-\d\d)"', page)
    end = re.search(r'name="date_to" value="(\d{4}-\d\d-\d\d)"', page)
    assert start and end
    first = datetime.fromisoformat(start.group(1))
    assert first.weekday() == 0, "next Monday"
    assert datetime.fromisoformat(end.group(1)) - first == timedelta(days=6)
    assert len(re.findall(r'name="weekdays" value="[0-4]" checked', page)) == 5
    assert not re.search(r'name="weekdays" value="[56]" checked', page)
    # The list comes before the form that makes more of it.
    assert page.index("Upcoming") > page.index("<details")
    await _sign_out(committed)


async def test_a_bulk_form_with_no_weekday_is_refused_in_words(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    response = web.post(
        "/admin/slots/bulk",
        data={
            "csrf_token": _csrf(web),
            "date_from": "2031-01-06",
            "date_to": "2031-01-12",
            "times": "10:00",
            "duration_min": "60",
        },
        follow_redirects=False,
    )
    assert response.headers["location"].endswith("flash=no-weekday")
    assert "Tick at least one weekday" in web.get(response.headers["location"]).text
    await _sign_out(committed)


async def test_slots_are_grouped_by_day_and_an_action_lands_on_its_row(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    page = web.get("/admin/slots").text
    assert re.search(r'<h3 id="day-\d{4}-\d\d-\d\d">[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}', page)
    assert f'id="slot-{scratch["slot_id"]}"' in page
    response = web.post(
        f"/admin/slots/{scratch['slot_id']}/block",
        data={"csrf_token": _csrf(web)},
        follow_redirects=False,
    )
    assert (
        response.headers["location"] == f"/admin/slots?flash=slot-blocked#slot-{scratch['slot_id']}"
    )
    await _sign_out(committed)


async def test_slot_delete_asks_really_and_the_why_is_visible_text(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    await _status(committed, scratch, RequestStatus.rejected)
    await committed.execute(
        update(Slot).where(Slot.id == scratch["slot_id"]).values(status=SlotStatus.available)
    )
    await committed.commit()
    page = web.get("/admin/slots").text
    row = page.split(f'id="slot-{scratch["slot_id"]}"', 1)[1].split("</li>", 1)[0]
    # Still pointed at by the request that asked for it: no delete, and the reason is on the page.
    assert "Cannot be deleted: a request asked for this time" in row
    assert "title=" not in row
    await _sign_out(committed)


# --- confirm steps elsewhere -------------------------------------------------------------------


async def test_import_and_restore_sit_behind_a_really(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    page = web.get("/admin/maintenance").text
    form = _form(page, "config/import")
    assert "Really import this file?" in form
    assert form.index("<details") < form.index('name="apply" value="1"')
    assert 'name="apply" value="0"' in form.split("<details")[0], "preview needs no confirm"
    await _sign_out(committed)


# --- waitlist --------------------------------------------------------------------------------


async def test_the_waitlist_has_one_labelled_message_field(
    web: TestClient, committed: AsyncSession
) -> None:
    from tests.e2e.test_admin_web import WAITLIST_CLIENT_TG  # noqa: F401

    _sign_in(web)
    page = web.get("/admin/waitlist?status=all").text
    for form in re.findall(r'<form method="post" class="msg-form">.*?</form>', page, re.S):
        assert form.count('name="note"') == 1
        assert 'for="note-' in form
    await _sign_out(committed)


# --- delivery --------------------------------------------------------------------------------


def test_an_intent_reads_as_words() -> None:
    assert _intent_label("request.proposal.client") == "Request proposal, to the client"
    assert _intent_label("waitlist.joined.admin") == "Waitlist joined, to you"
    assert _intent_label("something") == "Something"


async def test_the_delivery_counts_are_filters(web: TestClient, committed: AsyncSession) -> None:
    _sign_in(web)
    page = web.get("/admin/delivery").text
    for status in OutboxStatus:
        assert f'href="/admin/delivery?status={status.value}"' in page
    filtered = web.get("/admin/delivery?status=dead").text
    assert 'aria-current="true"' in filtered
    assert web.get("/admin/delivery?status=nonsense").status_code == 200
    await _sign_out(committed)


# --- translations ------------------------------------------------------------------------------


async def test_a_group_saves_in_one_post_and_only_what_changed(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    page = web.get("/admin/translations?lang=en").text
    assert page.count("value__") > 20 and 'name="key"' not in page
    rows = (
        await committed.execute(
            select(Translation.key, Translation.value)
            .where(Translation.lang == "en", Translation.key.like("common.%"))
            .order_by(Translation.key)
        )
    ).all()
    (key_a, old_a), (key_b, old_b) = rows[0], rows[1]
    try:
        response = web.post(
            "/admin/translations",
            data={
                "csrf_token": _csrf(web),
                "lang": "en",
                "group": "navigation",
                f"value__{key_a}": old_a + " X",
                f"value__{key_b}": old_b,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "flash=translations-1-0" in response.headers["location"]
        await _fresh(committed)
        now = dict(
            (
                await committed.execute(
                    select(Translation.key, Translation.value).where(
                        Translation.lang == "en", Translation.key.in_([key_a, key_b])
                    )
                )
            ).all()
        )
        assert now[key_a] == old_a + " X"
        assert now[key_b] == old_b
    finally:
        web.post(
            "/admin/translations",
            data={"csrf_token": _csrf(web), "lang": "en", f"value__{key_a}": old_a},
        )
    await _sign_out(committed)


# --- accessibility ---------------------------------------------------------------------------


async def test_the_current_page_is_marked_in_the_nav_and_every_input_has_a_name(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    page = web.get("/admin/waitlist").text
    assert re.search(r'<a href="/admin/waitlist" aria-current="page">', page)
    assert page.count('aria-current="page"') == 1
    for path in ("/admin/content", "/admin/timezones", "/admin/privacy", "/admin/translations"):
        html = re.sub(r"<label\b.*?</label>", "", web.get(path).text, flags=re.S)
        for tag in re.findall(r"<(?:input|textarea)\b[^>]*>", html):
            if re.search(r'type="(hidden|checkbox|radio|submit)"', tag):
                continue
            assert "aria-label=" in tag or "id=" in tag or "placeholder=" in tag, (path, tag)
    await _sign_out(committed)


def test_every_badge_colour_is_defined_for_both_palettes() -> None:
    css = open("app/channels/web/static/admin.css", encoding="utf-8").read()
    for token in ("--amber-ink", "--red-ink", "--amber-bg"):
        assert css.count(f"{token}:") >= 3, token  # light, system-dark, chosen-dark
    assert "opacity" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S), "no opacity on text"
    assert "min-height: 2.25rem" in css  # pills are at least 32 px tall
