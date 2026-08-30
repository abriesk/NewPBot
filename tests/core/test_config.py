"""Configuration loading (IMPLEMENTATION.md §4).

Settings are validated as they load, so everything here is a boot-time
failure rather than a runtime one -- which is the point. A service that
starts with a two-character secret, or a UTC offset where an IANA name
belongs, is worse than one that refuses to start.

The defaults are asserted against §4 as literals, so changing one has to be
a deliberate edit to a test. The rest are the constraints that only bite in
deployment: SMTP_FROM required once SMTP_HOST is set, the base URL without
its trailing slash, and the webhook path given an unguessable segment when
unset and normalised to a leading slash when supplied.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from tests.conftest import TEST_ENV


def _settings(**overrides: str) -> Settings:
    """Settings as the tests mean them, not as this machine happens to be set up.

    The SMTP pair is blanked unless a test asks for it: init arguments outrank
    the environment in pydantic-settings, so a deployment that really does
    configure Gmail would otherwise decide what §4's tests observe.
    """
    base = {k.lower(): v for k, v in TEST_ENV.items()}
    base["smtp_host"] = ""
    base["smtp_from"] = ""
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def test_defaults_match_the_specification() -> None:
    """§4's default column, read off the model rather than off a running
    deployment -- a .env that legitimately sets TELEGRAM_MODE=polling must not
    make this fail."""
    defaults = {name: field.default for name, field in Settings.model_fields.items()}

    assert defaults["telegram_mode"] == "webhook"
    assert defaults["smtp_port"] == 587
    assert defaults["smtp_starttls"] is True
    assert defaults["practice_timezone"] == "Asia/Yerevan"
    assert defaults["default_language"] == "ru"
    assert defaults["worker_poll_seconds"] == 20
    assert defaults["trust_proxy_headers"] is True
    assert defaults["log_level"] == "INFO"


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
