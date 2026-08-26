"""The dot and /admin/status (IMPLEMENTATION.md §12.2, §16.8).

The reader's half of the health signal. Its own job is small -- read one file,
never crash -- but the failure it has to survive is the interesting one: the
worker being dead is exactly when this page is opened, and it is also exactly
when the file it reads is missing or stale.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.channels.web.security import ADMIN_COOKIE, CSRF_COOKIE
from app.channels.web.status import STALE_AFTER, read_status
from app.config import get_settings
from app.core.enums import CheckState
from app.core.policies import now_utc
from app.main import create_app

ADMIN_USER = get_settings().admin_username
ADMIN_PASSWORD = get_settings().admin_password


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv("STATE_PATH", str(directory))
    get_settings.cache_clear()
    try:
        yield directory
    finally:
        get_settings.cache_clear()


@pytest.fixture
def web(state: Path) -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


def _sign_in(client: TestClient) -> None:
    client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={
            "csrf_token": client.cookies.get(CSRF_COOKIE, ""),
            "username": ADMIN_USER,
            "password": ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, "admin sign-in failed"
    assert client.cookies.get(ADMIN_COOKIE)


def _write(state: Path, *, age: timedelta = timedelta(), checks: list[dict] | None = None) -> None:
    payload = {
        "written_at": (now_utc() - age).isoformat(),
        "state": "ok",
        "version": 1,
        "checks": checks
        if checks is not None
        else [{"id": "outbox_dead", "state": "ok", "summary": "", "detail": "none"}],
    }
    payload["state"] = "ok"
    for check in payload["checks"]:
        if check["state"] == "fail":
            payload["state"] = "fail"
        elif check["state"] == "warn" and payload["state"] == "ok":
            payload["state"] = "warn"
    (state / "status.json").write_text(json.dumps(payload), encoding="utf-8")


# --- The reader -------------------------------------------------------------


def test_a_missing_file_reads_as_a_dead_worker(state: Path) -> None:
    reading = read_status()

    assert reading.state is CheckState.fail
    assert reading.checks[0]["id"] == "worker_alive"


def test_an_unparseable_file_reads_as_a_dead_worker(state: Path) -> None:
    (state / "status.json").write_text("{ not json", encoding="utf-8")

    assert read_status().state is CheckState.fail


def test_a_stale_file_reads_as_a_dead_worker(state: Path) -> None:
    """§16.9's one reader-computed check: the file's own age is the liveness
    signal, so nothing has to notice the worker died (DESIGN.md §22.3)."""
    _write(state, age=STALE_AFTER + timedelta(minutes=1))

    reading = read_status()

    assert reading.state is CheckState.fail
    alive = reading.checks[0]
    assert alive["id"] == "worker_alive"
    assert "stale after" in alive["detail"]


def test_a_fresh_file_is_trusted(state: Path) -> None:
    _write(state, age=timedelta(seconds=30))

    reading = read_status()

    assert reading.state is CheckState.ok
    assert reading.healthy
    assert reading.checks[0]["id"] == "worker_alive"
    assert reading.checks[0]["state"] == "ok"


def test_the_worst_check_decides_the_colour(state: Path) -> None:
    _write(
        state,
        checks=[
            {"id": "outbox_dead", "state": "ok", "summary": "", "detail": ""},
            {"id": "disk_space", "state": "warn", "summary": "low", "detail": "1 GB"},
        ],
    )
    assert read_status().colour == "amber"

    _write(
        state,
        checks=[
            {"id": "disk_space", "state": "warn", "summary": "low", "detail": "1 GB"},
            {"id": "outbox_dead", "state": "fail", "summary": "undelivered", "detail": "1"},
        ],
    )
    assert read_status().colour == "red"


# --- The page ---------------------------------------------------------------


def test_the_status_page_needs_a_session(web: TestClient) -> None:
    response = web.get("/admin/status", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_the_dot_appears_on_every_admin_page(web: TestClient, state: Path) -> None:
    _write(state)
    _sign_in(web)

    for path in ("/admin/requests", "/admin/settings", "/admin/maintenance"):
        page = web.get(path)
        assert page.status_code == 200, path
        assert 'class="dot dot-ok"' in page.text, path


def test_a_dead_worker_turns_the_dot_red_without_breaking_the_page(
    web: TestClient, state: Path
) -> None:
    """The page has to keep working precisely when the health file does not."""
    _write(state, age=STALE_AFTER + timedelta(minutes=5))
    _sign_in(web)

    page = web.get("/admin/requests")

    assert page.status_code == 200
    assert 'class="dot dot-fail"' in page.text


def test_the_status_page_says_what_to_do(web: TestClient, state: Path) -> None:
    _write(
        state,
        checks=[
            {
                "id": "outbox_dead",
                "state": "fail",
                "summary": "2 messages could not be delivered.",
                "detail": "outbox: 2 dead",
            }
        ],
    )
    _sign_in(web)

    page = web.get("/admin/status")

    assert page.status_code == 200
    assert "2 messages could not be delivered." in page.text
    assert "outbox: 2 dead" in page.text
    assert "Call whoever runs your server" in page.text


def test_a_healthy_status_page_says_there_is_nothing_to_do(
    web: TestClient, state: Path
) -> None:
    _write(state)
    _sign_in(web)

    page = web.get("/admin/status")

    assert "Nothing to do" in page.text


# --- Recording an unhandled request error (§6.9) -----------------------------


def test_an_unhandled_request_error_is_recorded_without_its_message(
    web: TestClient, state: Path
) -> None:
    """The failure that otherwise leaves no trace at all: the client sees a 500
    and leaves (DESIGN.md §22.1). What is stored is the class and where it was
    raised -- never the message, which can carry anything a client typed."""
    import asyncio

    from sqlalchemy import NullPool, select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.models import ErrorEvent

    app = web.app

    @app.get("/__boom", include_in_schema=False)
    async def boom() -> None:
        raise ValueError("problem text: lena@example.test said something private")

    with pytest.raises(ValueError):
        # TestClient re-raises server exceptions by default; the handler still
        # runs, which is what this asserts.
        web.get("/__boom")

    async def latest() -> ErrorEvent | None:
        # A dedicated engine: the application's belongs to the TestClient's
        # event loop, and asyncpg connections do not cross loops.
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                return (
                    await session.execute(
                        select(ErrorEvent).order_by(ErrorEvent.id.desc()).limit(1)
                    )
                ).scalar_one_or_none()
        finally:
            await engine.dispose()

    row = asyncio.run(latest())

    assert row is not None
    assert row.kind == "ValueError"
    assert row.source.value == "web"
    assert "lena@example.test" not in row.location
    assert "private" not in row.location
