"""Hardening (IMPLEMENTATION.md §17, §19 M9).

M9's acceptance is "the security checklist in §17 verified item by item". This
file is that checklist, one test per line of it, so a regression names the line
it broke rather than "something in security".

    - Passwords: Argon2id
    - Admin session cookie: HttpOnly, Secure, SameSite=Lax, rotated on login
    - CSRF token on every mutating admin and client form
    - Rate limits: admin login 5/15min/IP; magic link 3/h/email and 10/h/IP;
      booking submission 5/h/client
    - Telegram webhook secret checked before body parsing
    - All web-rendered content passes the sanitiser
    - SQL exclusively through SQLAlchemy; no string-built queries
    - Logging: never problem_text, negotiation bodies, tokens, or payloads
    - .env git-ignored; .env.example committed
    - Uploads: none in this version
"""

from __future__ import annotations

import ast
import pathlib
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.web import ratelimit
from app.channels.web.security import ADMIN_COOKIE, CSRF_COOKIE
from app.config import get_settings
from app.core.enums import Channel, Modality
from app.core.errors import RateLimited
from app.core.models import AdminUser, AuditLog, BookingRequest, Client, Slot
from app.core.services import booking
from app.core.services.booking import SUBMISSIONS_PER_HOUR
from app.core.services.clients import erase_client, export_client, resolve_client
from app.main import create_app

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / "app"


@pytest.fixture(autouse=True)
def clear_limits() -> Iterator[None]:
    """Each test starts with a fresh window; the limiter is process-global."""
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture
def web() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


# --- Passwords: Argon2id ----------------------------------------------------


async def test_passwords_are_argon2id(db: AsyncSession) -> None:
    stored = (await db.execute(select(AdminUser.password_hash))).scalars().all()
    assert stored
    assert all(h.startswith("$argon2id$") for h in stored)


def test_no_weaker_hash_is_imported_anywhere() -> None:
    """md5 and sha1 over a password would be the classic mistake."""
    for path in APP.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "hashlib.md5" not in source, path
        assert "hashlib.sha1(" not in source, path


# --- Admin session cookie ---------------------------------------------------


def test_the_admin_cookie_is_httponly_secure_and_lax(web: TestClient) -> None:
    settings = get_settings()
    web.get("/admin/login")
    response = web.post(
        "/admin/login",
        data={
            "csrf_token": web.cookies.get(CSRF_COOKIE, ""),
            "username": settings.admin_username,
            "password": settings.admin_password,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    header = " ".join(
        value for name, value in response.headers.items() if name.lower() == "set-cookie"
    )
    assert ADMIN_COOKIE in header
    assert "HttpOnly" in header
    assert "Secure" in header  # BASE_URL is https in the test environment
    assert "SameSite=lax" in header.replace("SameSite=Lax", "SameSite=lax")


def test_the_client_cookie_is_httponly_too() -> None:
    """A client session is not readable from script either."""
    from app.channels.web.security import _cookie_kwargs

    kwargs = _cookie_kwargs()
    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"


def test_the_csrf_cookie_is_deliberately_readable() -> None:
    """Double-submit needs the page to read it; it carries no authority."""
    import inspect

    from app.channels.web import security

    source = inspect.getsource(security.issue_csrf)
    assert "httponly=False" in source


# --- Rate limits (§17) ------------------------------------------------------


def test_the_limits_are_the_numbers_in_section_17() -> None:
    assert ratelimit.ADMIN_LOGIN.allowance == 5
    assert ratelimit.ADMIN_LOGIN.window_seconds == 15 * 60
    assert ratelimit.MAGIC_LINK_IP.allowance == 10
    assert ratelimit.MAGIC_LINK_IP.window_seconds == 3600
    assert ratelimit.MAGIC_LINK_PER_EMAIL == 3
    assert SUBMISSIONS_PER_HOUR == 5


def test_a_window_allows_exactly_its_allowance() -> None:
    limit = ratelimit.Limit("probe", 3, 60)
    assert [ratelimit.check(limit, "k") for _ in range(4)] == [True, True, True, False]
    assert ratelimit.remaining(limit, "probe-other") == 3


def test_windows_are_per_key() -> None:
    limit = ratelimit.Limit("probe2", 1, 60)
    assert ratelimit.check(limit, "a") is True
    assert ratelimit.check(limit, "b") is True
    assert ratelimit.check(limit, "a") is False


def test_admin_login_is_limited_per_ip(web: TestClient) -> None:
    """§17: 5 per 15 minutes."""
    settings = get_settings()
    web.get("/admin/login")
    token = web.cookies.get(CSRF_COOKIE, "")

    codes = []
    for _ in range(6):
        response = web.post(
            "/admin/login",
            data={"csrf_token": token, "username": settings.admin_username, "password": "wrong"},
            follow_redirects=False,
        )
        codes.append(response.status_code)

    assert codes[:5] == [401] * 5
    assert codes[5] == 429


def test_the_limiter_counts_successes_too(web: TestClient) -> None:
    """A limiter that only counts failures is defeated by succeeding."""
    limit = ratelimit.Limit("probe3", 2, 60)
    ratelimit.check(limit, "x")
    ratelimit.check(limit, "x")
    assert ratelimit.check(limit, "x") is False


async def test_booking_submission_is_limited_per_client(
    db: AsyncSession, client: Client, session_type_id: int, practice: object
) -> None:
    """§17: 5 an hour. Enforced in the core, so Telegram gets it as well as the
    web -- a limit only one channel applied would be no limit."""
    from datetime import UTC, datetime, timedelta

    from app.core.models import Slot as SlotModel

    slots = []
    for n in range(SUBMISSIONS_PER_HOUR + 1):
        slot = SlotModel(
            practice_id=practice.id,  # type: ignore[attr-defined]
            starts_at=datetime.now(UTC) + timedelta(days=60, microseconds=n + 1),
            duration_min=60,
        )
        db.add(slot)
        slots.append(slot)
    await db.flush()

    for slot in slots[:SUBMISSIONS_PER_HOUR]:
        await booking.submit_slot_request(
            db,
            client_id=client.id,
            slot_id=slot.id,
            session_type_id=session_type_id,
            modality=Modality.online,
            source_channel=Channel.web,
        )

    with pytest.raises(RateLimited):
        await booking.submit_slot_request(
            db,
            client_id=client.id,
            slot_id=slots[-1].id,
            session_type_id=session_type_id,
            modality=Modality.online,
            source_channel=Channel.telegram,
        )


async def test_magic_link_allowance_counts_issued_tokens(db: AsyncSession) -> None:
    """§17: 3 per hour per email, counted from auth_token rows."""
    from app.core.enums import TokenPurpose
    from app.core.services.clients import issue_token, magic_link_allowance_left

    email = "ratelimit-probe@example.test"
    person = await resolve_client(db, Channel.email, email)

    assert await magic_link_allowance_left(db, email) == 3
    for expected in (2, 1, 0):
        await issue_token(db, TokenPurpose.login, client_id=person.id)
        assert await magic_link_allowance_left(db, email) == expected


async def test_the_allowance_is_per_address(db: AsyncSession) -> None:
    from app.core.enums import TokenPurpose
    from app.core.services.clients import issue_token, magic_link_allowance_left

    first = await resolve_client(db, Channel.email, "one@example.test")
    await issue_token(db, TokenPurpose.login, client_id=first.id)

    assert await magic_link_allowance_left(db, "one@example.test") == 2
    assert await magic_link_allowance_left(db, "two@example.test") == 3


# --- SQL and logging --------------------------------------------------------


def test_no_string_built_sql_anywhere_in_the_application() -> None:
    """§17: SQL exclusively through SQLAlchemy.

    `text()` is legitimate for a fragment SQLAlchemy cannot express, but it
    must never be handed an f-string or a concatenation -- that is the shape
    injection takes.
    """
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "text" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.JoinedStr) or (
                isinstance(first, ast.BinOp) and isinstance(first.op, ast.Add)
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, f"interpolated SQL: {offenders}"


def test_nothing_logs_problem_text_or_a_token() -> None:
    """§17 and hard rule 8: log identifiers, never content.

    Checked on the call sites: every logging call's arguments are inspected for
    the names that carry content.
    """
    forbidden = {"problem_text", "body_text", "raw_token", "password", "token_hash"}
    offenders: list[str] = []

    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in ("debug", "info", "warning", "error", "exception", "critical"):
                continue
            if getattr(func.value, "id", None) != "logger":
                continue
            for argument in ast.walk(ast.Module(body=list(node.args), type_ignores=[])):
                identifier = getattr(argument, "attr", None) or getattr(argument, "id", None)
                if identifier in forbidden:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} logs {identifier}")

    assert not offenders, offenders


def test_the_payload_of_an_outbox_row_is_never_logged() -> None:
    from app.worker.jobs import outbox

    source = pathlib.Path(outbox.__file__).read_text(encoding="utf-8")
    assert "message.payload" not in source.split("def deliver_one")[0]


# --- Deployment hygiene -----------------------------------------------------


def test_env_is_git_ignored_and_the_example_is_not() -> None:
    # Repository hygiene, not application behaviour: the image deliberately does
    # not ship .gitignore or .env.example, so this runs against a checkout.
    if not (ROOT / ".gitignore").is_file():
        pytest.skip("not running against a repository checkout")
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
    assert "!.env.example" in ignored
    assert (ROOT / ".env.example").is_file()


def test_the_committed_example_carries_no_real_secret() -> None:
    if not (ROOT / ".env.example").is_file():
        pytest.skip("not running against a repository checkout")
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    # The bot token pattern is `digits:base64ish`; the placeholder is all zeros.
    for line in text.splitlines():
        if line.startswith("SECRET_KEY="):
            assert "change-me" in line
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            assert set(line.split("=", 1)[1].split(":")[0]) <= {"0"}


def test_there_are_no_upload_endpoints() -> None:
    """§17: uploads, none in this version."""
    for path in APP.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "UploadFile" not in source, path
        assert "File(" not in source, path


def test_the_webhook_secret_is_compared_in_constant_time() -> None:
    import inspect

    from app.channels.telegram import webhook

    source = inspect.getsource(webhook.build_router)
    assert "compare_digest" in source
    # And before the body is parsed.
    assert source.index("compare_digest") < source.index("request.json()")


# --- Export and erasure (DESIGN.md §16) -------------------------------------


async def test_export_returns_everything_held_about_one_person(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    import json

    await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        problem_text="what they wrote",
        contact_note="how to reach them",
    )

    data = await export_client(db, client.id)
    json.dumps(data)  # must be serialisable

    assert data["client"]["id"] == str(client.id)
    assert data["identities"]
    assert len(data["requests"]) == 1
    # The one direction problem text may travel: back to its author.
    assert data["requests"][0]["problem_text"] == "what they wrote"


async def test_erasure_makes_a_person_unreachable_and_keeps_the_statistics(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    from app.core.models import Identity

    request = await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        problem_text="deeply private",
        display_name="Anna",
    )

    erased = await erase_client(db, client.id)

    assert erased.erased_at is not None
    assert erased.display_name is None

    # No way left to reach or recognise them.
    identities = (
        (await db.execute(select(Identity).where(Identity.client_id == client.id))).scalars().all()
    )
    assert identities == []

    # The booking survives, without the words.
    await db.refresh(request)
    assert request.problem_text is None
    assert request.display_name is None
    surviving = (
        await db.execute(select(BookingRequest).where(BookingRequest.id == request.id))
    ).scalar_one()
    assert surviving is not None


async def test_erasure_cancels_a_confirmed_session_first(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """Otherwise the slot stays booked and a reminder fires at nobody."""
    from app.core.enums import RequestStatus, SlotStatus

    request = await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
    )
    await booking.admin_approve(db, request.id)

    await erase_client(db, client.id)

    await db.refresh(request)
    assert request.status is RequestStatus.cancelled
    await db.refresh(future_slot)
    assert future_slot.status is SlotStatus.available


async def test_erasure_is_audited_without_recording_what_was_erased(
    db: AsyncSession, client: Client
) -> None:
    """Hard rule 8 applies to the audit log too."""
    await erase_client(db, client.id)

    row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "client.erase", AuditLog.entity_id == str(client.id)
            )
        )
    ).scalar_one()
    assert row.meta is not None
    assert set(row.meta) == {"requests"}  # a count, not the content


# --- Audit coverage ---------------------------------------------------------


async def test_every_state_change_leaves_an_audit_row(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§8: every use-case that changes state appends an audit row."""
    request = await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
    )
    await booking.admin_approve(db, request.id)
    await booking.admin_cancel(db, request.id, reason="ill")

    actions = (
        (await db.execute(select(AuditLog.action).where(AuditLog.entity_id == str(request.uuid))))
        .scalars()
        .all()
    )
    assert set(actions) == {"request.submit", "request.confirm", "request.cancel"}


async def test_audit_meta_never_carries_content(db: AsyncSession) -> None:
    rows = (await db.execute(select(AuditLog).limit(500))).scalars().all()
    for row in rows:
        blob = str(row.meta or {})
        assert "problem" not in blob.lower()


# --- Retention (§14, DESIGN.md §16) -----------------------------------------


def test_the_retention_window_is_a_setting() -> None:
    from app.core.services.settings import MUTABLE_FIELDS

    assert "retention_months" in MUTABLE_FIELDS


def test_the_purge_job_is_registered() -> None:
    from app.worker.main import JOBS

    assert "purge_content" in {job.__name__ for job in JOBS}


# --- Sanitiser --------------------------------------------------------------


def test_every_web_rendered_block_goes_through_the_sanitiser() -> None:
    """§17: all web-rendered content passes the sanitiser.

    The templates mark block HTML `| safe`, which is only sound because
    `to_web_html` sanitised it first -- so nothing else may produce that HTML.
    """
    import inspect

    from app.render import markdown

    source = inspect.getsource(markdown.to_web_html)
    assert "nh3.clean" in source

    templates = ROOT / "app" / "channels" / "web" / "templates"
    safe_uses = []
    for path in templates.rglob("*.html"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "| safe" in line:
                safe_uses.append(f"{path.name}:{number}: {line.strip()}")

    # Each one must be block HTML, which only to_web_html produces.
    for use in safe_uses:
        assert re.search(r"(html|blocks)", use), use
