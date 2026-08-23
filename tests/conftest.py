"""Test-wide fixtures.

Required configuration is injected before anything imports app.config, so tests
never depend on a developer's local .env.
"""

from __future__ import annotations

import os

TEST_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://psycho:psycho@localhost:5432/psychobooking_test",
    "SECRET_KEY": "test-secret-key-that-is-at-least-32-bytes-long",
    "BASE_URL": "https://example.test",
    "TELEGRAM_BOT_TOKEN": "0000000000:test-token",
    "TELEGRAM_BOT_USERNAME": "test_bot",
    "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
    "TELEGRAM_ADMIN_IDS": "1,2",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "test-admin-password",
}

for _key, _value in TEST_ENV.items():
    os.environ.setdefault(_key, _value)
