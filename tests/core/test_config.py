"""Configuration loading (IMPLEMENTATION.md §4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from tests.conftest import TEST_ENV


def _settings(**overrides: str) -> Settings:
    return Settings(**{**{k.lower(): v for k, v in TEST_ENV.items()}, **overrides})  # type: ignore[arg-type]


def test_defaults_match_the_specification() -> None:
    s = _settings()
    assert s.telegram_mode == "webhook"
    assert s.smtp_port == 587
    assert s.smtp_starttls is True
    assert s.practice_timezone == "Asia/Yerevan"
    assert s.default_language == "ru"
    assert s.worker_poll_seconds == 20
    assert s.trust_proxy_headers is True


def test_email_channel_is_disabled_without_smtp_host() -> None:
    assert _settings().email_enabled is False
    assert _settings(smtp_host="smtp.example.test", smtp_from="a@example.test").email_enabled


def test_smtp_from_is_required_once_smtp_host_is_set() -> None:
    with pytest.raises(ValidationError):
        _settings(smtp_host="smtp.example.test")


def test_base_url_rejects_a_trailing_slash() -> None:
    with pytest.raises(ValidationError):
        _settings(base_url="https://example.test/")


def test_secret_key_must_be_at_least_32_bytes() -> None:
    with pytest.raises(ValidationError):
        _settings(secret_key="too-short")


@pytest.mark.parametrize("bad", ["UTC+3", "Moscow Standard Time", "not/a/zone"])
def test_practice_timezone_must_be_an_iana_name(bad: str) -> None:
    # DESIGN.md §8: offset strings break twice a year at DST transitions.
    with pytest.raises(ValidationError):
        _settings(practice_timezone=bad)


def test_webhook_path_gets_an_unguessable_segment_when_unset() -> None:
    path = _settings().telegram_webhook_path
    assert path.startswith("/channels/telegram/webhook/")
    assert len(path.rsplit("/", 1)[1]) >= 16


def test_explicit_webhook_path_is_normalised_to_a_leading_slash() -> None:
    assert _settings(telegram_webhook_path="hook/abc").telegram_webhook_path == "/hook/abc"


def test_admin_telegram_ids_parse_to_ints() -> None:
    assert _settings(telegram_admin_ids="10, 20 ,30").admin_telegram_ids == {10, 20, 30}
    assert _settings(telegram_admin_ids="").admin_telegram_ids == frozenset()
