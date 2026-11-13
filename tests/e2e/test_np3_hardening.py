"""NP3 (IMPLEMENTATION.md §16, §17): login limits and client address (F10, C2),
CSRF (F15), security headers (C15), /readyz (F18), the upload cap (C3), and the
access-log filter.

A test per fix; each was written to fail on the code before it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient
from starlette.requests import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.web import ratelimit
from app.channels.web.security import ADMIN_COOKIE, CSRF_COOKIE
from app.config import get_settings
from app.main import create_app

ADMIN_USER = "admin"


@pytest.fixture
def admin_password() -> str:
    return get_settings().admin_password


@pytest.fixture
def behind_cloudflare(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CLIENT_IP_HEADER", "CF-Connecting-IP")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def web() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


def _request(headers: dict[str, str], peer: str = "192.0.2.5") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/login",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 40000),
        }
    )


# --- C2 / F10: the client address ------------------------------------------


def test_cf_connecting_ip_is_read_when_configured(behind_cloudflare: None) -> None:
    request = _request({"CF-Connecting-IP": "203.0.113.7"})
    assert ratelimit.client_ip(request) == "203.0.113.7"


def test_a_forged_x_forwarded_for_never_moves_the_address(behind_cloudflare: None) -> None:
    for forged in ("9.9.9.9", "1.1.1.1, 2.2.2.2", "not-an-ip"):
        request = _request({"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": forged})
        assert ratelimit.client_ip(request) == "203.0.113.7"


def test_without_a_configured_header_x_forwarded_for_is_ignored() -> None:
    request = _request({"X-Forwarded-For": "9.9.9.9"})
    assert ratelimit.client_ip(request) == "192.0.2.5"


def test_x_forwarded_for_mode_takes_the_hop_the_proxy_appended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIENT_IP_HEADER", "X-Forwarded-For")
    get_settings.cache_clear()
    try:
        request = _request({"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
        assert ratelimit.client_ip(request) == "203.0.113.9"
    finally:
        get_settings.cache_clear()


def test_a_forged_x_forwarded_for_does_not_reset_the_login_limit(
    web: TestClient, behind_cloudflare: None, admin_password: str
) -> None:
    web.get("/admin/login")
    statuses = []
    for i in range(8):
        statuses.append(
            web.post(
                "/admin/login",
                data={
                    "csrf_token": web.cookies.get(CSRF_COOKIE, ""),
                    "username": ADMIN_USER,
                    "password": "wrong",
                },
                headers={"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": f"192.0.2.{i}"},
            ).status_code
        )
    assert statuses[:5] == [401] * 5
    assert statuses[5:] == [429] * 3


def test_the_global_login_cap_holds_across_addresses(
    web: TestClient, behind_cloudflare: None
) -> None:
    web.get("/admin/login")
    statuses = []
    for i in range(36):
        statuses.append(
            web.post(
                "/admin/login",
                data={
                    "csrf_token": web.cookies.get(CSRF_COOKIE, ""),
                    "username": ADMIN_USER,
                    "password": "wrong",
                },
                headers={"CF-Connecting-IP": f"198.51.100.{i + 1}"},
            ).status_code
        )
    # Distinct addresses never trip the per-IP limit ...
    assert statuses[:25] == [401] * 25
    # ... but the global cap does, however many addresses are used.
    assert 429 in statuses[25:]


async def test_argon2_verify_runs_off_the_event_loop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argon2 import PasswordHasher

    from app.channels.web.security import authenticate_admin

    loop_thread = threading.get_ident()
    seen: list[int] = []
    original = PasswordHasher.verify

    def spy(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:  # noqa: A002
        seen.append(threading.get_ident())
        return original(self, hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", spy)

    await authenticate_admin(db, ADMIN_USER, "wrong-password")  # a real user
    await authenticate_admin(db, "nobody-by-that-name", "wrong")  # the dummy hash

    assert len(seen) == 2
    assert all(ident != loop_thread for ident in seen)


async def test_the_event_loop_keeps_turning_during_a_verify(db: AsyncSession) -> None:
    from app.channels.web.security import authenticate_admin

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await authenticate_admin(db, ADMIN_USER, "wrong-password")
    finally:
        task.cancel()
    assert ticks >= 3


# --- F15: the CSRF token ------------------------------------------------------


def test_the_csrf_cookie_is_host_prefixed_and_secure(web: TestClient) -> None:
    response = web.get("/admin/login")
    cookies = [v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"]
    csrf = [c for c in cookies if "pb_csrf" in c]
    assert csrf, cookies
    assert csrf[0].startswith("__Host-pb_csrf=")
    assert "Secure" in csrf[0] and "Path=/" in csrf[0] and "Domain" not in csrf[0]


def _sign_in(client: TestClient, password: str) -> None:
    client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={
            "csrf_token": client.cookies.get(CSRF_COOKIE, ""),
            "username": ADMIN_USER,
            "password": password,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.cookies.get(ADMIN_COOKIE)
    client.get("/admin/maintenance")  # a page with forms: its token is bound to this session


def test_a_token_from_another_session_is_refused(web: TestClient, admin_password: str) -> None:
    from app.channels.web.security import csrf_token_for

    _sign_in(web, admin_password)
    mine = web.cookies.get(CSRF_COOKIE, "")
    assert mine

    # A validly signed, unexpired token minted for somebody else's session.
    theirs = csrf_token_for(_request({"cookie": f"{ADMIN_COOKIE}=another-sessions-cookie"}))
    assert theirs != mine

    # Double-submit with it: the cookie and the field agree, the binding does not.
    web.cookies.set(CSRF_COOKIE, theirs)
    response = web.post("/admin/logout", data={"csrf_token": theirs}, follow_redirects=False)
    assert response.status_code == 403

    # The right token for this session still works.
    web.cookies.set(CSRF_COOKIE, mine)
    ok = web.post("/admin/logout", data={"csrf_token": mine}, follow_redirects=False)
    assert ok.status_code != 403


def test_a_csrf_token_expires(web: TestClient, admin_password: str) -> None:
    web.get("/admin/login")
    token = web.cookies.get(CSRF_COOKIE, "")
    assert token

    with time_machine.travel(datetime.now(UTC) + timedelta(hours=30), tick=True):
        response = web.post(
            "/admin/login",
            data={"csrf_token": token, "username": ADMIN_USER, "password": admin_password},
            follow_redirects=False,
        )
    assert response.status_code == 403


# --- C15: security headers ---------------------------------------------------


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/admin/login", "/no/such/page", "/"])
def test_security_headers_are_on_every_response(web: TestClient, path: str) -> None:
    response = web.get(path)
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in response.headers.get("content-security-policy", "")
    assert response.headers.get("referrer-policy") == "no-referrer"


# --- F18: /readyz -------------------------------------------------------------


def test_readyz_names_no_exception_class(
    web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DatabaseExploded(Exception):
        pass

    def broken() -> object:
        raise DatabaseExploded("no route to db:5432")

    monkeypatch.setattr("app.main.get_session_factory", broken)

    response = web.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "fail"}
    assert "DatabaseExploded" not in response.text


# --- C3: the upload cap, while streaming ---------------------------------------


def test_an_oversized_declared_body_is_refused_with_413(
    web: TestClient, admin_password: str
) -> None:
    _sign_in(web, admin_password)
    payload = json.dumps({"format": "psychobooking.config", "version": 1, "pad": "x" * 6_000_000})

    response = web.post(
        "/admin/maintenance/config/import",
        data={"csrf_token": web.cookies.get(CSRF_COOKIE, ""), "apply": "0"},
        files={"file": ("config.json", payload, "application/json")},
    )

    assert response.status_code == 413


async def test_the_body_is_cut_off_while_streaming() -> None:
    from app.channels.web.middleware import BodyLimitMiddleware

    pulled = 0
    chunk = b"x" * 65536

    async def app(scope, receive, send):  # type: ignore[no-untyped-def]
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():  # type: ignore[no-untyped-def]
        nonlocal pulled
        pulled += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    sent: list[dict] = []

    async def send(message):  # type: ignore[no-untyped-def]
        sent.append(message)

    limit = 1024 * 1024
    middleware = BodyLimitMiddleware(app, {"/upload": limit})
    scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}
    await middleware(scope, receive, send)

    assert sent[0]["status"] == 413
    assert pulled * len(chunk) <= limit + 2 * len(chunk)


# --- the access log -----------------------------------------------------------


def test_healthz_is_not_in_the_access_log() -> None:
    access = logging.getLogger("uvicorn.access")

    def record(path: str) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
            ("192.0.2.1:1", "GET", path, "1.1", 200), None,
        )

    assert access.filter(record("/healthz")) is False
    assert access.filter(record("/admin/login")) is not False


# --- Review round 1 ------------------------------------------------------------


async def test_no_more_than_two_argon2_verifies_run_at_once() -> None:
    """Each verify holds 64 MiB. Unbounded threads let a handful of parallel
    logins push the 512 MB web container over its limit; waiting is free."""
    from argon2 import PasswordHasher

    from app.channels.web import security

    lock = threading.Lock()
    running = 0
    peak = 0
    finished = 0

    def fake_verify(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:  # noqa: A002
        nonlocal running, peak, finished
        with lock:
            running += 1
            peak = max(peak, running)
        import time

        time.sleep(0.05)
        with lock:
            running -= 1
            finished += 1
        return True

    original = PasswordHasher.verify
    PasswordHasher.verify = fake_verify  # type: ignore[method-assign,assignment]
    try:
        hasher = PasswordHasher()
        await asyncio.gather(*(security._verify_off_loop(hasher, "h", "p") for _ in range(10)))
    finally:
        PasswordHasher.verify = original  # type: ignore[method-assign]

    assert finished == 10
    assert peak <= 2


def test_a_csrf_token_is_refreshed_before_it_runs_out(
    web: TestClient, admin_password: str
) -> None:
    web.get("/admin/login")
    first = web.cookies.get(CSRF_COOKIE, "")
    assert first

    with time_machine.travel(datetime.now(UTC) + timedelta(hours=11), tick=True):
        web.get("/admin/login")
        second = web.cookies.get(CSRF_COOKIE, "")
    assert second and second != first

    # The first token is dead by now; the refreshed one is not.
    with time_machine.travel(datetime.now(UTC) + timedelta(hours=20), tick=True):
        response = web.post(
            "/admin/login",
            data={"csrf_token": second, "username": ADMIN_USER, "password": admin_password},
            follow_redirects=False,
        )
    assert response.status_code == 303


def test_a_young_csrf_token_is_kept(web: TestClient) -> None:
    web.get("/admin/login")
    first = web.cookies.get(CSRF_COOKIE, "")
    web.get("/admin/login")
    assert web.cookies.get(CSRF_COOKIE, "") == first


def test_a_send_ends_before_postgres_ends_the_idle_transaction() -> None:
    """The outbox sends inside an open transaction; Postgres ends one idle for
    60 s. aiogram's default request timeout is 60 s, so a hung Telegram would
    get the session killed and the message sent twice."""
    from app.channels.telegram.transport import SEND_TIMEOUT_SECONDS, TelegramTransport
    from app.config import get_settings as settings

    assert SEND_TIMEOUT_SECONDS == 45
    transport = TelegramTransport(settings=settings())
    assert transport._bot.session.timeout == 45  # noqa: SLF001


def test_migrations_lift_the_timeouts() -> None:
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
    for name in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"):
        assert name in source


async def test_a_send_that_hangs_ends_as_transient_before_the_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels.base import RenderedMessage
    from app.channels.telegram import transport as module

    class SlowBot:
        async def send_message(self, **kwargs: object) -> None:
            await asyncio.sleep(5)

    monkeypatch.setattr(module, "SEND_TIMEOUT_SECONDS", 0.1)
    sender = module.TelegramTransport(bot=SlowBot(), settings=get_settings())  # type: ignore[arg-type]

    result = await sender.send("1", RenderedMessage(parts=["hello"]))

    assert not result.ok and not result.permanent_failure, result


async def test_unknown_user_logins_hash_the_dummy_once_and_never_more_than_two_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a restart the dummy hash is not computed yet. Ten parallel logins
    for a name that does not exist must neither compute it ten times nor run
    Argon2 more than two at once -- each call holds 64 MiB."""
    import time

    from argon2 import PasswordHasher

    from app.channels.web import security

    lock = threading.Lock()
    state = {"running": 0, "peak": 0, "hashes": 0}

    def work() -> None:
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.05)
        with lock:
            state["running"] -= 1

    def fake_hash(self: PasswordHasher, password: str | bytes) -> str:
        work()
        with lock:
            state["hashes"] += 1
        return "$argon2id$fake"

    def fake_verify(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:  # noqa: A002
        work()
        return True

    monkeypatch.setattr(PasswordHasher, "hash", fake_hash)
    monkeypatch.setattr(PasswordHasher, "verify", fake_verify)
    monkeypatch.setattr(security, "_dummy_hash", None)

    hasher = PasswordHasher()

    async def one_login() -> None:
        await security._verify_off_loop(hasher, await security._dummy(), "guess")

    await asyncio.gather(*(one_login() for _ in range(10)))

    assert state["hashes"] == 1
    assert state["peak"] <= 2


def test_a_partial_render_near_expiry_keeps_the_token_and_a_page_refreshes_it(
    web: TestClient,
) -> None:
    """An HTMX partial must not rotate the cookie under a page whose forms still
    carry the old token: the therapist's Save would 403 and lose her text."""
    web.get("/admin/login")
    first = web.cookies.get(CSRF_COOKIE, "")
    assert first

    with time_machine.travel(datetime.now(UTC) + timedelta(hours=11), tick=True):
        web.get("/admin/login", headers={"HX-Request": "true"})
        after_partial = web.cookies.get(CSRF_COOKIE, "")
        web.get("/admin/login")
        after_page = web.cookies.get(CSRF_COOKIE, "")

    assert after_partial == first
    assert after_page != first
