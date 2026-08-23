"""Application configuration.

Every knob here is an environment variable (IMPLEMENTATION.md §4). Anything the
therapist can change at runtime -- availability, booking mode, prices, reminder
offsets, retention -- is a database setting instead, and MUST NOT appear here.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TelegramMode = Literal["webhook", "polling"]
LanguageCode = Literal["ru", "hy", "en"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core -------------------------------------------------------------
    database_url: str
    secret_key: Annotated[str, Field(min_length=32)]
    base_url: str

    # --- Telegram ---------------------------------------------------------
    telegram_bot_token: str
    telegram_bot_username: str
    telegram_webhook_secret: str
    telegram_webhook_path: str = ""
    telegram_mode: TelegramMode = "webhook"
    telegram_admin_ids: str = ""

    # --- Email (channel disabled entirely when smtp_host is unset) ---------
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    # --- Seed-only values (written to the practice row at first startup) ---
    practice_name: str = "Practice"
    practice_timezone: str = "Asia/Yerevan"
    default_language: LanguageCode = "ru"
    admin_username: str
    admin_password: str

    # --- Runtime ----------------------------------------------------------
    worker_poll_seconds: int = 20
    log_level: str = "INFO"
    trust_proxy_headers: bool = True

    # --- Deployment (consumed by Compose; declared so .env stays one file) -
    domain: str | None = None
    acme_email: str | None = None

    @field_validator("base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        if v.endswith("/"):
            raise ValueError("BASE_URL must not have a trailing slash")
        if not v.startswith(("http://", "https://")):
            raise ValueError("BASE_URL must include a scheme")
        return v

    @field_validator("practice_timezone")
    @classmethod
    def _iana_timezone(cls, v: str) -> str:
        # DESIGN.md §8: IANA names only. "UTC+3" and friends break at DST.
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{v!r} is not an IANA timezone name") from exc
        return v

    @field_validator("telegram_webhook_path")
    @classmethod
    def _unguessable_webhook_path(cls, v: str) -> str:
        if not v:
            return f"/channels/telegram/webhook/{secrets.token_urlsafe(24)}"
        return v if v.startswith("/") else f"/{v}"

    @model_validator(mode="after")
    def _email_config_is_all_or_nothing(self) -> Settings:
        if self.smtp_host and not self.smtp_from:
            raise ValueError("SMTP_FROM is required when SMTP_HOST is set")
        return self

    @property
    def email_enabled(self) -> bool:
        """§4: with SMTP_HOST unset the email channel is disabled cleanly --
        no email identities, no email outbox rows, Telegram login only."""
        return bool(self.smtp_host)

    @property
    def admin_telegram_ids(self) -> frozenset[int]:
        return frozenset(int(part) for part in self.telegram_admin_ids.split(",") if part.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
