"""NP4b: the client site's warm, personal pages through the real ASGI app (IMPLEMENTATION.md §12.1,
§12.2, §15, §17).

Her content is hers: the photo, languages, bio, price and cancellation rule come from the admin and
are never invented -- with none of it the pages still read well and say nothing false.
"""

# ruff: noqa: F811
from __future__ import annotations

import io
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, select, update

from app.channels.web.security import client_session_value
from app.core.enums import Channel, Modality, RequestStatus
from app.core.models import (
    BookingRequest,
    Client,
    ContentBlock,
    ContentTopic,
    Practice,
    PracticePhoto,
    SessionType,
)
from app.core.services import content, photo
from app.main import create_app
from tests.e2e.test_admin_web import (  # noqa: F401
    _csrf,
    _sign_in,
    committed,
    restore_practice,
)

# Fixtures and helpers shared with the admin and booking tests.
from tests.e2e.test_web_booking import web_slot  # noqa: F401


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
async def no_photo(committed) -> AsyncIterator[None]:
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
    assert response.status_code == 303 and "Photo%20saved" in response.headers["location"]

    page = web.get("/admin/settings")
    assert "/media/photo?v=" in page.text
    version = page.text.split("/media/photo?v=")[1].split('"')[0]

    # Public: no sign-in needed, as the client site shows it.
    web.cookies.clear()
    served = web.get(f"/media/photo?v={version}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert "immutable" in served.headers["cache-control"]
    assert len(served.content) < 200_000
    assert max(Image.open(io.BytesIO(served.content)).size) == 640
    # Without the current version the copy is kept only briefly.
    assert "immutable" not in web.get("/media/photo").headers["cache-control"]
    # A browser that already has it is told so.
    again = web.get("/media/photo", headers={"If-None-Match": served.headers["etag"]})
    assert again.status_code == 304


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
async def test_bio_cancellation_and_privacy_are_content_topics_she_can_write(committed) -> None:
    codes = set((await committed.execute(select(ContentTopic.code))).scalars())
    assert {"about_me", "cancellation", "privacy"} <= codes
    hidden = (
        await committed.execute(
            select(ContentTopic.show_in_menu).where(ContentTopic.code == "about_me")
        )
    ).scalar_one()
    assert hidden is False  # not a menu entry: nothing to link to while it is empty


# --- item 2: the front page ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def empty_content(committed, no_photo) -> AsyncIterator[None]:
    """No photo, no bio, no languages, no price, no home text: what a new practice looks like."""
    topics = ("about_me", "cancellation", "privacy", "home")
    saved = (
        await committed.execute(
            select(
                SessionType.id, SessionType.price_amount_minor, SessionType.price_display_override
            )
        )
    ).all()
    await committed.execute(
        update(SessionType).values(price_amount_minor=None, price_display_override=None)
    )
    ids = select(ContentTopic.id).where(ContentTopic.code.in_(topics))
    held = (
        (await committed.execute(select(ContentBlock).where(ContentBlock.topic_id.in_(ids))))
        .scalars()
        .all()
    )
    for block in held:
        await committed.delete(block)
    await committed.commit()
    yield
    await committed.rollback()
    for row_id, amount, override in saved:
        await committed.execute(
            update(SessionType)
            .where(SessionType.id == row_id)
            .values(price_amount_minor=amount, price_display_override=override)
        )
    await committed.commit()


@pytest_asyncio.fixture
async def her_content(committed, empty_content) -> AsyncIterator[None]:
    """Everything she can supply, written the way the admin writes it."""
    topic = dict((await committed.execute(select(ContentTopic.code, ContentTopic.id))).all())
    await content.upsert_block(
        committed,
        topic_id=topic["about_me"],
        lang="en",
        position=0,
        body_md="I work with grief and change.",
    )
    await content.upsert_block(
        committed, topic_id=topic["about_me"], lang="en", position=1, body_md="A second paragraph."
    )
    await content.upsert_block(
        committed,
        topic_id=topic["home"],
        lang="en",
        position=0,
        body_md="Her free text on the front page.",
    )
    await committed.execute(
        update(SessionType)
        .where(SessionType.code == "individual")
        .values(price_amount_minor=15000, price_currency="AMD")
    )
    await committed.execute(
        update(Practice).values(languages="Русский, English", online_only=False)
    )
    await photo.set_photo(committed, _jpeg())
    await committed.commit()
    yield


@pytest.mark.asyncio
async def test_the_front_page_with_nothing_written_still_reads_well_and_invents_nothing(
    web: TestClient, empty_content: None
) -> None:
    page = web.get("/?lang=en")
    assert page.status_code == 200
    assert "/media/photo" not in page.text  # a plain illustration, not a stock face
    assert 'class="art art-portrait"' in page.text
    assert "What happens after you book" in page.text
    assert 'href="/book"' in page.text
    assert "Languages" not in page.text  # a fact she has not given is left out
    assert "AMD" not in page.text and "Sessions" in page.text
    assert "More about me" not in page.text
    # No placeholder text ships as content.
    assert "Lorem" not in page.text and "TODO" not in page.text


@pytest.mark.asyncio
async def test_the_front_page_shows_what_she_wrote_and_nothing_else(
    web: TestClient, her_content: None
) -> None:
    page = web.get("/?lang=en").text
    assert "/media/photo?v=" in page and "art-portrait" not in page
    assert "I work with grief and change." in page
    assert (
        "A second paragraph." not in page and 'href="/t/about_me"' in page
    )  # the rest is one click away
    assert "Her free text on the front page." in page
    assert "Русский, English" in page
    assert "15\u00a0000\u00a0AMD" in page
    assert "Online" in page and "In person" in page
    assert web.get("/t/about_me?lang=en").status_code == 200


@pytest.mark.asyncio
async def test_online_only_says_online_only(
    web: TestClient, empty_content: None, committed
) -> None:
    await committed.execute(update(Practice).values(online_only=True))
    await committed.commit()
    page = web.get("/?lang=en").text
    facts = page.split('<dl class="facts">')[1].split("</dl>")[0]
    assert "In person" not in facts


# --- item 3: the slot picker, without htmx -------------------------------------------------------


@pytest.mark.asyncio
async def test_book_renders_the_strip_and_the_chosen_days_times_on_the_server(
    web: TestClient, web_slot: int
) -> None:
    page = web.get("/book?lang=en&tz=Asia/Yerevan").text
    assert 'class="daystrip"' in page and 'href="/book?' in page  # days are plain links
    assert f'name="slot_id" value="{web_slot}"' in page  # a plain list of forms, no htmx needed
    assert "Times are shown in Yerevan (GMT+4)" in page
    assert "data-zone-change" in page and 'id="tz-field"' in page
    assert "<noscript>" in page  # the "show times" fallback
    # One session type's picker is not hidden by a missing second one.
    assert 'name="modality"' in page


@pytest.mark.asyncio
async def test_the_partial_takes_a_day_and_a_day_without_times_falls_back(
    web: TestClient, web_slot: int
) -> None:
    partial = web.get("/book/slots?modality=online&tz=Asia/Yerevan&day=1999-01-01")
    assert partial.status_code == 200 and f'value="{web_slot}"' in partial.text
    assert 'aria-current="true"' in partial.text


# --- item 4: what happens next, and privacy ------------------------------------------------------


@pytest.mark.asyncio
async def test_what_happens_next_is_on_details_and_waitlist_and_says_only_her_and_her_hours(
    web: TestClient, empty_content: None, committed
) -> None:
    hours = (await committed.execute(select(Practice.pending_expiry_hours))).scalar_one()
    waitlist = web.get("/waitlist?lang=en").text
    assert "Who reads what you write" in waitlist and "Only I do." in waitlist
    assert f"stays open for {hours} hours" in waitlist
    # Without her rule the default only says where to write; it names no fee or window.
    assert "Write to me on your request page" in waitlist
    assert 'href="/privacy"' in waitlist


@pytest.mark.asyncio
async def test_her_cancellation_block_replaces_the_default(
    web: TestClient, her_content: None, committed
) -> None:
    topic = (
        await committed.execute(select(ContentTopic.id).where(ContentTopic.code == "cancellation"))
    ).scalar_one()
    await content.upsert_block(
        committed, topic_id=topic, lang="en", position=0, body_md="Please tell me a day ahead."
    )
    await committed.commit()
    page = web.get("/waitlist?lang=en").text
    assert "Please tell me a day ahead." in page
    assert "Write to me on your request page" not in page


@pytest.mark.asyncio
async def test_the_privacy_page_is_linked_from_every_footer_and_has_a_reviewed_default(
    web: TestClient, empty_content: None, committed
) -> None:
    assert 'href="/privacy"' in web.get("/?lang=en").text
    page = web.get("/privacy?lang=en")
    months = (await committed.execute(select(Practice.retention_months))).scalar_one()
    assert page.status_code == 200
    assert "Who reads it: only me." in page.text and f"about {months} months" in page.text
    for lang in ("ru", "hy"):
        assert web.get(f"/privacy?lang={lang}").status_code == 200


@pytest.mark.asyncio
async def test_her_privacy_block_replaces_the_default(
    web: TestClient, empty_content: None, committed
) -> None:
    topic = (
        await committed.execute(select(ContentTopic.id).where(ContentTopic.code == "privacy"))
    ).scalar_one()
    await content.upsert_block(
        committed, topic_id=topic, lang="en", position=0, body_md="My own privacy words."
    )
    await committed.commit()
    page = web.get("/privacy?lang=en").text
    assert "My own privacy words." in page and "Who reads it: only me." not in page


# --- item 5: the status page and the calendar file -----------------------------------------------


@pytest_asyncio.fixture
async def confirmed_request(committed) -> AsyncIterator[tuple[Client, BookingRequest]]:
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    type_id = (await committed.execute(select(SessionType.id).limit(1))).scalar_one()
    client = Client(
        practice_id=practice.id, language="en", timezone="Asia/Yerevan", display_name="T"
    )
    committed.add(client)
    await committed.flush()
    row = BookingRequest(
        practice_id=practice.id,
        client_id=client.id,
        session_type_id=type_id,
        modality=Modality.online,
        status=RequestStatus.confirmed,
        source_channel=Channel.web,
        scheduled_start=datetime.now(UTC) + timedelta(days=5),
        scheduled_duration_min=50,
        confirmed_at=datetime.now(UTC),
        client_timezone="Asia/Yerevan",
    )
    committed.add(row)
    await committed.commit()
    yield client, row
    await committed.rollback()
    await committed.execute(delete(BookingRequest).where(BookingRequest.id == row.id))
    await committed.execute(delete(Client).where(Client.id == client.id))
    await committed.commit()


def test_the_ics_is_for_the_client_it_belongs_to_only(web: TestClient, confirmed_request) -> None:
    _client, row = confirmed_request
    assert web.get(f"/r/{row.uuid}/session.ics").status_code == 404  # not signed in


def test_a_confirmed_request_is_a_status_page_with_a_calendar_file(
    web: TestClient, confirmed_request
) -> None:
    client, row = confirmed_request
    web.cookies.set("pb_client", client_session_value(client.id))
    page = web.get(f"/r/{row.uuid}?lang=en").text
    assert "Your session is booked" in page
    assert 'class="timeline"' in page and 'aria-current="step"' in page
    assert f'href="/r/{row.uuid}/session.ics"' in page and "Add to calendar" in page

    ics = web.get(f"/r/{row.uuid}/session.ics")
    assert ics.status_code == 200
    assert ics.headers["content-type"].startswith("text/calendar")
    assert "attachment" in ics.headers["content-disposition"]
    assert "BEGIN:VEVENT" in ics.text and f"UID:{row.uuid}@" in ics.text
    assert "DTEND" in ics.text and "\r\n" in ics.text
    start = row.scheduled_start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    assert f"DTSTART:{start}" in ics.text
    end = (row.scheduled_start + timedelta(minutes=50)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    assert f"DTEND:{end}" in ics.text


@pytest.mark.asyncio
async def test_no_calendar_file_until_it_is_confirmed(
    web: TestClient, confirmed_request, committed
) -> None:
    client, row = confirmed_request
    await committed.execute(
        update(BookingRequest)
        .where(BookingRequest.id == row.id)
        .values(status=RequestStatus.expired)
    )
    await committed.commit()
    web.cookies.set("pb_client", client_session_value(client.id))
    assert web.get(f"/r/{row.uuid}/session.ics").status_code == 404
    page = web.get(f"/r/{row.uuid}?lang=en").text
    assert "This request was not answered in time" in page
    assert 'class="timeline"' not in page and "session.ics" not in page
