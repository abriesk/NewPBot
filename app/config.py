"""Application configuration.

Every knob here is an environment variable (IMPLEMENTATION.md §4). Anything the
therapist can change at runtime -- availability, booking mode, prices, reminder
offsets, retention -- is a database setting instead, and MUST NOT appear here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TelegramMode = Literal["webhook", "polling"]
LanguageCode = Literal["ru", "hy", "en"]

#: Telegram allows 256; 16 keeps a guessed secret out of reach.
MIN_WEBHOOK_SECRET = 16


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
    #: Required in webhook mode, at least 16 characters (F16). Empty is allowed
    #: only for polling, which has no webhook to protect.
    telegram_webhook_secret: str = ""
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

    #: The request header that carries the caller's address (C2): `CF-Connecting-IP`
    #: on the cloudflared profile, `X-Forwarded-For` behind one appending proxy,
    #: empty for none. Compose sets it per profile; see ratelimit.client_ip.
    client_ip_header: str = ""

    #: Container-side path of the directory the `backup` sidecar writes to
    #: (§16.6). `web` mounts it read-only and only ever lists and streams it.
    backup_path: str = "/backups"

    #: Container-side path of the directory the worker writes status.json to
    #: (§16.8). `worker` mounts it read-write, `web` read-only.
    state_path: str = "/state"

    # --- Deployment (consumed by Compose; declared so .env stays one file) -
    domain: str | None = None
    acme_email: str | None = None
    #: Host side of the same mount, and the sidecar's own schedule. Declared
    #: here so `.env` stays one file even though only Compose reads them.
    backup_dir: str | None = None
    backup_hour_utc: int = 3
    backup_retention_days: int = 30
    state_dir: str | None = None

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

    @model_validator(mode="after")
    def _derive_webhook_path(self) -> Settings:
        """§4: the path defaults to a random segment chosen *at install*.

        Derived from SECRET_KEY rather than generated per process. A fresh
        random segment on every boot would leave Telegram posting to the
        previous URL until re-registration caught up, and would make the path
        impossible to write down. This is stable for a deployment, changes if
        the signing key does, and is not guessable without it.
        """
        if not self.telegram_webhook_path:
            digest = hmac.new(
                self.secret_key.encode(), b"telegram-webhook-path", hashlib.sha256
            ).digest()
            segment = base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]
            object.__setattr__(
                self, "telegram_webhook_path", f"/channels/telegram/webhook/{segment}"
            )
        elif not self.telegram_webhook_path.startswith("/"):
            object.__setattr__(self, "telegram_webhook_path", f"/{self.telegram_webhook_path}")
        return self

    @model_validator(mode="after")
    def _webhook_secret_is_long_enough(self) -> Settings:
        secret = self.telegram_webhook_secret
        if self.telegram_mode == "webhook" and not secret:
            raise ValueError("TELEGRAM_WEBHOOK_SECRET is required when TELEGRAM_MODE=webhook")
        if secret and len(secret) < MIN_WEBHOOK_SECRET:
            raise ValueError(
                f"TELEGRAM_WEBHOOK_SECRET must be at least {MIN_WEBHOOK_SECRET} characters"
            )
        return self

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
