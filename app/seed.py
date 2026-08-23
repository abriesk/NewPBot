"""Install-time seed data (IMPLEMENTATION.md §20).

Idempotent by construction: every insert is `ON CONFLICT DO NOTHING` against a
real unique constraint, so running it on every boot is safe and running it twice
changes nothing.

Translations are seeded only for keys that do not yet exist. A deploy never
overwrites an existing row -- the therapist's edits win (§15).

Placement note: §3's layout does not name a module for this, and §19's M1 calls
for a "seed script". It sits beside db.py, config.py, and main.py because it is
install-time infrastructure rather than a domain service. Say the word if you
would rather it lived under app/core/services/.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.models import (
    AdminUser,
    ContentTopic,
    Practice,
    SessionType,
    TimezoneOption,
    Translation,
)
from app.db import dispose_engine, unit_of_work

logger = logging.getLogger(__name__)

LOCALES_DIR = Path(__file__).resolve().parents[1] / "locales"

#: Armenian is `hy`. `am` is Amharic and MUST NOT appear anywhere (hard rule 5).
LANGUAGES = ("ru", "hy", "en")

#: §20. Both active, no price until the therapist sets one.
SESSION_TYPES = (
    {"code": "individual", "duration_min": 60, "sort_order": 0},
    {"code": "couple", "duration_min": 60, "sort_order": 1},
)

#: §20. `references` is not in the menu -- it is sent with waitlist
#: confirmations. Titles are not a column; they come from the translation key
#: `content.topic.<code>.title`.
CONTENT_TOPICS = (
    {"code": "work_terms", "sort_order": 0, "show_in_menu": True},
    {"code": "qualification", "sort_order": 1, "show_in_menu": True},
    {"code": "about_psychotherapy", "sort_order": 2, "show_in_menu": True},
    {"code": "references", "sort_order": 3, "show_in_menu": False},
)

#: §20. IANA names with friendly labels, never UTC offsets -- offsets break at
#: every DST transition, which is what v1.0's `"UTC+3"` strings did twice a year.
TIMEZONE_OPTIONS = (
    {"iana_name": "Asia/Yerevan", "display_name": "Yerevan, Tbilisi, Dubai"},
    {"iana_name": "Europe/Moscow", "display_name": "Moscow, Minsk, Istanbul"},
    {"iana_name": "Europe/Kyiv", "display_name": "Kyiv, Bucharest, Athens"},
    {"iana_name": "Europe/Berlin", "display_name": "Berlin, Paris, Madrid"},
    {"iana_name": "Europe/London", "display_name": "London, Dublin, Lisbon"},
    {"iana_name": "America/New_York", "display_name": "New York, Toronto, Miami"},
    {"iana_name": "America/Los_Angeles", "display_name": "Los Angeles, Vancouver, Seattle"},
)


class _NoBoolLoader(yaml.SafeLoader):
    """A YAML loader that does not coerce `yes`/`no`/`on`/`off` to booleans.

    The locale catalogue contains the key `common.yes`. Under YAML 1.1 -- which
    PyYAML implements -- a bare `yes:` parses as the boolean True, so a plain
    safe_load seeds the key `common.True` and every lookup of `common.yes`
    misses silently. Every value in these files is a quoted string, so dropping
    boolean resolution costs nothing and removes the trap.
    """


_NoBoolLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _flatten(node: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in node.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{full}."))
        else:
            flat[full] = str(value)
    return flat


def load_locale_catalogue(locales_dir: Path | None = None) -> dict[str, dict[str, str]]:
    """Read locales/{ru,hy,en}.yaml into {lang: {dotted_key: value}}."""
    directory = locales_dir or LOCALES_DIR
    catalogue: dict[str, dict[str, str]] = {}
    for lang in LANGUAGES:
        path = directory / f"{lang}.yaml"
        text = path.read_text(encoding="utf-8")
        # S506 flags non-safe loaders. _NoBoolLoader subclasses SafeLoader and
        # only drops the boolean resolver, so it constructs no arbitrary
        # objects; the safety property S506 protects is intact.
        raw = yaml.load(text, Loader=_NoBoolLoader) or {}  # noqa: S506
        catalogue[lang] = _flatten(raw)
    return catalogue


async def _seed_practice(session: AsyncSession, settings: Settings) -> Practice:
    """Exactly one practice row. One practice is served; this is not the start
    of an onboarding flow (DESIGN.md §18)."""
    existing = (
        await session.execute(select(Practice).order_by(Practice.id).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    practice = Practice(
        name=settings.practice_name,
        default_language=settings.default_language,
        timezone=settings.practice_timezone,
    )
    session.add(practice)
    await session.flush()
    logger.info("seeded practice id=%s", practice.id)
    return practice


async def _seed_admin_user(session: AsyncSession, settings: Settings, practice_id: int) -> None:
    """The plaintext password is hashed here and never persisted or logged.

    An existing row is left alone: re-running the seed must not reset a password
    the therapist has since changed.
    """
    existing = (
        await session.execute(
            select(AdminUser.id).where(
                AdminUser.practice_id == practice_id,
                AdminUser.username == settings.admin_username,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    session.add(
        AdminUser(
            practice_id=practice_id,
            username=settings.admin_username,
            password_hash=PasswordHasher().hash(settings.admin_password),
        )
    )
    logger.info("seeded admin user %r", settings.admin_username)


async def _seed_rows(
    session: AsyncSession,
    model: type[Any],
    practice_id: int,
    rows: tuple[dict[str, Any], ...],
    conflict_cols: tuple[str, ...],
) -> None:
    await session.execute(
        pg_insert(model)
        .values([{**row, "practice_id": practice_id} for row in rows])
        .on_conflict_do_nothing(index_elements=list(conflict_cols))
    )


async def _seed_translations(
    session: AsyncSession, practice_id: int, catalogue: dict[str, dict[str, str]]
) -> int:
    """Insert only missing keys. Existing rows are never overwritten (§15).

    Untranslated entries -- the ones hy.yaml ships as `""` behind a `# TODO`
    marker -- are skipped rather than stored. Seeding an empty string would
    satisfy the `translation` lookup in §15's chain and return "" to the client,
    where skipping it lets the lookup fall through to the practice default
    language as intended.
    """
    values = [
        {"practice_id": practice_id, "lang": lang, "key": key, "value": value}
        for lang, entries in catalogue.items()
        for key, value in entries.items()
        if value.strip()
    ]
    if not values:
        return 0

    result = await session.execute(
        pg_insert(Translation)
        .values(values)
        .on_conflict_do_nothing(index_elements=["practice_id", "lang", "key"])
        .returning(Translation.id)
    )
    return len(result.fetchall())


async def seed_all(session: AsyncSession, settings: Settings | None = None) -> None:
    """Seed everything in §20. Safe to run on every boot."""
    settings = settings or get_settings()

    practice = await _seed_practice(session, settings)
    await _seed_admin_user(session, settings, practice.id)
    await _seed_rows(session, SessionType, practice.id, SESSION_TYPES, ("practice_id", "code"))
    await _seed_rows(session, ContentTopic, practice.id, CONTENT_TOPICS, ("practice_id", "code"))
    await _seed_rows(
        session, TimezoneOption, practice.id, TIMEZONE_OPTIONS, ("practice_id", "iana_name")
    )
    inserted = await _seed_translations(session, practice.id, load_locale_catalogue())
    logger.info("seed complete: %d new translation row(s)", inserted)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    try:
        async with unit_of_work() as session:
            await seed_all(session, settings)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
