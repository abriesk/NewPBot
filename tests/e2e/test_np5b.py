"""NP5b: the admin's Today page, slots on the week grid, free-time chips, repeat-a-week and week
templates, through the real HTTP surface.

Enforces IMPLEMENTATION.md §12.2 (the admin console) and §7.1 (what an action may do).
"""

# ruff: noqa: F811 - the admin suite's fixtures are imported and then named as parameters
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.web.admin import _copy_sentence, _flash
from app.core.enums import RequestStatus, SlotStatus
from app.core.models import (
    AdminSession,
    BookingRequest,
    NegotiationMessage,
    OutboxMessage,
    Practice,
    Slot,
    WeekTemplate,
)
from tests.e2e.test_admin_web import (  # noqa: F401 - fixtures and helpers of the admin suite
    ADMIN_PASSWORD,
    ADMIN_USER,
    _csrf,
    _fresh,
    _sign_in,
    committed,
    restore_practice,
    scratch,
    waitlist_scratch,
    web,
)

#: Slots and templates these tests make live from 2028 (the `scratch` teardown removes the slots
#: too); the week below starts on a Monday.
MONDAY = date(2028, 6, 5)

#: Every slot id `_slots` made, so a test that used near dates (the chips) leaves nothing behind
#: on the client's picker for the booking suites that run after it.
_MADE: list[int] = []


@pytest_asyncio.fixture(autouse=True)
async def _clean(committed: AsyncSession) -> AsyncIterator[None]:
    async def sweep() -> None:
        await committed.rollback()
        await committed.execute(
            delete(Slot).where(Slot.starts_at >= datetime(2028, 1, 1, tzinfo=UTC))
        )
        if _MADE:
            await committed.execute(delete(Slot).where(Slot.id.in_(_MADE)))
            _MADE.clear()
        await committed.execute(delete(WeekTemplate))
        await committed.commit()

    await sweep()
    yield
    await sweep()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def _zone(committed: AsyncSession) -> ZoneInfo:
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    return ZoneInfo(practice.timezone)


async def _slots(
    committed: AsyncSession, day: date, times: list[str], status: SlotStatus = SlotStatus.available
) -> list[int]:
    zone = await _zone(committed)
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    ids = []
    for at in times:
        slot = Slot(
            practice_id=practice.id,
            starts_at=datetime.combine(day, time.fromisoformat(at), tzinfo=zone).astimezone(UTC),
            duration_min=60,
            status=status,
        )
        committed.add(slot)
        await committed.flush()
        ids.append(int(slot.id))
        _MADE.append(int(slot.id))
    await committed.commit()
    return ids


async def _day_status(committed: AsyncSession, day: date) -> dict[str, str]:
    await _fresh(committed)
    zone = await _zone(committed)
    rows = (
        (
            await committed.execute(
                select(Slot).where(
                    Slot.starts_at >= datetime.combine(day, time.min, tzinfo=zone),
                    Slot.starts_at
                    < datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone),
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.starts_at.astimezone(zone).strftime("%H:%M"): r.status.value for r in rows}


def _post(web: TestClient, path: str, **data: str) -> str:
    """POST a form; the redirect it answers with (a 303 is the only good answer)."""
    response = web.post(path, data={"csrf_token": _csrf(web), **data}, follow_redirects=False)
    assert response.status_code == 303, (path, response.status_code)
    return response.headers["location"]


def _flash_of(web: TestClient, location: str) -> str:
    """The sentence the page at `location` shows as its flash."""
    html = web.get(location).text
    match = re.search(r'<p class="flash[^"]*" role="[a-z]+">(.*?)</p>', html, re.S)
    assert match, f"no flash on {location}"
    return match.group(1)


# --- Today -------------------------------------------------------------------------------------


def test_today_needs_the_sign_in(web: TestClient) -> None:
    response = web.get("/admin", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("/admin/login")


async def test_sign_in_lands_on_today(web: TestClient, committed: AsyncSession) -> None:
    web.get("/admin/login")
    response = web.post(
        "/admin/login",
        data={
            "csrf_token": _csrf(web),
            "username": ADMIN_USER,
            "password": ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/admin"


async def test_today_lists_what_waits_with_approve_only_where_the_slot_is_held(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    uuid = str(scratch["request_uuid"])
    page = web.get("/admin").text
    assert "Waiting for you" in page and f"/admin/requests/{uuid}" in page
    assert f"/admin/requests/{uuid}/approve" not in page, "its slot is not held: no inline Approve"

    # The client's slot is held for them: now one tap approves.
    await committed.execute(
        update(Slot)
        .where(Slot.id == scratch["slot_id"])
        .values(
            status=SlotStatus.held,
            held_by_request=scratch["request_id"],
            hold_expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
    )
    await committed.commit()
    page = web.get("/admin").text
    form = page.split(f'action="/admin/requests/{uuid}/approve"', 1)[1].split("</form>", 1)[0]
    assert 'name="back" value="today"' in form

    location = _post(web, f"/admin/requests/{uuid}/approve", back="today")
    assert location == "/admin?flash=approve-done"
    assert "Request approved" in _flash_of(web, location)
    await _fresh(committed)
    status = (
        await committed.execute(
            select(BookingRequest.status).where(BookingRequest.id == scratch["request_id"])
        )
    ).scalar_one()
    assert status is RequestStatus.confirmed


async def test_today_shows_tomorrows_session_but_not_the_day_after(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    zone = await _zone(committed)
    tomorrow = datetime.now(zone).date() + timedelta(days=1)

    def at(day: date) -> datetime:
        return datetime.combine(day, time(10, 0), tzinfo=zone).astimezone(UTC)

    await committed.execute(
        update(BookingRequest)
        .where(BookingRequest.id == scratch["request_id"])
        .values(
            status=RequestStatus.confirmed,
            scheduled_start=at(tomorrow),
            confirmed_at=datetime.now(UTC),
            display_name="Tomorrow Person",
            slot_id=None,
        )
    )
    await committed.commit()
    _sign_in(web)
    page = web.get("/admin").text
    block = page.split('id="days-h"', 1)[1]
    assert "Tomorrow Person" in block.split("Tomorrow", 1)[1]
    assert "10:00" in block

    await committed.execute(
        update(BookingRequest)
        .where(BookingRequest.id == scratch["request_id"])
        .values(scheduled_start=at(tomorrow + timedelta(days=1)))
    )
    await committed.commit()
    assert "Tomorrow Person" not in web.get("/admin").text


async def test_today_counts_undelivered_messages_and_links_to_delivery_filters(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    _sign_in(web)
    assert "/admin/delivery?status=dead" not in web.get("/admin").text
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    from app.core.enums import Channel, OutboxStatus

    committed.add(
        OutboxMessage(
            practice_id=practice.id,
            channel=Channel.email,
            address="np5b@example.invalid",
            intent_key="request.confirmed.client",
            payload={},
            locale="en",
            status=OutboxStatus.dead,
            request_id=scratch["request_id"],
        )
    )
    await committed.commit()
    try:
        page = web.get("/admin").text
        assert re.search(r'href="/admin/delivery\?status=dead">\d+ gave up', page)
        assert "Messages not delivered" in page
    finally:
        await committed.execute(
            delete(OutboxMessage).where(OutboxMessage.address == "np5b@example.invalid")
        )
        await committed.commit()


async def test_the_two_daily_switches_live_on_today_and_settings_keeps_the_rest(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    today = web.get("/admin").text
    assert 'action="/admin/switch/availability"' in today
    assert 'action="/admin/switch/online-only"' in today

    location = _post(web, "/admin/switch/availability", value="off")
    assert location == "/admin?flash=availability-off"
    assert "Accepting new bookings is off" in _flash_of(web, location)
    await _fresh(committed)
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.availability_on is False

    _post(web, "/admin/switch/online-only", value="on")
    _post(web, "/admin/switch/availability", value="on")
    await _fresh(committed)
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.availability_on is True and practice.online_only is True

    settings = web.get("/admin/settings").text
    assert 'name="availability_on"' not in settings and 'name="online_only"' not in settings
    # Saving Settings must not read the missing boxes as "off".
    _post(web, "/admin/settings", name=practice.name, timezone=practice.timezone)
    await _fresh(committed)
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.availability_on is True and practice.online_only is True
    _post(web, "/admin/switch/online-only", value="off")


def test_an_unknown_switch_is_a_404(web: TestClient) -> None:
    _sign_in(web)
    response = web.post(
        "/admin/switch/captcha",
        data={"csrf_token": _csrf(web), "value": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 404
    refused = web.post("/admin/switch/availability", data={"value": "off"}, follow_redirects=False)
    assert refused.status_code == 403


def test_the_old_routes_keep_working(web: TestClient) -> None:
    _sign_in(web)
    for path in ("/admin/requests", "/admin/requests?view=grid", "/admin/slots", "/admin/settings"):
        assert web.get(path).status_code == 200, path
    nav = web.get("/admin").text
    assert '<a href="/admin" aria-current="page">Today</a>' in nav


# --- the week grid with slots --------------------------------------------------------------------


async def test_the_grid_draws_free_and_blocked_slots_with_block_on_the_cell(
    web: TestClient, committed: AsyncSession
) -> None:
    free, blocked = (
        await _slots(committed, MONDAY, ["10:00"]),
        await _slots(committed, MONDAY + timedelta(days=1), ["10:00"], SlotStatus.blocked),
    )
    _sign_in(web)
    page = web.get(f"/admin/requests?view=grid&start={MONDAY}").text
    cell = page.split(f'id="slot-{free[0]}"', 1)[1].split("</div>", 1)[0]
    assert "Free" in cell and f"/admin/slots/{free[0]}/block" in cell
    assert f'name="week" value="{MONDAY}"' in cell
    cell = page.split(f'id="slot-{blocked[0]}"', 1)[1].split("</div>", 1)[0]
    assert "Blocked" in cell and f"/admin/slots/{blocked[0]}/unblock" in cell
    assert f'id="d-{MONDAY}"' in page and "Block day" in page


async def test_a_cell_block_goes_back_to_the_same_week(
    web: TestClient, committed: AsyncSession
) -> None:
    (slot_id,) = await _slots(committed, MONDAY, ["10:00"])
    _sign_in(web)
    location = _post(web, f"/admin/slots/{slot_id}/block", week=str(MONDAY + timedelta(days=2)))
    assert location.startswith(f"/admin/requests?view=grid&start={MONDAY}")
    assert "flash=slot-blocked" in location
    assert (await _day_status(committed, MONDAY)) == {"10:00": "blocked"}
    location = _post(web, f"/admin/slots/{slot_id}/unblock")  # no week: the Slots page, as before
    assert location.startswith("/admin/slots")
    assert (await _day_status(committed, MONDAY)) == {"10:00": "available"}


async def test_block_this_day_blocks_it_in_one_submit(
    web: TestClient, committed: AsyncSession
) -> None:
    await _slots(committed, MONDAY, ["10:00", "11:00", "15:00"])
    await _slots(committed, MONDAY + timedelta(days=1), ["10:00"])
    _sign_in(web)
    location = _post(
        web, "/admin/slots/block-range", date_from=str(MONDAY), do="block", week=str(MONDAY)
    )
    assert "flash=blocked-3" in location and "view=grid" in location
    assert "Blocked 3 slots" in _flash_of(web, location)
    assert set((await _day_status(committed, MONDAY)).values()) == {"blocked"}
    assert set((await _day_status(committed, MONDAY + timedelta(days=1))).values()) == {"available"}
    # And the header now offers to free the day again.
    assert "Free day" in web.get(f"/admin/requests?view=grid&start={MONDAY}").text
    _post(web, "/admin/slots/block-range", date_from=str(MONDAY), do="unblock", week=str(MONDAY))
    assert set((await _day_status(committed, MONDAY)).values()) == {"available"}


async def test_blocking_a_range_refuses_over_a_booked_slot_and_lists_its_time(
    web: TestClient, committed: AsyncSession, scratch: dict[str, object]
) -> None:
    await _slots(committed, MONDAY, ["10:00"])
    (booked,) = await _slots(committed, MONDAY + timedelta(days=1), ["14:30"])
    await committed.execute(
        update(Slot)
        .where(Slot.id == booked)
        .values(status=SlotStatus.booked, booked_request=scratch["request_id"])
    )
    await committed.commit()
    _sign_in(web)
    location = _post(
        web,
        "/admin/slots/block-range",
        date_from=str(MONDAY),
        date_to=str(MONDAY + timedelta(days=2)),
    )
    assert 'class="flash error" role="alert"' in web.get(location).text, "a refusal is an alert"
    sentence = _flash_of(web, location)
    assert "Nothing was blocked" in sentence and "14:30" in sentence
    assert "Test Client" not in location, "no client name rides the URL"
    assert (await _day_status(committed, MONDAY)) == {"10:00": "available"}  # all or nothing
    # An unreadable range is said, and changes nothing.
    bad = _post(web, "/admin/slots/block-range", date_from="soon")
    assert "dates could not be read" in _flash_of(web, bad)


# --- chips ---------------------------------------------------------------------------------------


async def test_the_request_page_offers_free_times_as_chips_and_one_tap_proposes(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    zone = await _zone(committed)
    soon = datetime.now(zone).date() + timedelta(days=3)
    await _slots(committed, soon, ["09:00", "17:30"])
    _sign_in(web)
    uuid = str(scratch["request_uuid"])
    page = web.get(f"/admin/requests/{uuid}").text
    form = page.split(f'action="/admin/requests/{uuid}/propose"', 1)[1].split("</form>", 1)[0]
    value = f"{soon.isoformat()}T17:30"
    assert f'name="scheduled_start" value="{value}"' in form
    first_button = re.search(r"<button[^>]*>", form)
    assert first_button and "name=" not in first_button.group(0), (
        "Enter in the note must not tap somebody's time: the first submit button posts nothing"
    )
    assert "<textarea" in form and 'name="body"' in form

    _post(web, f"/admin/requests/{uuid}/propose", scheduled_start=value, body="See you\r\nthen")
    await _fresh(committed)
    message = (
        (
            await committed.execute(
                select(NegotiationMessage)
                .where(NegotiationMessage.request_id == scratch["request_id"])
                .order_by(NegotiationMessage.id.desc())
            )
        )
        .scalars()
        .first()
    )
    assert message is not None and message.body_text == "See you\nthen"
    assert message.proposed_start == datetime.combine(soon, time(17, 30), tzinfo=zone).astimezone(
        UTC
    )


async def test_chips_are_on_approve_and_move_and_the_free_time_is_for_this_request_only(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    zone = await _zone(committed)
    soon = datetime.now(zone).date() + timedelta(days=2)
    (free,) = await _slots(committed, soon, ["12:00"])
    (taken,) = await _slots(committed, soon, ["13:00"], SlotStatus.blocked)
    _sign_in(web)
    uuid = str(scratch["request_uuid"])
    approve = (
        web.get(f"/admin/requests/{uuid}")
        .text.split(f"/{uuid}/approve", 1)[1]
        .split("</form>", 1)[0]
    )
    assert f"{soon.isoformat()}T12:00" in approve
    assert f"{soon.isoformat()}T13:00" not in approve, "a blocked slot is not offered"
    assert free and taken
    # Confirmed: the Move card carries chips too, and the datetime stays as "another time".
    await committed.execute(
        update(BookingRequest)
        .where(BookingRequest.id == scratch["request_id"])
        .values(
            status=RequestStatus.confirmed,
            scheduled_start=datetime.now(UTC) + timedelta(days=5),
            confirmed_at=datetime.now(UTC),
        )
    )
    await committed.commit()
    move = (
        web.get(f"/admin/requests/{uuid}")
        .text.split(f"/{uuid}/reschedule", 1)[1]
        .split("</form>", 1)[0]
    )
    assert f"{soon.isoformat()}T12:00" in move and 'type="datetime-local"' in move
    assert "Another time" in move


# --- repeat a week and templates ---------------------------------------------------------------


async def test_repeat_week_reports_what_it_made_and_skips_what_exists(
    web: TestClient, committed: AsyncSession
) -> None:
    await _slots(committed, MONDAY, ["10:00", "11:00"])
    await _slots(committed, MONDAY + timedelta(days=2), ["15:00"])
    nxt = MONDAY + timedelta(days=7)
    await _slots(committed, nxt, ["10:00"])  # already open next week
    _sign_in(web)

    location = _post(web, "/admin/slots/repeat-week", source=str(MONDAY), week=str(MONDAY))
    assert location.startswith(f"/admin/requests?view=grid&start={nxt}"), "lands on the new week"
    sentence = _flash_of(web, location)
    assert (
        sentence.startswith("Copied this week to next week: 2 new slots.")
        and "1 already existed" in sentence
    )
    assert set((await _day_status(committed, nxt)).keys()) == {"10:00", "11:00"}
    assert (await _day_status(committed, nxt + timedelta(days=2))) == {"15:00": "available"}

    again = _post(web, "/admin/slots/repeat-week", source=str(MONDAY))
    assert again.startswith("/admin/slots") and "flash=repeated-0-3-0-0" in again
    assert _flash_of(web, again).startswith(
        "Copied this week to next week: no new slots. 3 already existed"
    )


async def test_the_slots_page_and_grid_carry_the_repeat_and_template_forms(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    for path in ("/admin/slots", f"/admin/requests?view=grid&start={MONDAY}"):
        page = web.get(path).text
        assert 'action="/admin/slots/repeat-week"' in page, path
        assert 'action="/admin/slots/templates"' in page, path
    assert 'action="/admin/slots/block-range"' in web.get("/admin/slots").text


async def test_templates_save_apply_and_delete_through_the_forms(
    web: TestClient, committed: AsyncSession
) -> None:
    await _slots(committed, MONDAY, ["10:00", "11:00"])
    await _slots(committed, MONDAY + timedelta(days=1), ["10:00"])
    _sign_in(web)

    empty = _post(web, "/admin/slots/templates", name="Void", week=str(MONDAY + timedelta(days=70)))
    assert "no slots to save" in _flash_of(web, empty)
    nameless = _post(web, "/admin/slots/templates", name="   ", week=str(MONDAY))
    assert "Give the template a name" in _flash_of(web, nameless)

    saved = _post(web, "/admin/slots/templates", name="Normal week", week=str(MONDAY))
    assert "Template saved with 3 slots a week." in _flash_of(web, saved)
    await _fresh(committed)
    template = (await committed.execute(select(WeekTemplate))).scalar_one()
    assert template.name == "Normal week" and len(template.entries) == 3
    tid = template.id
    page = web.get("/admin/slots").text
    assert f'/admin/slots/templates/{tid}/apply"' in page and "Normal week" in page

    first = MONDAY + timedelta(days=14)
    location = _post(
        web,
        f"/admin/slots/templates/{tid}/apply",
        date_from=str(first),
        date_to=str(first + timedelta(days=6)),
    )
    assert "Applied the template: 3 new slots." in _flash_of(web, location)
    assert set((await _day_status(committed, first)).keys()) == {"10:00", "11:00"}

    bad = _post(web, f"/admin/slots/templates/{tid}/apply", date_from="x", date_to="y")
    assert "dates could not be read" in _flash_of(web, bad)
    gone = _post(web, f"/admin/slots/templates/{tid}/delete")
    assert "Template deleted" in _flash_of(web, gone)
    missing = _post(
        web, f"/admin/slots/templates/{tid}/apply", date_from=str(first), date_to=str(first)
    )
    assert "no longer exists" in _flash_of(web, missing)
    nope = web.post(
        f"/admin/slots/templates/{tid}/frob",
        data={"csrf_token": _csrf(web)},
        follow_redirects=False,
    )
    assert nope.status_code == 404


# --- flash sentences ---------------------------------------------------------------------------


def test_the_new_flash_codes_become_sentences() -> None:
    assert _flash("blocked-4") == ("Blocked 4 slots. They are no longer offered.", "ok")
    assert _flash("blocked-1") == ("Blocked 1 slot. It is no longer offered.", "ok")
    assert _flash("blocked-0") == ("Nothing to do: there are no free slots in that range.", "ok")
    assert _flash("unblocked-2") == ("Freed 2 slots: offered again.", "ok")
    text, kind = _flash("no-block:Tue 6 Jun 10:00") or ("", "")
    assert kind == "error" and "Tue 6 Jun 10:00" in text and "Move or cancel" in text
    assert _flash("repeated-8-3-2-1") == (
        "Copied this week to next week: 8 new slots. 3 already existed; 2 in the past "
        "were skipped; "
        "1 blocked slot not copied.",
        "ok",
    )
    assert _copy_sentence("Copied", "1-0-0-0") == "Copied 1 new slot."
    assert _copy_sentence("Copied", "x-0-0") == ""
    assert _flash("availability-on") is not None and _flash("template-saved-5") is not None


async def _scalar(committed: AsyncSession, stmt: object) -> object:
    """One value, read on a session of its own: the `scratch` request lives on `committed`, and
    expunging it (as `_fresh` does) breaks that fixture's teardown."""
    async with AsyncSession(committed.bind) as reader:
        return (await reader.execute(stmt)).scalars().first()  # type: ignore[call-overload]


# --- review round 1 ------------------------------------------------------------------------------


def test_the_free_text_time_has_its_own_field_name_so_it_cannot_overwrite_a_chip() -> None:
    html = open("app/channels/web/templates/admin/request_detail.html", encoding="utf-8").read()
    assert 'type="datetime-local" name="scheduled_start"' not in html
    assert html.count('type="datetime-local" name="scheduled_other"') == 3
    assert html.count('<button type="submit" name="scheduled_start"') == 1  # the chip macro


async def _chip_case(
    web: TestClient,
    committed: AsyncSession,
    scratch: dict[str, object],
    action: str,
    how: str,
    days: int = 4,
) -> tuple[datetime, str]:
    """Post what a browser posts: the chip button AND the (empty) datetime field, or the reverse."""
    zone = await _zone(committed)
    # End the read's transaction: the `scratch` teardown rolls back, and a rollback with one open
    # expires the fixture's request, which its own teardown then cannot read.
    await committed.commit()
    when = datetime.combine(
        datetime.now(zone).date() + timedelta(days=days), time(16, 0), tzinfo=zone
    )
    value = when.strftime("%Y-%m-%dT%H:%M")
    if action == "reschedule":
        await committed.execute(
            update(BookingRequest)
            .where(BookingRequest.id == scratch["request_id"])
            .values(
                status=RequestStatus.confirmed,
                scheduled_start=datetime.now(UTC) + timedelta(days=9),
                confirmed_at=datetime.now(UTC),
            )
        )
        await committed.commit()
    _sign_in(web)
    fields = (
        {"scheduled_start": value, "scheduled_other": ""}
        if how == "chip"
        else {
            "scheduled_start": "",
            "scheduled_other": value,
        }
    )
    location = _post(web, f"/admin/requests/{scratch['request_uuid']}/{action}", **fields)
    assert location.endswith(f"flash={action}-done"), location
    return when.astimezone(UTC), location


async def test_a_chip_or_the_other_field_gives_a_proposal_its_time(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    for how in ("chip", "other"):
        when, _ = await _chip_case(web, committed, scratch, "propose", how)
        message = await _scalar(
            committed,
            select(NegotiationMessage)
            .where(NegotiationMessage.request_id == scratch["request_id"])
            .order_by(NegotiationMessage.id.desc()),
        )
        assert message is not None and message.proposed_start == when, how


async def test_a_chip_or_the_other_field_gives_an_approval_its_time(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    when, _ = await _chip_case(web, committed, scratch, "approve", "chip")
    got = await _scalar(
        committed,
        select(BookingRequest.scheduled_start).where(BookingRequest.id == scratch["request_id"]),
    )
    assert got == when, "approved at the chip's time, not at the held slot's"


async def test_the_other_field_alone_gives_an_approval_its_time(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    when, _ = await _chip_case(web, committed, scratch, "approve", "other")
    got = await _scalar(
        committed,
        select(BookingRequest.scheduled_start).where(BookingRequest.id == scratch["request_id"]),
    )
    assert got == when


async def test_a_chip_or_the_other_field_gives_a_move_its_time(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    for days, how in ((4, "chip"), (5, "other")):
        when, _ = await _chip_case(web, committed, scratch, "reschedule", how, days)
        got = await _scalar(
            committed,
            select(BookingRequest.scheduled_start).where(
                BookingRequest.id == scratch["request_id"]
            ),
        )
        assert got == when, how
        # The next round moves it again: a different day from the one just booked.
        await committed.execute(
            update(BookingRequest)
            .where(BookingRequest.id == scratch["request_id"])
            .values(scheduled_start=datetime.now(UTC) + timedelta(days=9))
        )
        await committed.commit()


def test_a_hand_edited_flash_number_cannot_raise() -> None:
    huge = "9" * 5000
    for code in (
        f"blocked-{huge}",
        f"unblocked-{huge}",
        f"template-saved-{huge}",
        f"created-{huge}",
        f"translations-{huge}-{huge}",
        f"repeated-{huge}-1-1-1",
        "applied-1-1-1-" + huge,
    ):
        assert _flash(code) is not None  # shown as plain text at worst, never a 500
    assert _copy_sentence("Copied", f"{huge}-0-0-0") == ""


def test_the_no_block_flash_echoes_only_a_times_sentence() -> None:
    text, kind = _flash("no-block:Tue 6 Jun 10:00, 11:00; Wed 7 Jun 09:00 and 3 more") or ("", "")
    assert kind == "error" and "Tue 6 Jun 10:00, 11:00" in text
    text, kind = _flash("no-block:<b>call me on 555</b> https://evil.example") or ("", "")
    assert kind == "error" and "evil" not in text and "<b>" not in text
    assert "a booked or held slot is in that range" in text


async def test_the_flash_codes_over_http_never_500(web: TestClient) -> None:
    _sign_in(web)
    assert web.get("/admin?flash=blocked-" + "9" * 5000).status_code == 200
