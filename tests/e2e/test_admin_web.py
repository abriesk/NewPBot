"""Admin web UI (IMPLEMENTATION.md §12.2, M7 acceptance).

The acceptance criterion is one sentence with five verbs: a therapist can,
*without touching the database*, change availability, create a week of slots,
edit a block, roll it back, and approve a request. `test_the_m7_acceptance_path`
does exactly those five things through the real HTTP surface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import NullPool, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.channels.web.admin import BAD_PRICE, _price_amount, _price_display
from app.channels.web.security import ADMIN_COOKIE, CSRF_COOKIE
from app.config import get_settings
from app.core.enums import Channel, Modality, RequestStatus, SlotStatus
from app.core.models import (
    AdminSession,
    BookingRequest,
    Client,
    ContentBlock,
    ContentBlockRevision,
    ContentTopic,
    Identity,
    OutboxMessage,
    Practice,
    Reminder,
    SessionType,
    Slot,
    Translation,
)
from app.core.services.translations import invalidate_cache
from app.main import create_app
from app.render.labels import session_type_name

# The seed hashes whatever ADMIN_USERNAME/ADMIN_PASSWORD the deployment was
# installed with (§20), so the test signs in with those rather than a literal.
ADMIN_USER = get_settings().admin_username
ADMIN_PASSWORD = get_settings().admin_password
CLIENT_TG = "700800900"


@pytest_asyncio.fixture
async def committed() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def restore_practice(committed: AsyncSession) -> AsyncIterator[None]:
    """Snapshot and restore every practice setting around each test.

    The settings form posts checkboxes, so anything the test leaves unchecked
    is switched off -- which is correct for a form (the real template renders
    them all) but would otherwise leak into every later test in the run. This
    is the second time shared-database state has bitten these suites; a
    snapshot is cheaper than remembering.
    """
    from app.core.services.settings import MUTABLE_FIELDS

    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    before = {field: getattr(practice, field) for field in MUTABLE_FIELDS}

    yield

    await _fresh(committed)
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    for field, value in before.items():
        setattr(practice, field, value)
    await committed.commit()


@pytest.fixture
def web() -> Iterator[TestClient]:
    # https, so the Secure session cookies are kept (§17).
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


def _csrf(client: TestClient) -> str:
    return client.cookies.get(CSRF_COOKIE, "")


async def _purge_content(session: AsyncSession) -> None:
    """Remove every block and revision for the topic these tests edit."""
    topic = (
        await session.execute(select(ContentTopic).where(ContentTopic.code == "work_terms"))
    ).scalar_one()
    block_ids = (
        (await session.execute(select(ContentBlock.id).where(ContentBlock.topic_id == topic.id)))
        .scalars()
        .all()
    )
    if block_ids:
        await session.execute(
            delete(ContentBlockRevision).where(ContentBlockRevision.block_id.in_(block_ids))
        )
        await session.execute(delete(ContentBlock).where(ContentBlock.id.in_(block_ids)))
        await session.commit()


async def _fresh(session: AsyncSession) -> None:
    """See what the app just committed on its own connection.

    `rollback` ends this session's transaction so the next read gets a new
    snapshot; `expunge_all` drops the identity map, so a row this session
    loaded earlier is fetched again rather than returned expired -- an expired
    attribute would lazy-load on access, outside the async context, which is
    what MissingGreenlet means.
    """
    await session.rollback()
    session.expunge_all()


def _sign_in(client: TestClient) -> None:
    client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={
            "csrf_token": _csrf(client),
            "username": ADMIN_USER,
            "password": ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, "admin sign-in failed"
    assert client.cookies.get(ADMIN_COOKIE)


@pytest_asyncio.fixture
async def scratch(committed: AsyncSession) -> AsyncIterator[dict[str, object]]:
    """A request to approve and a block to edit, committed so the app sees them.

    Everything is torn down afterwards: these tests commit like production
    rather than rolling back.
    """
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    session_type_id = (
        await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
    ).scalar_one()

    # Idempotent setup: a run that died before teardown must not block the next.
    stale = (
        (
            await committed.execute(
                select(Identity.client_id).where(Identity.external_id == CLIENT_TG)
            )
        )
        .scalars()
        .all()
    )
    if stale:
        await committed.execute(
            update(Slot)
            .where(
                Slot.held_by_request.in_(
                    select(BookingRequest.id).where(BookingRequest.client_id.in_(stale))
                )
                | Slot.booked_request.in_(
                    select(BookingRequest.id).where(BookingRequest.client_id.in_(stale))
                )
            )
            .values(
                status=SlotStatus.available,
                hold_expires_at=None,
                held_by_request=None,
                booked_request=None,
            )
        )
        await committed.execute(delete(OutboxMessage).where(OutboxMessage.client_id.in_(stale)))
        await committed.execute(delete(BookingRequest).where(BookingRequest.client_id.in_(stale)))
        await committed.execute(delete(Identity).where(Identity.client_id.in_(stale)))
        await committed.execute(delete(Client).where(Client.id.in_(stale)))
        await committed.commit()

    # Content blocks too: a leftover block would carry its version forward and
    # make "edit a block" assert against the wrong number.
    await _purge_content(committed)

    client = Client(practice_id=practice.id, language="ru", display_name="Test Client")
    committed.add(client)
    await committed.flush()
    committed.add(
        Identity(
            practice_id=practice.id,
            client_id=client.id,
            channel=Channel.telegram,
            external_id=CLIENT_TG,
            verified_at=datetime.now(UTC),
        )
    )

    slot = Slot(
        practice_id=practice.id,
        starts_at=datetime.now(UTC) + timedelta(days=14, microseconds=11),
        duration_min=60,
        status=SlotStatus.available,
    )
    committed.add(slot)
    await committed.flush()

    request = BookingRequest(
        practice_id=practice.id,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        status=RequestStatus.pending,
        source_channel=Channel.web,
        slot_id=slot.id,
        problem_text="something private",
        expires_at=datetime.now(UTC) + timedelta(hours=48),
    )
    committed.add(request)
    await committed.flush()
    # `uuid` is a server default (gen_random_uuid), which flush does not fetch.
    # Reading it after commit would trigger a lazy load outside the async
    # context -- which is what MissingGreenlet means.
    await committed.refresh(request)
    await committed.refresh(slot)
    await committed.commit()

    payload = {
        "request_uuid": str(request.uuid),
        "request_id": int(request.id),
        "slot_id": int(slot.id),
        "client_id": client.id,
        "session_type_id": int(session_type_id),
    }
    yield payload

    # Teardown, dependency order. A held slot must be released before the
    # request that owns it is deleted (§6.4's CHECK versus §6.5's SET NULL).
    await committed.rollback()
    await committed.execute(
        update(Slot)
        .where((Slot.held_by_request == request.id) | (Slot.booked_request == request.id))
        .values(
            status=SlotStatus.available,
            hold_expires_at=None,
            held_by_request=None,
            booked_request=None,
        )
    )
    await committed.execute(delete(Reminder).where(Reminder.request_id == request.id))
    await committed.execute(delete(OutboxMessage).where(OutboxMessage.request_id == request.id))
    await committed.execute(delete(OutboxMessage).where(OutboxMessage.client_id == client.id))
    await committed.execute(delete(BookingRequest).where(BookingRequest.id == request.id))
    await committed.execute(delete(Slot).where(Slot.id == slot.id))
    await committed.execute(delete(Slot).where(Slot.starts_at >= datetime(2028, 1, 1, tzinfo=UTC)))
    await committed.execute(delete(Identity).where(Identity.client_id == client.id))
    await committed.execute(delete(Client).where(Client.id == client.id))

    topic = (
        await committed.execute(select(ContentTopic).where(ContentTopic.code == "work_terms"))
    ).scalar_one()
    block_ids = (
        (await committed.execute(select(ContentBlock.id).where(ContentBlock.topic_id == topic.id)))
        .scalars()
        .all()
    )
    if block_ids:
        await committed.execute(
            delete(ContentBlockRevision).where(ContentBlockRevision.block_id.in_(block_ids))
        )
        await committed.execute(delete(ContentBlock).where(ContentBlock.id.in_(block_ids)))

    await committed.execute(delete(AdminSession))
    await committed.commit()


# --- The M7 acceptance path -------------------------------------------------


async def test_the_m7_acceptance_path(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    """§19 M7, verb by verb, entirely over HTTP."""
    _sign_in(web)

    # 1. Change availability.
    web.get("/admin/settings")
    response = web.post(
        "/admin/settings",
        data={
            "csrf_token": _csrf(web),
            "booking_mode": "slots",
            "name": "Practice",
            "timezone": "Asia/Yerevan",
            "default_language": "ru",
            "reminder_offsets_min": "1440, 60",
            # availability_on omitted: an unchecked box means off.
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    # A redirect alone proves nothing: the handler redirects to the login page
    # when the session is missing, and back here with a flash when a value is
    # refused. Both are 303.
    assert response.headers["location"].startswith("/admin/settings"), response.headers["location"]
    assert "rejected" not in response.headers["location"], response.headers["location"]
    await _fresh(committed)
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.availability_on is False

    # Put it back on, the same way.
    web.get("/admin/settings")
    web.post(
        "/admin/settings",
        data={
            "csrf_token": _csrf(web),
            "availability_on": "1",
            "booking_mode": "slots",
            "name": "Practice",
            "timezone": "Asia/Yerevan",
            "default_language": "ru",
            "reminder_offsets_min": "1440, 60",
        },
    )
    await _fresh(committed)
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.availability_on is True

    # 2. Create a week of slots.
    web.get("/admin/slots")
    monday = date(2028, 3, 6)
    response = web.post(
        "/admin/slots/bulk",
        data={
            "csrf_token": _csrf(web),
            "date_from": monday.isoformat(),
            "date_to": (monday + timedelta(days=6)).isoformat(),
            "times": "10:00, 11:00",
            # A list value becomes repeated fields, as the checkbox group does.
            "weekdays": ["0", "2", "4"],
            "duration_min": "60",
            "modality": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    await _fresh(committed)
    created = (
        (
            await committed.execute(
                select(Slot).where(Slot.starts_at >= datetime(2028, 1, 1, tzinfo=UTC))
            )
        )
        .scalars()
        .all()
    )
    # Three weekdays, two times each.
    assert len(created) == 6

    # 3. Edit a block.
    web.get("/admin/content")
    response = web.post(
        "/admin/content/blocks",
        data={
            "csrf_token": _csrf(web),
            "topic_code": "work_terms",
            "lang": "ru",
            "position": 0,
            "body_md": "**Первая** версия.",
            "is_published": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    web.get("/admin/content")
    web.post(
        "/admin/content/blocks",
        data={
            "csrf_token": _csrf(web),
            "topic_code": "work_terms",
            "lang": "ru",
            "position": 0,
            "body_md": "Испорченная вставка.",
            "is_published": "1",
        },
    )
    await _fresh(committed)
    block = (
        await committed.execute(
            select(ContentBlock).where(ContentBlock.lang == "ru", ContentBlock.position == 0)
        )
    ).scalar_one()
    assert block.body_md == "Испорченная вставка."
    assert block.version == 2
    # A plain int, not the ORM object: _fresh detaches everything it loaded.
    block_id = int(block.id)

    # 4. Roll it back.
    web.get(f"/admin/content/revisions/{block_id}")
    response = web.post(
        f"/admin/content/revisions/{block_id}/restore",
        data={"csrf_token": _csrf(web), "version": 1},
        follow_redirects=False,
    )
    assert response.status_code == 303
    await _fresh(committed)
    restored = (
        await committed.execute(select(ContentBlock).where(ContentBlock.id == block_id))
    ).scalar_one()
    assert restored.body_md == "**Первая** версия."

    # 5. Approve a request.
    uuid = scratch["request_uuid"]
    web.get(f"/admin/requests/{uuid}")
    response = web.post(
        f"/admin/requests/{uuid}/approve",
        data={"csrf_token": _csrf(web), "scheduled_start": "", "meeting_url": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    await _fresh(committed)
    approved = (
        await committed.execute(
            select(BookingRequest).where(BookingRequest.id == scratch["request_id"])
        )
    ).scalar_one()
    assert approved.status is RequestStatus.confirmed
    assert approved.scheduled_start is not None

    slot = (await committed.execute(select(Slot).where(Slot.id == scratch["slot_id"]))).scalar_one()
    assert slot.status is SlotStatus.booked

    # Confirming schedules reminders and queues notifications, through the
    # outbox rather than a direct send (hard rule 2).
    reminders = (
        (
            await committed.execute(
                select(Reminder).where(Reminder.request_id == scratch["request_id"])
            )
        )
        .scalars()
        .all()
    )
    assert {r.offset_min for r in reminders} == {1440, 60}
    rows = (
        (
            await committed.execute(
                select(OutboxMessage).where(OutboxMessage.request_id == scratch["request_id"])
            )
        )
        .scalars()
        .all()
    )
    assert any(r.intent_key == "request.confirmed.client" for r in rows)


# --- Authentication (§17) ---------------------------------------------------


def test_every_admin_page_requires_a_session(web: TestClient) -> None:
    for path in (
        "/admin/requests",
        "/admin/waitlist",
        "/admin/slots",
        "/admin/content",
        "/admin/translations",
        "/admin/settings",
        "/admin/session-types",
        "/admin/timezones",
        "/admin/delivery",
        "/admin/clients",
        "/admin/privacy",
    ):
        response = web.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/admin/login"), path

    # /admin is a convenience redirect, so it reaches the login page on the
    # second hop rather than the first.
    assert web.get("/admin").url.path == "/admin/login"


def test_the_login_page_itself_is_reachable(web: TestClient) -> None:
    assert web.get("/admin/login").status_code == 200


def test_a_wrong_password_is_refused(web: TestClient) -> None:
    web.get("/admin/login")
    response = web.post(
        "/admin/login",
        data={"csrf_token": _csrf(web), "username": ADMIN_USER, "password": "wrong"},
    )
    assert response.status_code == 401
    assert not web.cookies.get(ADMIN_COOKIE)


def test_an_unknown_user_is_refused_the_same_way(web: TestClient) -> None:
    """Which half was wrong is not the caller's business."""
    web.get("/admin/login")
    wrong_user = web.post(
        "/admin/login",
        data={"csrf_token": _csrf(web), "username": "nobody", "password": "whatever"},
    )
    web.get("/admin/login")
    wrong_password = web.post(
        "/admin/login",
        data={"csrf_token": _csrf(web), "username": ADMIN_USER, "password": "wrong"},
    )
    assert wrong_user.status_code == wrong_password.status_code == 401


def test_login_without_a_csrf_token_is_refused(web: TestClient) -> None:
    response = web.post("/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert response.status_code == 403


async def test_signing_in_again_revokes_the_previous_session(
    web: TestClient, committed: AsyncSession
) -> None:
    """§17: rotated on login."""
    _sign_in(web)
    first = web.cookies.get(ADMIN_COOKIE)

    # A second sign-in, on the same client rather than a nested TestClient:
    # each TestClient runs its own lifespan, and the second one exiting would
    # dispose the shared engine while the first is still using it.
    web.cookies.delete(ADMIN_COOKIE)
    _sign_in(web)
    second = web.cookies.get(ADMIN_COOKIE)
    assert first != second

    # The first cookie no longer opens anything.
    web.cookies.set(ADMIN_COOKIE, first or "")
    assert web.get("/admin/requests", follow_redirects=False).status_code == 303

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_signing_out_ends_the_session(web: TestClient, committed: AsyncSession) -> None:
    _sign_in(web)
    assert web.get("/admin/requests", follow_redirects=False).status_code == 200

    web.post("/admin/logout", data={"csrf_token": _csrf(web)}, follow_redirects=False)
    assert web.get("/admin/requests", follow_redirects=False).status_code == 303

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_only_the_hash_of_a_session_token_is_stored(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    raw = web.cookies.get(ADMIN_COOKIE)

    await _fresh(committed)
    stored = (await committed.execute(select(AdminSession.token_hash))).scalars().all()
    assert raw not in stored

    await committed.execute(delete(AdminSession))
    await committed.commit()


# --- CSRF on every mutating admin form (§17) --------------------------------


async def test_mutating_admin_posts_require_a_csrf_token(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    for path, payload in (
        ("/admin/settings", {"name": "x"}),
        ("/admin/translations", {"lang": "ru", "key": "common.skip", "value": "x"}),
        ("/admin/session-types", {"code": "individual"}),
        ("/admin/timezones", {"iana_name": "Europe/Lisbon", "display_name": "Lisbon"}),
        ("/admin/content/preview", {"body_md": "hello"}),
    ):
        response = web.post(path, data=payload)
        assert response.status_code == 403, f"{path} accepted a request with no token"

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


# --- Content preview (§12.2) ------------------------------------------------


async def test_the_preview_renders_all_three_channels_side_by_side(
    web: TestClient, committed: AsyncSession
) -> None:
    """§12.2 states this explicitly."""
    _sign_in(web)
    web.get("/admin/content")
    response = web.post(
        "/admin/content/preview",
        data={"csrf_token": _csrf(web), "body_md": "# Условия\n\n- один\n- два"},
    )
    assert response.status_code == 200
    body = response.text

    assert "Telegram" in body and "Web" in body and "Email" in body
    # Telegram: a heading becomes a bold line, bullets become dots (§11.2).
    assert "&lt;b&gt;Условия&lt;/b&gt;" in body or "<b>Условия</b>" in body
    assert "•" in body
    # Web: headings shifted one level down (§11.3).
    assert "<h2>Условия</h2>" in body

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_the_preview_reports_unsupported_markdown_rather_than_rendering_it(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    web.get("/admin/content")
    response = web.post(
        "/admin/content/preview",
        data={"csrf_token": _csrf(web), "body_md": "| a | b |\n|---|---|\n| 1 | 2 |"},
    )
    assert response.status_code == 200
    assert "table" in response.text.lower()

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_saving_unsupported_markdown_is_refused_at_save_time(
    web: TestClient, committed: AsyncSession
) -> None:
    """§11.1: in front of her, not at send time in front of a client."""
    _sign_in(web)
    web.get("/admin/content")
    response = web.post(
        "/admin/content/blocks",
        data={
            "csrf_token": _csrf(web),
            "topic_code": "work_terms",
            "lang": "ru",
            "position": 0,
            "body_md": "<div>raw html</div>",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "HTML" in response.headers["location"] or "flash" in response.headers["location"]

    await _fresh(committed)
    stored = (
        (
            await committed.execute(
                select(ContentBlock).where(ContentBlock.body_md == "<div>raw html</div>")
            )
        )
        .scalars()
        .all()
    )
    assert not stored

    await committed.execute(delete(AdminSession))
    await committed.commit()


# --- Privacy (hard rule 8) --------------------------------------------------


async def test_problem_text_appears_on_the_detail_page_only(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    """DESIGN.md §16: visible through the authenticated admin UI, and nowhere
    else -- not in a list, a log, an email, or audit meta."""
    _sign_in(web)

    listing = web.get("/admin/requests")
    assert listing.status_code == 200
    assert "something private" not in listing.text

    detail = web.get(f"/admin/requests/{scratch['request_uuid']}")
    assert detail.status_code == 200
    assert "something private" in detail.text


async def test_the_delivery_page_lists_outbox_state(
    web: TestClient, committed: AsyncSession
) -> None:
    """DESIGN.md §15: so "did she get my message?" is answerable."""
    _sign_in(web)
    response = web.get("/admin/delivery")
    assert response.status_code == 200
    assert "pending" in response.text and "dead" in response.text

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


# --- Translations (§12.2, §15) ----------------------------------------------


async def test_the_translations_page_is_grouped_and_shows_the_english_wording(
    web: TestClient, committed: AsyncSession
) -> None:
    """§15's catalogue is 158 keys; one alphabetical list of them is not a
    screen anyone can work in."""
    from app.channels.web.translation_groups import GROUPS

    _sign_in(web)
    page = web.get("/admin/translations", params={"lang": "ru"})

    assert page.status_code == 200
    for group in GROUPS:
        if group.slug == "admin":
            # DESIGN.md §11: ru carries no admin keys, so the box is not drawn.
            assert group.title not in page.text
        else:
            assert group.title in page.text, group.slug

    # The English wording sits beside the field, so she is not translating a
    # bare key name from memory.
    assert "What should I call you?" in page.text
    # And a key with no Russian row yet is offered for editing rather than
    # merely named.
    assert 'name="key" value="auth.sign_in"' in page.text

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_a_translation_still_saves_from_the_grouped_page(
    web: TestClient, committed: AsyncSession
) -> None:
    """The grouping is presentation; the form it wraps is the one that worked
    before.

    The wording saved has to *differ* from the seeded one, or the assertion
    passes whether or not the form did anything.
    """
    from app.core.services.translations import get_text, invalidate_cache
    from app.seed import load_locale_catalogue

    seeded = load_locale_catalogue()["ru"]["request.thread"]
    edited = f"{seeded} (edited)"

    _sign_in(web)
    web.get("/admin/translations", params={"lang": "ru"})
    response = web.post(
        "/admin/translations",
        data={
            "csrf_token": _csrf(web),
            "lang": "ru",
            "key": "request.thread",
            "value": edited,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    await committed.rollback()
    invalidate_cache()
    assert await get_text(committed, "ru", "request.thread") == edited

    # Restored, not deleted: `request.thread` is a seeded key (§20), so
    # removing the row leaves the database one short of the catalogue. That
    # fails the seed tests on the *next* run rather than this one, which is
    # about as unhelpful as a failure gets.
    await committed.execute(
        update(Translation)
        .where(Translation.lang == "ru", Translation.key == "request.thread")
        .values(value=seeded)
    )
    await committed.execute(delete(AdminSession))
    await committed.commit()
    invalidate_cache()


# --- Settings validation ----------------------------------------------------


async def test_settings_refuses_a_field_that_is_not_settable(
    web: TestClient, committed: AsyncSession
) -> None:
    """§4: anything not in MUTABLE_FIELDS is an environment variable or not
    settable at all."""
    from app.core.services.settings import MUTABLE_FIELDS

    assert "retention_months" in MUTABLE_FIELDS
    assert "database_url" not in MUTABLE_FIELDS
    assert "secret_key" not in MUTABLE_FIELDS

    _sign_in(web)
    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_a_timezone_must_be_an_iana_name(web: TestClient, committed: AsyncSession) -> None:
    """DESIGN.md §8: an offset string breaks at every DST transition."""
    _sign_in(web)
    web.get("/admin/timezones")
    response = web.post(
        "/admin/timezones",
        data={"csrf_token": _csrf(web), "iana_name": "UTC+3", "display_name": "Nope"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "not-an-iana-name" in response.headers["location"]

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


# --- The week schedule (§12.2, M13) -----------------------------------------

#: A client that belongs to these tests alone, so teardown can be exact.
SCHEDULE_TG = "700800901"

#: Written into every request the grid fixture makes. Hard rule 8 says it must
#: never reach this surface, so it has to be present to be worth asserting on.
PRIVATE = "a private matter, never on the grid"


def _day_sections(html: str) -> list[str]:
    """The seven day columns, each bounded by its own `</section>`.

    Bounded rather than split on the next opening tag: the unscheduled list
    follows the last column, and an unbounded final chunk would swallow it --
    making everything listed *beside* the grid look as though it were on it.
    """
    grid = html.split("<h2>Unscheduled")[0]
    return [chunk.split("</section>")[0] for chunk in grid.split('<section class="wday')[1:]]


def _columns(html: str) -> list[str]:
    """The day headings the grid rendered, in order."""
    return [
        section.split("</h3>")[0].split("<h3>")[-1].strip() for section in _day_sections(html)
    ]


def _column_holding(html: str, uuid: object) -> str | None:
    """Which day column carries a link to this request, by its heading."""
    for section in _day_sections(html):
        if str(uuid) in section:
            return section.split("</h3>")[0].split("<h3>")[-1].strip()
    return None


def _beside_the_grid(html: str) -> str:
    return html.split("<h2>Unscheduled")[-1]


@pytest_asyncio.fixture
async def schedule_week(committed: AsyncSession) -> AsyncIterator[dict[str, object]]:
    """One request per interesting status, at known local times this week.

    Placed through the practice's own clock rather than in UTC: the whole point
    of the view is that the therapist sees her own Wednesday afternoon, so the
    fixture has to speak the way she does.
    """
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    session_type_id = int(
        (
            await committed.execute(select(SessionType.id).order_by(SessionType.id).limit(1))
        ).scalar_one()
    )
    zone = ZoneInfo(practice.timezone)

    # Idempotent setup: a run that died before teardown must not block the next.
    stale = (
        (
            await committed.execute(
                select(Identity.client_id).where(Identity.external_id == SCHEDULE_TG)
            )
        )
        .scalars()
        .all()
    )
    if stale:
        await committed.execute(
            update(Slot)
            .where(
                Slot.held_by_request.in_(
                    select(BookingRequest.id).where(BookingRequest.client_id.in_(stale))
                )
            )
            .values(status=SlotStatus.available, hold_expires_at=None, held_by_request=None)
        )
        await committed.execute(delete(BookingRequest).where(BookingRequest.client_id.in_(stale)))
        await committed.execute(delete(Identity).where(Identity.client_id.in_(stale)))
        await committed.execute(delete(Client).where(Client.id.in_(stale)))
        await committed.commit()

    client = Client(practice_id=practice.id, language="ru", display_name="Grid Client")
    committed.add(client)
    await committed.flush()
    committed.add(
        Identity(
            practice_id=practice.id,
            client_id=client.id,
            channel=Channel.telegram,
            external_id=SCHEDULE_TG,
            verified_at=datetime.now(UTC),
        )
    )

    today = datetime.now(UTC).astimezone(zone).date()
    monday = today - timedelta(days=today.weekday())

    def instant(offset: int, hour: int) -> datetime:
        """A local wall-clock time this week, as the instant it really is."""
        return datetime.combine(
            monday + timedelta(days=offset),
            datetime.min.time().replace(hour=hour),
            tzinfo=zone,
        ).astimezone(UTC)

    def make(status: RequestStatus, *, at: datetime | None = None, wanted: str | None = None):
        request = BookingRequest(
            practice_id=practice.id,
            client_id=client.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            status=status,
            source_channel=Channel.web,
            scheduled_start=at,
            confirmed_at=datetime.now(UTC) if at is not None else None,
            desired_time_text=wanted,
            problem_text=PRIVATE,
        )
        committed.add(request)
        return request

    confirmed = make(RequestStatus.confirmed, at=instant(2, 14))  # Wed 14:00
    completed = make(RequestStatus.completed, at=instant(0, 10))  # Mon 10:00
    rejected = make(RequestStatus.rejected, at=instant(3, 11))  # Thu 11:00, must not show
    timeless = make(RequestStatus.pending, wanted="some evening next week?")

    # A negotiating request that is still on its slot: `admin_propose` keeps
    # `slot_id` when the proposal names the time the client already holds.
    talking = make(RequestStatus.negotiating)
    # A negotiating request that is not: proposing a *different* time releases
    # the slot (§7.1), and the proposed instant lives on the negotiation
    # message rather than on the request, so nothing places this one.
    countered = make(RequestStatus.negotiating)

    # A pending request holds a slot rather than a schedule: §7.1 sets
    # `scheduled_start` only at approval.
    slots = [
        Slot(
            practice_id=practice.id,
            starts_at=at,
            duration_min=60,
            status=SlotStatus.available,
        )
        for at in (instant(4, 9), instant(1, 16))  # Fri 09:00, Tue 16:00
    ]
    for row in slots:
        committed.add(row)
    await committed.flush()

    holding = make(RequestStatus.pending)
    await committed.flush()
    for request, slot in ((holding, slots[0]), (talking, slots[1])):
        request.slot_id = slot.id
        slot.status = SlotStatus.held
        slot.held_by_request = request.id
        slot.hold_expires_at = datetime.now(UTC) + timedelta(minutes=30)

    await committed.flush()
    for row in (confirmed, completed, rejected, timeless, holding, talking, countered):
        await committed.refresh(row)
    await committed.commit()

    yield {
        "monday": monday.isoformat(),
        "confirmed": str(confirmed.uuid),
        "completed": str(completed.uuid),
        "rejected": str(rejected.uuid),
        "timeless": str(timeless.uuid),
        "holding": str(holding.uuid),
        "talking": str(talking.uuid),
        "countered": str(countered.uuid),
        "client_id": client.id,
        "session_type_id": session_type_id,
        "practice_id": int(practice.id),
    }

    slot_ids = [int(row.id) for row in slots]
    await committed.rollback()
    await committed.execute(
        update(Slot)
        .where(Slot.id.in_(slot_ids))
        .values(status=SlotStatus.available, hold_expires_at=None, held_by_request=None)
    )
    await committed.execute(delete(BookingRequest).where(BookingRequest.client_id == client.id))
    await committed.execute(delete(Slot).where(Slot.id.in_(slot_ids)))
    await committed.execute(delete(Identity).where(Identity.client_id == client.id))
    await committed.execute(delete(Client).where(Client.id == client.id))
    await committed.execute(delete(AdminSession))
    await committed.commit()


def test_the_schedule_draws_seven_days_of_the_current_week(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    _sign_in(web)
    page = web.get("/admin/requests?view=grid")

    assert page.status_code == 200
    assert len(_columns(page.text)) == 7


def test_every_kind_of_live_request_with_a_time_is_on_the_grid(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    """Confirmed, completed and slot-holding requests all carry a time, so all
    three belong in a column -- and a rejected one belongs nowhere."""
    _sign_in(web)
    html = web.get("/admin/requests?view=grid").text

    assert _column_holding(html, schedule_week["confirmed"]) is not None
    assert _column_holding(html, schedule_week["completed"]) is not None
    assert _column_holding(html, schedule_week["holding"]) is not None
    assert str(schedule_week["rejected"]) not in html


def test_a_negotiation_still_sitting_on_its_slot_is_on_the_grid(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    """`admin_propose` keeps `slot_id` when the proposal names the time the
    client already holds, so this one has an instant to be placed by."""
    _sign_in(web)
    html = web.get("/admin/requests?view=grid").text

    assert _column_holding(html, schedule_week["talking"]) == _columns(html)[1]  # Tue


def test_a_countered_negotiation_falls_to_the_list_with_nothing_to_show(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    """Proposing a *different* time releases the slot (§7.1) and records the
    instant on the negotiation message, not on the request. Nothing places it,
    so it lands beside the grid -- and because it came through the slot path it
    has no `desired_time_text` either, leaving the row with only a name.

    This is the current, specified behaviour rather than a desirable one; it is
    asserted so that changing it has to be a decision.
    """
    _sign_in(web)
    html = web.get("/admin/requests?view=grid").text

    assert _column_holding(html, schedule_week["countered"]) is None
    assert str(schedule_week["countered"]) in _beside_the_grid(html)


def test_a_session_lands_on_the_weekday_it_is_actually_on(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    _sign_in(web)
    html = web.get("/admin/requests?view=grid").text
    columns = _columns(html)

    assert _column_holding(html, schedule_week["completed"]) == columns[0]  # Mon
    assert _column_holding(html, schedule_week["confirmed"]) == columns[2]  # Wed
    assert _column_holding(html, schedule_week["holding"]) == columns[4]  # Fri


def test_a_finished_session_still_appears_so_the_week_is_not_a_lie(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    """The worker sweeps confirmed to completed once the end passes (§14); a
    grid without them renders every past week empty."""
    _sign_in(web)

    assert str(schedule_week["completed"]) in web.get("/admin/requests?view=grid").text


def test_a_request_with_only_wording_is_listed_beside_the_grid(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    _sign_in(web)
    html = web.get("/admin/requests?view=grid").text

    assert _column_holding(html, schedule_week["timeless"]) is None
    beside = _beside_the_grid(html)
    assert str(schedule_week["timeless"]) in beside
    assert "some evening next week?" in beside


def test_start_moves_a_whole_week_at_a_time(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    _sign_in(web)
    monday = date.fromisoformat(str(schedule_week["monday"]))

    here = web.get(f"/admin/requests?view=grid&start={monday.isoformat()}").text
    ahead = web.get(
        f"/admin/requests?view=grid&start={(monday + timedelta(days=7)).isoformat()}"
    ).text

    assert str(schedule_week["confirmed"]) in here
    assert str(schedule_week["confirmed"]) not in ahead
    assert len(_columns(ahead)) == 7


def test_a_date_mid_week_is_snapped_back_to_its_monday(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    """§12.2: a hand-edited URL lands on a whole week, not a seven-day window
    that starts on a Thursday."""
    _sign_in(web)
    monday = date.fromisoformat(str(schedule_week["monday"]))

    from_monday = web.get(f"/admin/requests?view=grid&start={monday.isoformat()}").text
    from_thursday = web.get(
        f"/admin/requests?view=grid&start={(monday + timedelta(days=3)).isoformat()}"
    ).text

    assert _columns(from_monday) == _columns(from_thursday)


def test_a_start_that_is_not_a_date_falls_back_to_this_week(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    """The parameter is navigation, not input: nonsense lands somewhere useful."""
    _sign_in(web)

    nonsense = web.get("/admin/requests?view=grid&start=next-tuesday-ish")

    assert nonsense.status_code == 200
    assert _columns(nonsense.text) == _columns(web.get("/admin/requests?view=grid").text)


def test_an_unknown_view_is_the_list_rather_than_an_error(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    _sign_in(web)

    page = web.get("/admin/requests?view=gantt")

    assert page.status_code == 200
    assert "<table>" in page.text


def test_the_schedule_never_renders_problem_text(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    """Hard rule 8, guarded on a new surface that renders request data."""
    _sign_in(web)

    for url in ("/admin/requests?view=grid", "/admin/requests"):
        assert PRIVATE not in web.get(url).text


def test_the_toggle_leaves_the_list_view_exactly_as_it_was(
    web: TestClient, schedule_week: dict[str, object]
) -> None:
    _sign_in(web)

    default = web.get("/admin/requests").text
    explicit = web.get("/admin/requests?view=list").text
    filtered = web.get("/admin/requests?status=confirmed")

    assert "<table>" in default
    assert "<table>" in explicit
    assert 'class="filters"' in default
    assert filtered.status_code == 200
    assert str(schedule_week["confirmed"]) in filtered.text
    # A second view of the same route, reachable from the first.
    assert 'href="/admin/requests?view=grid"' in default


async def test_a_week_that_loses_an_hour_still_has_seven_days(
    web: TestClient, committed: AsyncSession, schedule_week: dict[str, object]
) -> None:
    """A week containing a DST transition is not 168 hours long.

    Placing entries by arithmetic on the week's first instant walks a day's
    worth of them across the boundary; placing them by local wall-clock date
    does not. Europe/Berlin falls back on Sunday 25 October 2026.
    """
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    practice.timezone = "Europe/Berlin"
    request = BookingRequest(
        practice_id=schedule_week["practice_id"],
        client_id=schedule_week["client_id"],
        session_type_id=schedule_week["session_type_id"],
        modality=Modality.online,
        status=RequestStatus.confirmed,
        source_channel=Channel.web,
        # 10:00 on the fall-back Sunday, in her clock: 09:00 UTC, because the
        # zone is back on UTC+1 by then.
        scheduled_start=datetime(2026, 10, 25, 10, 0, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
            UTC
        ),
        confirmed_at=datetime.now(UTC),
    )
    committed.add(request)
    await committed.flush()
    await committed.refresh(request)
    await committed.commit()

    _sign_in(web)
    html = web.get("/admin/requests?view=grid&start=2026-10-19").text

    columns = _columns(html)
    assert len(columns) == 7
    assert _column_holding(html, request.uuid) == columns[6], "the fall-back Sunday"
    # Her clock, not the stored instant: 10:00 local is 09:00 UTC that day.
    assert "10:00" in _day_sections(html)[6]
    assert "09:00" not in _day_sections(html)[6]


# --- The requests list scales with the table, not the rows ------------------


async def _count_queries(db: AsyncSession, work: object) -> int:
    """How many statements one coroutine issues on this session."""
    from sqlalchemy import event

    connection = (await db.connection()).sync_connection
    assert connection is not None
    seen = 0

    def counter(*_args: object, **_kwargs: object) -> None:
        nonlocal seen
        seen += 1

    event.listen(connection, "before_cursor_execute", counter)
    try:
        await work  # type: ignore[misc]
    finally:
        event.remove(connection, "before_cursor_execute", counter)
    return seen


async def test_the_requests_list_does_not_query_per_row(
    db: AsyncSession, client: Client, session_type_id: int
) -> None:
    """`_summary` looks the practice and the session type up itself, which is
    right for one request and wrong for two hundred: the list route was issuing
    two queries per row to render a table whose lookups are all identical.
    """
    from app.channels.web.admin import _summaries

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    requests = []
    for _ in range(6):
        booking_request = BookingRequest(
            practice_id=practice.id,
            client_id=client.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            status=RequestStatus.pending,
            source_channel=Channel.web,
        )
        db.add(booking_request)
        requests.append(booking_request)
    await db.flush()

    one = await _count_queries(db, _summaries(db, requests[:1]))
    six = await _count_queries(db, _summaries(db, requests))

    assert six == one, f"{six} queries for six rows against {one} for one"
    assert one <= 3, f"{one} queries to render a single row"


# --- Naming and reaching the person behind a request (§12.2) ----------------


async def test_a_request_with_no_name_of_its_own_shows_the_client_s(
    db: AsyncSession, client: Client, session_type_id: int
) -> None:
    """A web booking carries a name only if the client typed one that time. The
    client behind it may still have one, and a row the therapist cannot
    attribute to anybody is not much of a row."""
    from app.channels.web.admin import _summaries

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    client.display_name = "Anna"
    anonymous = BookingRequest(
        practice_id=practice.id,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        status=RequestStatus.pending,
        source_channel=Channel.web,
    )
    named = BookingRequest(
        practice_id=practice.id,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        status=RequestStatus.pending,
        source_channel=Channel.web,
        display_name="Anna B",
    )
    db.add_all([anonymous, named])
    await db.flush()

    rows = await _summaries(db, [anonymous, named])

    assert rows[0]["name"] == "Anna", "falls back to the client"
    assert rows[1]["name"] == "Anna B", "the request's own name still wins"


async def test_identities_say_whether_an_address_is_verified() -> None:
    """§13.3 delivers nothing to an unverified address, so a therapist waiting
    on a reply needs to see that here rather than deduce it from the delivery
    log."""
    from types import SimpleNamespace

    from app.channels.web.admin import _identity_contacts

    contacts = _identity_contacts(
        [
            SimpleNamespace(channel=Channel.email, external_id="a@b.test", verified_at=None),
            SimpleNamespace(
                channel=Channel.email, external_id="c@d.test", verified_at=datetime.now(UTC)
            ),
            SimpleNamespace(channel=Channel.telegram, external_id="100200300", verified_at=None),
        ]
    )

    assert [c.label for c in contacts] == [
        "email: a@b.test (unverified)",
        "email: c@d.test (verified)",
        # Telegram vouches for the id, so verified state is not a question there.
        "telegram: 100200300",
    ]


async def test_a_contact_is_something_to_click_rather_than_copy() -> None:
    """Interim, until the UI pass: answering somebody should not begin with
    selecting an address. An identity with no way of being opened stays plain
    text rather than becoming a link that goes nowhere."""
    from types import SimpleNamespace

    from app.channels.web.admin import _identity_contacts

    contacts = _identity_contacts(
        [
            SimpleNamespace(channel=Channel.email, external_id="a@b.test", verified_at=None),
            SimpleNamespace(channel=Channel.telegram, external_id="100200300", verified_at=None),
            SimpleNamespace(channel=Channel.web, external_id="session-only", verified_at=None),
        ]
    )

    assert [c.href for c in contacts] == [
        "mailto:a@b.test",
        # Resolves only where her own Telegram client knows the person; where it
        # does not, this reads as the id it always was.
        "tg://user?id=100200300",
        None,
    ]


async def test_the_list_says_how_to_reach_the_client(
    db: AsyncSession, client: Client, session_type_id: int
) -> None:
    """§12.2: the list is where she decides what to open, so a row she cannot
    answer from should say so there rather than one click later. Both identities
    appear when both exist, Telegram first, because §13.3 tries Telegram first.
    """
    from app.channels.web.admin import _summaries

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    db.add(
        Identity(
            practice_id=practice.id,
            client_id=client.id,
            channel=Channel.email,
            external_id="anna@example.test",
            verified_at=None,
        )
    )
    booking_request = BookingRequest(
        practice_id=practice.id,
        client_id=client.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        status=RequestStatus.pending,
        source_channel=Channel.web,
    )
    db.add(booking_request)
    await db.flush()

    rows = await _summaries(db, [booking_request])

    assert [c.label for c in rows[0]["contact"]] == [
        # The `client` fixture is Telegram-identified; the address above is not
        # verified, and §13.3 would deliver nothing to it.
        "telegram: 100200300",
        "email: anna@example.test (unverified)",
    ]


async def test_a_client_with_nothing_left_to_reach_them_by_shows_nothing(
    db: AsyncSession, session_type_id: int
) -> None:
    """An erased client keeps their bookings and loses every identity (§16), so
    the join has a row and no channel on it. That must read as "no contact",
    not as a missing key or a crash."""
    from app.channels.web.admin import _summaries

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    erased = Client(practice_id=practice.id, language="ru")
    db.add(erased)
    await db.flush()
    booking_request = BookingRequest(
        practice_id=practice.id,
        client_id=erased.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        status=RequestStatus.pending,
        source_channel=Channel.web,
    )
    db.add(booking_request)
    await db.flush()

    rows = await _summaries(db, [booking_request])

    assert rows[0]["contact"] == ()
    assert rows[0]["name"] == ""


# --- Offering only what §7.1 allows (§12.2, §13.2) --------------------------


def _action_form(html: str, uuid: object, action: str) -> str:
    """The one form on the request page that posts `action`."""
    marker = f'action="/admin/requests/{uuid}/{action}"'
    assert marker in html, f"no {action} form on the page"
    return html.split(marker, 1)[1].split("</form>", 1)[0]


def test_the_page_knows_about_every_admin_transition_the_core_has() -> None:
    """A transition added to §7.1 that this page has never heard of is a control
    it can only render as always-available, which is the fault being fixed."""
    from app.channels.web.admin import UNAVAILABLE_BECAUSE
    from app.core.services import booking

    in_the_table = {
        event
        for events in booking.ALLOWED.values()
        for event in events
        if event.startswith("admin_")
    }
    assert in_the_table == set(UNAVAILABLE_BECAUSE)


def test_each_status_offers_what_7_1_allows_and_greys_the_rest() -> None:
    """§7.1's table, spelled out per status, so a change to either side has to
    be made deliberately rather than discovered by a therapist."""
    from app.channels.web.admin import UNAVAILABLE_BECAUSE
    from app.core.services import booking

    expected = {
        RequestStatus.pending: {"admin_approve", "admin_propose", "admin_reject"},
        # A client who counters with a time has said what suits them, so
        # agreeing is an approval -- but nothing here is confirmed yet, and
        # cancel is for a confirmed session. This is the reported case.
        RequestStatus.negotiating: {"admin_approve", "admin_propose", "admin_reject"},
        # Moving it keeps the booking; cancelling ends it. Both, and only
        # from here (§7.1).
        RequestStatus.confirmed: {"admin_cancel", "admin_reschedule"},
        RequestStatus.rejected: set(),
        RequestStatus.expired: set(),
        RequestStatus.cancelled: set(),
        RequestStatus.completed: set(),
    }

    for status, offered in expected.items():
        greyed = {e for e in UNAVAILABLE_BECAUSE if e not in booking.ALLOWED[status]}
        assert set(UNAVAILABLE_BECAUSE) - greyed == offered, status


async def test_cancel_on_a_negotiation_is_greyed_with_its_reason(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    """The reported fault. Pressing Cancel on a `negotiating` request could only
    ever end in `InvalidTransition`, and the page said "refused" -- which
    explains nothing to somebody who has just been told that the obvious way to
    call a session off does not work."""
    _sign_in(web)
    await committed.execute(
        update(BookingRequest)
        .where(BookingRequest.id == scratch["request_id"])
        .values(status=RequestStatus.negotiating)
    )
    await committed.commit()

    page = web.get(f"/admin/requests/{scratch['request_uuid']}")
    assert page.status_code == 200

    cancel = _action_form(page.text, scratch["request_uuid"], "cancel")
    assert "Only a confirmed session can be cancelled" in cancel
    assert "<button" not in cancel, "a greyed action must not still be pressable"

    # Reject is the verb that fits a request which was never confirmed, and §7.1
    # allows it from here -- so it keeps its button.
    reject = _action_form(page.text, scratch["request_uuid"], "reject")
    assert "<button" in reject
    assert "Only a request that is still open can be rejected" not in reject

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


# --- The practice's people, and §16's paperwork (§12.2) ---------------------


async def test_the_clients_list_leaves_out_everybody_who_never_booked(
    db: AsyncSession, session_type_id: int
) -> None:
    """The reported fault. `/admin/clients` listed every `client` row, which is
    the right population for a data-subject request and the wrong one for a view
    of the practice's clients: most of it was people who asked for a magic link
    and stopped."""
    from app.core.services.clients import list_clients_for_admin

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    booked = Client(practice_id=practice.id, language="ru", display_name="Booked")
    curious = Client(practice_id=practice.id, language="ru", display_name="Never booked")
    db.add_all([booked, curious])
    await db.flush()
    db.add(
        BookingRequest(
            practice_id=practice.id,
            client_id=booked.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            status=RequestStatus.pending,
            source_channel=Channel.web,
        )
    )
    await db.flush()

    listed = {row.client_id for row in await list_clients_for_admin(db)}

    assert booked.id in listed
    assert curious.id not in listed


async def test_an_erased_client_is_not_listed_but_still_has_a_page(
    db: AsyncSession, session_type_id: int
) -> None:
    """§16: they asked to be forgotten and their bookings stay for the
    practice's statistics. Naming them in the day-to-day list is against the
    point of having honoured it -- but a booking elsewhere still links to them,
    so the page itself must resolve."""
    from app.core.services.clients import list_clients_for_admin

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    erased = Client(
        practice_id=practice.id,
        language="ru",
        display_name=None,
        erased_at=datetime.now(UTC),
    )
    db.add(erased)
    await db.flush()
    db.add(
        BookingRequest(
            practice_id=practice.id,
            client_id=erased.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            status=RequestStatus.completed,
            source_channel=Channel.web,
        )
    )
    await db.flush()

    assert erased.id not in {row.client_id for row in await list_clients_for_admin(db)}


async def test_a_client_row_counts_their_bookings_and_finds_both_ends_of_them(
    db: AsyncSession, client: Client, session_type_id: int
) -> None:
    """The point of the page: one person, everything they have booked, without
    a status filter and a squint."""
    from app.core.services.clients import list_clients_for_admin

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    past = datetime.now(UTC) - timedelta(days=30)
    ahead = datetime.now(UTC) + timedelta(days=14)

    for status, when in (
        (RequestStatus.completed, past),
        (RequestStatus.confirmed, ahead),
        # Neither end: pending has no `scheduled_start` yet, but it is a
        # request and it counts.
        (RequestStatus.pending, None),
    ):
        db.add(
            BookingRequest(
                practice_id=practice.id,
                client_id=client.id,
                session_type_id=session_type_id,
                modality=Modality.online,
                status=status,
                source_channel=Channel.web,
                scheduled_start=when,
                # §6.5: a confirmed row must carry both, and the check
                # constraint is right to insist.
                confirmed_at=datetime.now(UTC)
                if status is RequestStatus.confirmed
                else None,
            )
        )
    await db.flush()

    row = next(r for r in await list_clients_for_admin(db) if r.client_id == client.id)

    assert row.requests == 3
    assert row.last_session == past
    assert row.next_session == ahead


async def test_a_future_session_that_is_not_confirmed_is_not_a_next_session(
    db: AsyncSession, client: Client, session_type_id: int
) -> None:
    """A cancelled or rejected booking is not something she is about to sit
    down to, and a page that said otherwise would be worse than no page."""
    from app.core.services.clients import list_clients_for_admin

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    db.add(
        BookingRequest(
            practice_id=practice.id,
            client_id=client.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            status=RequestStatus.cancelled,
            source_channel=Channel.web,
            scheduled_start=datetime.now(UTC) + timedelta(days=3),
        )
    )
    await db.flush()

    row = next(r for r in await list_clients_for_admin(db) if r.client_id == client.id)

    assert row.requests == 1
    assert row.next_session is None


async def test_one_person_with_two_identities_is_one_row(
    db: AsyncSession, client: Client, session_type_id: int
) -> None:
    """A `client` row is already the grouping of somebody's Telegram and email
    identities (§8), which is what was being asked for. Grouping by identity
    would split a linked person back into two."""
    from app.core.services.clients import link_identity, list_clients_for_admin

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    await link_identity(db, client.id, Channel.email, "anna@example.test", verified=True)
    db.add(
        BookingRequest(
            practice_id=practice.id,
            client_id=client.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            status=RequestStatus.pending,
            source_channel=Channel.web,
        )
    )
    await db.flush()

    rows = [r for r in await list_clients_for_admin(db) if r.client_id == client.id]

    assert len(rows) == 1, "one person, however many ways there are to reach them"
    assert {i.channel for i in rows[0].identities} == {Channel.telegram, Channel.email}


async def test_the_clients_list_does_not_query_per_row(
    db: AsyncSession, session_type_id: int
) -> None:
    """§12.2: a list whose query count depends on how many rows it draws is the
    fault the requests list already had once."""
    from app.core.services.clients import list_clients_for_admin

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    for index in range(6):
        person = Client(practice_id=practice.id, language="ru", display_name=f"P{index}")
        db.add(person)
        await db.flush()
        db.add(
            Identity(
                practice_id=practice.id,
                client_id=person.id,
                channel=Channel.telegram,
                external_id=f"9900{index}",
                verified_at=datetime.now(UTC),
            )
        )
        db.add(
            BookingRequest(
                practice_id=practice.id,
                client_id=person.id,
                session_type_id=session_type_id,
                modality=Modality.online,
                status=RequestStatus.pending,
                source_channel=Channel.web,
            )
        )
    await db.flush()

    one = await _count_queries(db, list_clients_for_admin(db, limit=1))
    six = await _count_queries(db, list_clients_for_admin(db, limit=6))

    assert six == one, f"{six} queries for six rows against {one} for one"
    # The practice lookup, the grouped read, and the identities.
    assert one <= 3, f"{one} queries to render a single row"


async def test_privacy_still_lists_everybody_including_the_erased(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    """§12.2: the one list that must not be filtered. A data-subject request is
    answerable only against the whole population."""
    _sign_in(web)

    page = web.get("/admin/privacy")
    assert page.status_code == 200
    assert str(scratch["client_id"]) in page.text
    # Export and erase live here and only here.
    assert f"/admin/clients/{scratch['client_id']}/export" in page.text
    assert "type erase" in page.text

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_the_clients_page_does_not_offer_erasure(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    """Irreversible, so it stays off the page opened for ordinary work -- the
    same reasoning that already makes it require the word typed out."""
    _sign_in(web)

    listing = web.get("/admin/clients")
    assert listing.status_code == 200
    assert "type erase" not in listing.text

    detail = web.get(f"/admin/clients/{scratch['client_id']}")
    assert detail.status_code == 200
    assert "type erase" not in detail.text
    # It says where to go instead.
    assert f"/admin/privacy#c-{scratch['client_id']}" in detail.text

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_the_request_count_is_a_way_into_the_requests(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    """A count is the thing somebody wants to click. The page behind it already
    existed -- only the name linked to it, so the number read as though there
    were nowhere to go."""
    _sign_in(web)

    listing = web.get("/admin/clients")
    assert listing.status_code == 200
    assert f'/admin/clients/{scratch["client_id"]}#requests' in listing.text

    detail = web.get(f"/admin/clients/{scratch['client_id']}")
    assert 'id="requests"' in detail.text, "the anchor the count lands on"

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_a_client_page_shows_their_bookings_but_never_what_they_wrote(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    """Hard rule 8: `problem_text` appears on the request page and nowhere else.
    A client's history is a list, and a list never carries it."""
    _sign_in(web)

    page = web.get(f"/admin/clients/{scratch['client_id']}")
    assert page.status_code == 200
    assert str(scratch["request_uuid"])[:8] in page.text
    assert "something private" not in page.text

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_an_unknown_client_page_is_a_404_not_a_crash(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)

    assert web.get("/admin/clients/not-a-uuid").status_code == 404
    assert web.get("/admin/clients/00000000-0000-0000-0000-000000000000").status_code == 404

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_deleting_a_slot_a_finished_request_asked_for_is_refused_not_a_500(
    web: TestClient, scratch: dict[str, object], committed: AsyncSession
) -> None:
    """Reported in use: delete answered with an internal server error.

    A request that ended released the slot but kept pointing at the time it
    asked for (§7.1), so the row was `available` -- the one status the page
    offers delete for -- and the foreign key refused the delete inside the
    flush. The page now withholds the button and the route answers the race
    with a redirect.
    """
    _sign_in(web)

    await _fresh(committed)
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    slot = Slot(
        practice_id=practice.id,
        starts_at=datetime.now(UTC) + timedelta(days=21),
        duration_min=60,
        status=SlotStatus.available,
    )
    committed.add(slot)
    await committed.flush()
    request = BookingRequest(
        practice_id=practice.id,
        client_id=scratch["client_id"],
        session_type_id=scratch["session_type_id"],
        modality=Modality.online,
        status=RequestStatus.rejected,
        source_channel=Channel.web,
        slot_id=slot.id,
    )
    committed.add(request)
    await committed.commit()
    slot_id = int(slot.id)

    page = web.get("/admin/slots")
    assert page.status_code == 200
    assert f'/admin/slots/{slot_id}/delete' not in page.text, "the button must not be offered"

    response = web.post(
        f"/admin/slots/{slot_id}/delete",
        data={"csrf_token": _csrf(web)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash=" in response.headers["location"]

    await _fresh(committed)
    survivor = (
        await committed.execute(select(Slot).where(Slot.id == slot_id))
    ).scalar_one_or_none()
    assert survivor is not None, "the slot the request points at has to stay"

    # Blocking is what the refusal points at, and the page offers it.
    blocked = web.post(
        f"/admin/slots/{slot_id}/block",
        data={"csrf_token": _csrf(web)},
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    await _fresh(committed)
    assert (
        await committed.execute(select(Slot.status).where(Slot.id == slot_id))
    ).scalar_one() is SlotStatus.blocked

    await committed.rollback()
    await committed.execute(delete(BookingRequest).where(BookingRequest.id == request.id))
    await committed.execute(delete(Slot).where(Slot.id == slot_id))
    await committed.execute(delete(AdminSession))
    await committed.commit()


# --- Session type prices ----------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "amount"),
    [
        ("15000", 15000),  # 15,000 dram, stored as written
        ("15 000", 15000),  # a thousands separator
        (f"15{chr(0xA0)}000", 15000),  # and the non-breaking one a paste carries
        ("0", 0),
        ("", None),
        ("   ", None),
    ],
)
def test_a_price_is_read_as_it_is_written(typed: str, amount: int | None) -> None:
    assert _price_amount(typed) == amount


@pytest.mark.parametrize(
    "typed",
    [
        "free",
        "15000.50",  # a price here is whole units, and rounding it silently is worse
        "15000,50",
        "15 000 AMD",
        "-500",
        "²",  # `str.isdigit` alone says this is a digit; `int` disagrees
    ],
)
def test_a_price_that_is_not_a_whole_number_is_refused_rather_than_guessed(typed: str) -> None:
    with pytest.raises(ValueError):
        _price_amount(typed)


def test_the_form_shows_the_price_it_would_accept_back() -> None:
    """The field round-trips: what it renders is what it parses."""
    assert _price_display(15000) == "15000"
    assert _price_display(None) == ""
    assert _price_amount(_price_display(15000)) == 15000


async def test_a_price_with_a_thousands_separator_is_saved_rather_than_500ing(
    web: TestClient, committed: AsyncSession
) -> None:
    """Reported in use: the price field answered with an internal server error.

    `int()` on whatever she typed is what did it, and the field was labelled
    "minor units", which is what made her type something else.
    """
    _sign_in(web)

    response = web.post(
        "/admin/session-types",
        data={
            "csrf_token": _csrf(web),
            "code": "individual",
            "duration_min": "60",
            "price_amount_minor": "15 000",
            "price_currency": "amd",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert BAD_PRICE not in response.headers["location"]

    await _fresh(committed)
    row = (
        await committed.execute(select(SessionType).where(SessionType.code == "individual"))
    ).scalar_one()
    assert row.price_amount_minor == 15000, "whole dram, as she wrote it"
    assert row.price_currency == "AMD", "ISO 4217 is upper case"

    # And the page offers it back in the shape she would type it again.
    page = web.get("/admin/session-types")
    assert 'value="15000"' in page.text

    row.price_amount_minor = None
    row.price_currency = None
    await committed.commit()
    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_a_price_that_is_not_a_number_says_so_instead_of_crashing(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)

    response = web.post(
        "/admin/session-types",
        data={
            "csrf_token": _csrf(web),
            "code": "individual",
            "duration_min": "60",
            "price_amount_minor": "15000.50",
            "price_currency": "",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash=" in response.headers["location"]

    await _fresh(committed)
    row = (
        await committed.execute(select(SessionType).where(SessionType.code == "individual"))
    ).scalar_one()
    assert row.price_amount_minor is None, "a refused save must change nothing"

    await committed.rollback()
    await committed.execute(delete(AdminSession))
    await committed.commit()


# --- Session type names -----------------------------------------------------


async def test_a_session_type_with_no_name_falls_back_to_its_code(
    committed: AsyncSession,
) -> None:
    """§15 returns the key itself when nothing is written for it.

    A type the therapist adds has a `booking.type.<code>` nobody has written,
    so clients were offered `booking.type.superme`.
    """
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    row = SessionType(practice_id=practice.id, code="unnamedtype", duration_min=60)
    committed.add(row)
    await committed.commit()

    try:
        assert await session_type_name(committed, "ru", "unnamedtype") == "unnamedtype"
    finally:
        await committed.execute(delete(SessionType).where(SessionType.id == row.id))
        await committed.commit()


async def test_naming_a_session_type_reaches_both_client_channels(
    web: TestClient, committed: AsyncSession
) -> None:
    """The name is a `translation` row, so it is one edit for every surface."""
    _sign_in(web)

    response = web.post(
        "/admin/session-types",
        data={
            "csrf_token": _csrf(web),
            "code": "supervision",
            "duration_min": "60",
            "price_amount_minor": "",
            "price_currency": "",
            "is_active": "1",
            "name_ru": "Супервизия",
            "name_hy": "Սուպերվիզիա",
            "name_en": "Supervision",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    await _fresh(committed)
    row = (
        await committed.execute(select(SessionType).where(SessionType.code == "supervision"))
    ).scalar_one()

    try:
        assert await session_type_name(committed, "ru", "supervision") == "Супервизия"
        assert await session_type_name(committed, "hy", "supervision") == "Սուպերվիզիա"
        assert await session_type_name(committed, "en", "supervision") == "Supervision"

        # Written through `set_text`, so it is also on the translations page --
        # `booking.` is the first group there.
        page = web.get("/admin/translations?lang=ru")
        assert "booking.type.supervision" in page.text

        # And the form offers back exactly what is stored, not a fallback: a
        # prefilled Armenian field holding the Russian name would be saved as
        # though somebody had translated it.
        types_page = web.get("/admin/session-types")
        assert 'value="Супервизия"' in types_page.text
    finally:
        await committed.execute(
            delete(Translation).where(Translation.key == "booking.type.supervision")
        )
        await committed.execute(delete(SessionType).where(SessionType.id == row.id))
        await committed.commit()
        invalidate_cache()

    await committed.execute(delete(AdminSession))
    await committed.commit()


async def test_a_language_left_empty_writes_no_row(
    web: TestClient, committed: AsyncSession
) -> None:
    """§15's fallback chain beats an empty row, which would render as nothing."""
    _sign_in(web)

    web.post(
        "/admin/session-types",
        data={
            "csrf_token": _csrf(web),
            "code": "halfnamed",
            "duration_min": "60",
            "price_amount_minor": "",
            "price_currency": "",
            "is_active": "1",
            "name_ru": "Только по-русски",
            "name_hy": "",
            "name_en": "",
        },
        follow_redirects=False,
    )

    await _fresh(committed)
    row = (
        await committed.execute(select(SessionType).where(SessionType.code == "halfnamed"))
    ).scalar_one()

    try:
        keys = (
            (
                await committed.execute(
                    select(Translation.lang).where(
                        Translation.key == "booking.type.halfnamed"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert set(keys) == {"ru"}
    finally:
        await committed.execute(
            delete(Translation).where(Translation.key == "booking.type.halfnamed")
        )
        await committed.execute(delete(SessionType).where(SessionType.id == row.id))
        await committed.commit()
        invalidate_cache()

    await committed.execute(delete(AdminSession))
    await committed.commit()


# --- The theme control (§12.2) ----------------------------------------------


def test_the_console_carries_a_three_state_theme_control(web: TestClient) -> None:
    """Two states cannot express "follow the system": a toggle can leave the
    default and never return to it.
    """
    _sign_in(web)
    page = web.get("/admin/settings").text

    assert 'id="theme"' in page
    for value in ('value="system"', 'value="light"', 'value="dark"'):
        assert value in page, f"the theme control offers no {value}"


def test_the_theme_control_is_on_settings_and_nowhere_else(web: TestClient) -> None:
    """It is a setting, so it lives on the settings page rather than in the
    frame of every page. The script that *applies* the choice still runs
    everywhere -- only the control is in one place.
    """
    _sign_in(web)

    for path in ("/admin/requests", "/admin/slots", "/admin/clients"):
        page = web.get(path).text
        assert 'id="theme"' not in page, f"{path} carries the control"
        assert "localStorage.getItem('admin-guide-theme')" in page, path


def test_the_theme_control_is_not_posted_with_the_settings_form(web: TestClient) -> None:
    """It writes to this browser and to no route. Inside the form it would be
    a field that Save appears to own and does not.
    """
    _sign_in(web)
    page = web.get("/admin/settings").text

    form = page[page.index('action="/admin/settings"') : page.index("</form>")]
    assert 'id="theme"' not in form


def test_the_theme_control_says_it_will_not_follow_her(web: TestClient) -> None:
    """Every other control on that page is a database setting that follows her
    between devices. The one that cannot has to say so.
    """
    _sign_in(web)
    page = web.get("/admin/settings").text

    assert "this browser only" in page


def test_the_theme_is_applied_before_the_page_is_painted(web: TestClient) -> None:
    """An attribute set after first paint is a flash of the wrong palette on
    every load, so the script that reads the choice runs in the head and is not
    deferred.
    """
    _sign_in(web)
    page = web.get("/admin/requests").text

    head = page[: page.index("</head>")]
    assert "localStorage.getItem('admin-guide-theme')" in head
    assert "dataset.theme" in head


def test_the_console_and_the_guide_share_one_theme_key() -> None:
    """Two keys would mean setting dark in the console and opening the manual
    light. The guide's toggle predates this control and keeps its own key; this
    is the test that notices if either one wanders off it.
    """
    from app.channels.web.help import HELP_LANGUAGES, help_guide

    for lang in HELP_LANGUAGES:
        assert "admin-guide-theme" in help_guide(lang), f"{lang} guide changed its key"


def test_signing_in_honours_a_theme_already_chosen(web: TestClient) -> None:
    """The sign-in page offers no control -- there is nothing to configure
    before signing in -- but logging out must not flash back to the wrong
    palette.
    """
    page = web.get("/admin/login").text

    assert "localStorage.getItem('admin-guide-theme')" in page
    assert 'id="theme"' not in page


def test_the_client_surface_is_left_on_the_system_preference(web: TestClient) -> None:
    """Clients are strangers on every kind of device. Nothing on their side
    writes `data-theme`, so `prefers-color-scheme` still decides there.
    """
    page = web.get("/").text

    assert "admin-guide-theme" not in page
    assert "dataset.theme" not in page
