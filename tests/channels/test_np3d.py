"""NP3d items 2, 3 and 6: a bad bot token, anonymous CSRF tokens, escaped times in Telegram HTML.

Enforces IMPLEMENTATION.md §12.3 (the webhook), §13 (Telegram) and §17 (CSRF).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.telegram.router import _local
from app.config import get_settings
from app.core.models import Client, Practice, Translation
from app.core.services.translations import invalidate_cache
from app.main import create_app
from tests.channels.test_np3c import _request_with

# --- 2: a malformed bot token is logged once, and shutdown still disposes the engine -----------


@pytest.fixture
def bad_token(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "no-colon-so-not-a-token")
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("BASE_URL", "https://ci.example.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_a_malformed_token_is_one_error_line_and_not_registered(
    bad_token: None, caplog: pytest.LogCaptureFixture
) -> None:
    from app.channels.telegram import webhook as wh

    status = wh.WebhookStatus()
    with caplog.at_level(logging.INFO, logger=wh.logger.name):
        await wh.keep_webhook_registered(status)  # must return, not raise, and not loop

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert status.registered is False
    assert wh.healthz_webhook_field(status) == "not registered"


def test_a_malformed_token_does_not_stop_the_engine_being_disposed(
    bad_token: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app import main as app_main

    disposed: list[bool] = []

    async def dispose() -> None:
        disposed.append(True)

    monkeypatch.setattr(app_main, "dispose_engine", dispose)
    with caplog.at_level(logging.INFO):
        with TestClient(create_app()) as client:
            body = client.get("/healthz").json()
            assert body["webhook"] == "not registered"
    assert disposed == [True]
    webhook_errors = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.name == "app.channels.telegram.webhook"
    ]
    assert len(webhook_errors) == 1


# --- 3: a token minted for one anonymous visitor is not valid for another ----------------------


def test_two_anonymous_visitors_tokens_do_not_cross() -> None:
    from app.channels.web.security import _mint_csrf, csrf_cookie_name, csrf_ok

    alice, bob = _mint_csrf("|"), _mint_csrf("|")
    assert csrf_ok(_request_with({csrf_cookie_name(): alice}), alice) is True
    # Bob's cookie with Alice's token (an attacker who can plant a cookie, not read one)
    assert csrf_ok(_request_with({csrf_cookie_name(): bob}), alice) is False
    assert csrf_ok(_request_with({csrf_cookie_name(): alice}), bob) is False


def test_a_sessions_two_tabs_both_still_work() -> None:
    from app.channels.web.security import (
        CLIENT_COOKIE,
        _binding,
        _mint_csrf,
        csrf_cookie_name,
        csrf_ok,
    )

    session = {CLIENT_COOKIE: "one-client-session"}
    binding = _binding(_request_with(session))
    assert binding != "|"
    tab_a, tab_b = _mint_csrf(binding), _mint_csrf(binding)
    request = _request_with({**session, csrf_cookie_name(): tab_b})
    assert csrf_ok(request, tab_a) is True
    assert csrf_ok(request, tab_b) is True


# --- 6: a time label is escaped for Telegram HTML ----------------------------------------------


async def test_the_time_label_in_telegram_html_is_escaped(
    db: AsyncSession, practice: Practice, client: Client
) -> None:
    await db.execute(
        sa.update(Translation)
        .where(
            Translation.practice_id == practice.id,
            Translation.lang == client.language,
            Translation.key == "date.month.9",
        )
        .values(value="Sep<script>&")
    )
    invalidate_cache()
    try:
        label = await _local(db, datetime(2026, 9, 15, 14, 30, tzinfo=UTC), client, practice)
    finally:
        invalidate_cache()
    assert "<script>" not in label
    assert "Sep&lt;script&gt;&amp;" in label
