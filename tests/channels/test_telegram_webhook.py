"""Telegram webhook route (IMPLEMENTATION.md §12.3, §16.1).

The one public POST in the service, and it sits behind no session -- so most
of these tests are about what it refuses. The secret header is the whole of
the authentication and the path segment is unguessable so the endpoint cannot
be enumerated; both are asserted to be checked before any parsing, including
for a body that could not have been parsed anyway.

The rest is registration. §16.1 forbids handing Telegram a plain-HTTP URL,
and polling mode must register no webhook at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.channels.telegram.webhook import SECRET_HEADER, _parse, register_webhook
from app.config import get_settings
from app.main import create_app

UPDATE = {
    "update_id": 1,
    "message": {
        "message_id": 1,
        "chat": {"id": 424242, "type": "private"},
        "from": {"id": 424242, "first_name": "A", "last_name": "B"},
        "text": "/start",
    },
}


@pytest.fixture
def webhook_path() -> str:
    return get_settings().telegram_webhook_path


def test_a_missing_secret_header_is_rejected(webhook_path: str) -> None:
    """§12.3: reject with 403, before the body is parsed."""
    with TestClient(create_app()) as client:
        response = client.post(webhook_path, json=UPDATE)
    assert response.status_code == 403


def test_a_wrong_secret_header_is_rejected(webhook_path: str) -> None:
    with TestClient(create_app()) as client:
        response = client.post(webhook_path, json=UPDATE, headers={SECRET_HEADER: "not-the-secret"})
    assert response.status_code == 403


def test_an_unparseable_body_is_still_rejected_without_the_secret(
    webhook_path: str,
) -> None:
    """The proof that the check happens *before* parsing: a body that would
    raise on json() still gets a clean 403, not a 500."""
    with TestClient(create_app()) as client:
        response = client.post(
            webhook_path,
            content=b"this is not json at all",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 403


def test_the_webhook_path_carries_an_unguessable_segment(webhook_path: str) -> None:
    """§4: include an unguessable segment. The secret header is the real
    control; a guessable path just invites noise."""
    assert webhook_path.startswith("/")
    assert len(webhook_path.rsplit("/", 1)[1]) >= 16


def test_an_unrelated_path_is_not_the_webhook() -> None:
    with TestClient(create_app()) as client:
        response = client.post("/channels/telegram/webhook", json=UPDATE)
    assert response.status_code == 404


# --- Update parsing ---------------------------------------------------------


def test_a_message_update_is_parsed() -> None:
    update = _parse(UPDATE)
    assert update is not None
    assert update.chat_id == 424242
    assert update.text == "/start"
    assert update.display_name == "A B"


def test_a_callback_update_is_parsed() -> None:
    update = _parse(
        {
            "callback_query": {
                "id": "c1",
                "from": {"id": 7, "first_name": "C"},
                "message": {"message_id": 2, "chat": {"id": 7, "type": "private"}},
                "data": "slot:42",
            }
        }
    )
    assert update is not None
    assert update.chat_id == 7
    assert update.callback_data == "slot:42"


def test_an_unhandled_update_type_is_ignored_not_an_error() -> None:
    """Returning anything but 200 makes Telegram retry it forever."""
    assert _parse({"update_id": 9, "poll": {"id": "p"}}) is None


def test_an_update_without_a_chat_is_ignored() -> None:
    assert _parse({"message": {"message_id": 1, "text": "hi"}}) is None


# --- §16.1: refuse to register a webhook without TLS ------------------------


async def test_registration_refuses_over_plain_http(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """§16.1: refuse, and say to use polling instead, rather than failing
    silently."""
    import logging

    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.ERROR):
            registered = await register_webhook()
    finally:
        get_settings.cache_clear()

    assert registered is False
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "polling" in logged.lower()
    assert "https" in logged.lower()


async def test_polling_mode_does_not_register_a_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_MODE", "polling")
    get_settings.cache_clear()
    try:
        assert await register_webhook() is False
    finally:
        get_settings.cache_clear()
