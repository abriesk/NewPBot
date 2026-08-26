"""The maintenance page: config export/import and backup downloads (§12.2).

These go through the real HTTP surface, because the parts worth testing are the
adapter's: that a preview really writes nothing even though the route commits,
that a refusal is shown rather than half-applied, and that the download route
cannot be talked into serving a file the sidecar did not write.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import NullPool, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.channels.web.security import ADMIN_COOKIE, CSRF_COOKIE
from app.config import get_settings
from app.core.models import AuditLog, Practice
from app.main import create_app

ADMIN_USER = get_settings().admin_username
ADMIN_PASSWORD = get_settings().admin_password

VALID_DUMP = "psychobooking-2026-08-25.dump"


@pytest_asyncio.fixture
async def committed() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def restore_practice(committed: AsyncSession) -> AsyncIterator[None]:
    """An applied import is a real commit, so put the settings back afterwards."""
    from app.core.services.settings import MUTABLE_FIELDS

    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    before = {field: getattr(practice, field) for field in MUTABLE_FIELDS}

    yield

    await committed.rollback()
    committed.expunge_all()
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    for field, value in before.items():
        setattr(practice, field, value)
    await committed.commit()


@pytest.fixture
def backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the app at a scratch directory standing in for the mount."""
    directory = tmp_path / "backups"
    directory.mkdir()
    monkeypatch.setenv("BACKUP_PATH", str(directory))
    get_settings.cache_clear()
    try:
        yield directory
    finally:
        get_settings.cache_clear()


@pytest.fixture
def web(backups: Path) -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


def _csrf(client: TestClient) -> str:
    return client.cookies.get(CSRF_COOKIE, "")


def _sign_in(client: TestClient) -> None:
    client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={"csrf_token": _csrf(client), "username": ADMIN_USER, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, "admin sign-in failed"
    assert client.cookies.get(ADMIN_COOKIE)


def _export(client: TestClient) -> dict:
    response = client.get("/admin/maintenance/config/export")
    assert response.status_code == 200
    return json.loads(response.text)


def _import(client: TestClient, payload: dict | str, *, apply: str) -> object:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return client.post(
        "/admin/maintenance/config/import",
        data={"csrf_token": _csrf(client), "apply": apply},
        files={"file": ("config.json", body, "application/json")},
    )


# --- Access -----------------------------------------------------------------


def test_the_maintenance_page_needs_a_session(web: TestClient) -> None:
    for path in (
        "/admin/maintenance",
        "/admin/maintenance/config/export",
        f"/admin/maintenance/backups/{VALID_DUMP}",
    ):
        response = web.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"


def test_import_without_a_csrf_token_is_refused(web: TestClient) -> None:
    _sign_in(web)
    response = web.post(
        "/admin/maintenance/config/import",
        data={"csrf_token": "wrong", "apply": "1"},
        files={"file": ("config.json", "{}", "application/json")},
    )
    assert response.status_code == 403


# --- Configuration ----------------------------------------------------------


def test_the_export_downloads_as_a_named_json_file(web: TestClient) -> None:
    _sign_in(web)
    response = web.get("/admin/maintenance/config/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    assert "psychobooking-config-" in response.headers["content-disposition"]
    assert json.loads(response.text)["format"] == "psychobooking.config"


async def test_the_export_is_audited(web: TestClient, committed: AsyncSession) -> None:
    _sign_in(web)
    web.get("/admin/maintenance/config/export")

    await committed.rollback()
    action = (
        await committed.execute(
            select(AuditLog.action).order_by(AuditLog.id.desc()).limit(1)
        )
    ).scalar_one()
    assert action == "config.export"


async def test_a_preview_writes_nothing(web: TestClient, committed: AsyncSession) -> None:
    _sign_in(web)
    payload = _export(web)
    payload["practice"]["slot_hold_minutes"] = 37

    response = _import(web, payload, apply="0")

    assert response.status_code == 200
    assert "nothing was saved" in response.text

    await committed.rollback()
    committed.expunge_all()
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.slot_hold_minutes != 37


async def test_applying_writes_and_audits(web: TestClient, committed: AsyncSession) -> None:
    _sign_in(web)
    payload = _export(web)
    payload["practice"]["slot_hold_minutes"] = 37

    response = _import(web, payload, apply="1")
    assert response.status_code == 200
    assert "Imported" in response.text

    await committed.rollback()
    committed.expunge_all()
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.slot_hold_minutes == 37

    row = (
        await committed.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))
    ).scalar_one()
    assert row.action == "config.import"
    assert row.meta is not None and row.meta["applied"] is True
    # §16.7: counts, never content.
    assert "practice" in row.meta["sections"]


async def test_a_refused_file_changes_nothing_and_says_why(
    web: TestClient, committed: AsyncSession
) -> None:
    _sign_in(web)
    payload = _export(web)
    payload["practice"]["slot_hold_minutes"] = 41
    payload["translations"] = {"am": {"common.yes": "አዎ"}}

    response = _import(web, payload, apply="1")

    assert response.status_code == 400
    assert "not imported" in response.text
    assert "unknown language" in response.text

    await committed.rollback()
    committed.expunge_all()
    practice = (await committed.execute(select(Practice).limit(1))).scalar_one()
    assert practice.slot_hold_minutes != 41


def test_a_file_that_is_not_ours_is_refused(web: TestClient) -> None:
    _sign_in(web)
    response = _import(web, {"format": "something.else", "version": 1}, apply="1")

    assert response.status_code == 400
    assert "not imported" in response.text


def test_an_oversized_upload_is_refused_before_parsing(web: TestClient) -> None:
    """§17: the cap is enforced on what was read, not on Content-Length."""
    _sign_in(web)
    payload = json.dumps({"format": "psychobooking.config", "version": 1, "pad": "x" * 6_000_000})

    response = _import(web, payload, apply="1")

    assert response.status_code == 400
    assert "larger than 5 MB" in response.text


# --- Backups ----------------------------------------------------------------


def test_a_dump_is_listed_and_downloadable(web: TestClient, backups: Path) -> None:
    (backups / VALID_DUMP).write_bytes(b"PGDMP-pretend")
    _sign_in(web)

    page = web.get("/admin/maintenance")
    assert page.status_code == 200
    assert VALID_DUMP in page.text

    download = web.get(f"/admin/maintenance/backups/{VALID_DUMP}")
    assert download.status_code == 200
    assert download.content == b"PGDMP-pretend"
    assert "attachment" in download.headers["content-disposition"]


def test_an_in_progress_dump_is_neither_listed_nor_served(
    web: TestClient, backups: Path
) -> None:
    """§16.6: the sidecar writes under a temporary name and moves it into
    place, so a half-written dump must not match."""
    (backups / ".in-progress-42.dump").write_bytes(b"half")
    _sign_in(web)

    page = web.get("/admin/maintenance")
    assert "in-progress" not in page.text
    assert web.get("/admin/maintenance/backups/.in-progress-42.dump").status_code == 404


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "psychobooking-2026-08-25.dump.txt",
        "notes.txt",
        "psychobooking-2026-8-5.dump",
    ],
)
def test_the_download_route_serves_nothing_but_a_dump(web: TestClient, name: str) -> None:
    _sign_in(web)
    response = web.get(f"/admin/maintenance/backups/{name}", follow_redirects=False)
    assert response.status_code == 404


def test_an_empty_directory_is_not_an_error(web: TestClient) -> None:
    _sign_in(web)
    page = web.get("/admin/maintenance")

    assert page.status_code == 200
    assert "No backups yet" in page.text
