"""UI strings (IMPLEMENTATION.md §15).

Lookup order, and the reason for each step:

    process cache      -- a page renders dozens of keys; one round trip each is
                          silly
    translation row    -- the therapist's edits win over anything shipped
    repository YAML    -- a database problem degrades the wording, not the
                          service
    practice default   -- a missing translation falls back to the default
                          language before it falls back to nothing
    the key itself     -- visible, greppable, and never an exception

Two rules on values (§15): no markup -- emphasis and bullets come from the
renderer, not from `<b>` inside a string -- and placeholders are `str.format`
names. A KeyError during formatting falls back to the unformatted value and logs
at ERROR; it never raises into a handler, because a missing placeholder is not a
reason to fail a booking.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Translation
from app.core.services.settings import get_practice

logger = logging.getLogger(__name__)

#: {(lang, key): value}. Invalidated wholesale on an admin edit -- the catalogue
#: is a few hundred short strings, so precision is not worth the bookkeeping.
_cache: dict[tuple[str, str], str] = {}

#: Missing keys are logged once per key per process, not per occurrence (§15).
#: A key missing on a hot path would otherwise fill the log with one line per
#: request.
_warned: set[tuple[str, str]] = set()

#: Loaded lazily from locales/*.yaml. This is the fallback that keeps wording
#: working when the database does not.
_repository: dict[str, dict[str, str]] | None = None


def _repo_catalogue() -> dict[str, dict[str, str]]:
    global _repository
    if _repository is None:
        # Imported lazily: app.seed reads the filesystem, and core should not do
        # that at import time.
        from app.seed import load_locale_catalogue

        _repository = load_locale_catalogue()
    return _repository


def invalidate_cache() -> None:
    """Called after an admin edits a translation."""
    _cache.clear()


async def _load(session: AsyncSession, lang: str) -> None:
    """Warm the cache for one language in a single query."""
    rows = (
        await session.execute(
            select(Translation.key, Translation.value).where(Translation.lang == lang)
        )
    ).all()
    for key, value in rows:
        _cache[(lang, key)] = value


async def get_text(session: AsyncSession, lang: str, key: str, **fmt: Any) -> str:
    """Resolve one key. Never raises for a missing key or a bad placeholder."""
    value = await _resolve(session, lang, key)
    if not fmt:
        return value
    try:
        return value.format(**fmt)
    except (KeyError, IndexError):
        # §15: fall back to the unformatted value, log at ERROR, never raise.
        logger.error("translation %r in %r has an unsatisfied placeholder", key, lang)
        return value


async def _resolve(session: AsyncSession, lang: str, key: str, *, warn: bool = True) -> str:
    if (lang, key) in _cache:
        return _cache[(lang, key)]

    await _load(session, lang)
    if (lang, key) in _cache:
        return _cache[(lang, key)]

    repo = _repo_catalogue().get(lang, {})
    if repo.get(key, "").strip():
        _cache[(lang, key)] = repo[key]
        return repo[key]

    practice = await get_practice(session)
    if lang != practice.default_language:
        # `warn=False`: the caller asked for `lang`, not for the default. A key
        # missing from both would otherwise produce two lines for one lookup,
        # where §15 asks for one per key per process.
        fallback = await _resolve(session, practice.default_language, key, warn=False)
        if fallback != key:
            return fallback

    if warn and (lang, key) not in _warned:
        _warned.add((lang, key))
        logger.warning("missing translation %r for %r", key, lang)
    return key


async def set_text(session: AsyncSession, lang: str, key: str, value: str) -> Translation:
    """Admin edit. Invalidates the cache so the next lookup sees it."""
    practice = await get_practice(session)
    row = (
        await session.execute(
            select(Translation).where(
                Translation.practice_id == practice.id,
                Translation.lang == lang,
                Translation.key == key,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = Translation(practice_id=practice.id, lang=lang, key=key, value=value)
        session.add(row)
    else:
        row.value = value
    await session.flush()

    invalidate_cache()
    return row


async def missing_keys(session: AsyncSession, lang: str) -> list[str]:
    """Keys the admin translations page should flag.

    The `admin.` namespace is English-only by design (DESIGN.md §11), so it is
    not counted as missing for ru or hy.
    """
    catalogue = _repo_catalogue()
    expected = {k for k in catalogue.get("en", {}) if not k.startswith("admin.")}

    stored = set(
        (await session.execute(select(Translation.key).where(Translation.lang == lang)))
        .scalars()
        .all()
    )
    return sorted(expected - stored)
