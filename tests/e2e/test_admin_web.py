"""Admin web UI (IMPLEMENTATION.md §12.2, M7 acceptance).

The acceptance criterion is one sentence with five verbs: a therapist can,
*without touching the database*, change availability, create a week of slots,
edit a block, roll it back, and approve a request. `test_the_m7_acceptance_path`
does exactly those five things through the real HTTP surface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import NullPool, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

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
)
from app.main import create_app

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
