"""UI string lookup (IMPLEMENTATION.md §15).

Lookup never raises and never blanks. An unknown key comes back as itself,
an unsatisfied placeholder leaves the text intact, and a language missing a
key falls back to the practice default -- a missing string should degrade to
something legible, never to an empty page or a 500. A missing key is logged
once per process rather than once per call, because the alternative is a log
nobody can read.

The cache is the real risk. Two processes serve this application, so an
admin edit made in one must not leave the other serving the old string; the
tests assert invalidation from outside the process as well as inside it, and
that an unchanged catalogue does not drop the cache for nothing.

One test pins the YAML `yes` key, which a naive loader turns into a boolean
long before it is ever a translation.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Translation
from app.core.services import translations
from app.core.services.translations import (
    get_text,
    invalidate_cache,
    invalidate_if_stale,
    missing_keys,
    set_text,
)


def setup_function() -> None:
    """Each test starts from a cold cache; the module-level one is a process
    cache by design."""
    invalidate_cache()
    translations._warned.clear()
    translations._watermark = None


async def test_a_seeded_key_resolves(db: AsyncSession) -> None:
    assert await get_text(db, "en", "common.skip") == "Skip"


async def test_the_yes_key_resolves_despite_yaml_boolean_coercion(
    db: AsyncSession,
) -> None:
    """`yes:` parses as True under YAML 1.1; the loader disables that, and this
    is the end-to-end proof it worked."""
    assert await get_text(db, "en", "common.yes") == "Yes"
    assert await get_text(db, "en", "common.no") == "No"


async def test_an_unknown_key_returns_the_key_itself(db: AsyncSession) -> None:
    """Visible, greppable, and never an exception in front of a client."""
    assert await get_text(db, "en", "no.such.key") == "no.such.key"


async def test_a_missing_key_is_logged_once_per_process(db: AsyncSession, caplog: object) -> None:
    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)
    with caplog.at_level(logging.WARNING):
        await get_text(db, "en", "another.missing.key")
        await get_text(db, "en", "another.missing.key")
        await get_text(db, "en", "another.missing.key")

    warnings = [r for r in caplog.records if "another.missing.key" in r.getMessage()]
    assert len(warnings) == 1


async def test_an_untranslated_key_falls_back_to_the_default_language(
    db: AsyncSession,
) -> None:
    """A key with no row for the requested language must fall through to the
    practice default rather than return "".

    Asked of a language the catalogue does not carry. hy used to be the
    example, because it shipped entries empty behind a `# TODO`; it is
    complete now, and the chain still has to work for the next gap.
    """
    value = await get_text(db, "xx", "common.error.not_found")
    assert value.strip()
    assert value == await get_text(db, "ru", "common.error.not_found")


async def test_placeholders_are_interpolated(db: AsyncSession) -> None:
    rendered = await get_text(db, "en", "booking.submitted", uuid="abc-123")
    assert "abc-123" in rendered


async def test_an_unsatisfied_placeholder_does_not_raise(db: AsyncSession, caplog: object) -> None:
    """§15: fall back to the unformatted value and log at ERROR. A missing
    placeholder is not a reason to fail a booking."""
    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)
    await set_text(db, "en", "test.placeholder", "needs {missing_name}")

    with caplog.at_level(logging.ERROR):
        rendered = await get_text(db, "en", "test.placeholder", wrong_name="x")

    assert rendered == "needs {missing_name}"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_an_admin_edit_wins_over_the_repository_default(
    db: AsyncSession,
) -> None:
    await set_text(db, "en", "common.skip", "Skip this step")
    assert await get_text(db, "en", "common.skip") == "Skip this step"


async def test_editing_invalidates_the_cache(db: AsyncSession) -> None:
    assert await get_text(db, "en", "common.back") == "Back"
    await set_text(db, "en", "common.back", "Go back")
    assert await get_text(db, "en", "common.back") == "Go back"


async def _stamp(session: AsyncSession, lang: str, key: str) -> object:
    return (
        await session.execute(
            select(Translation.updated_at).where(Translation.lang == lang, Translation.key == key)
        )
    ).scalar_one()


async def test_editing_an_existing_row_moves_its_timestamp(db: AsyncSession) -> None:
    """The trap in the staleness check below.

    The seed inserts every key at boot, so a therapist's edit is always an
    UPDATE, never an INSERT. With only `server_default` on `updated_at` the
    column would be stamped once at first boot and never move again -- and the
    watermark would sit there watching a number that cannot change.
    """
    before = await _stamp(db, "en", "common.skip")
    await set_text(db, "en", "common.skip", "Skip this")

    assert await _stamp(db, "en", "common.skip") > before


async def test_an_edit_in_another_process_drops_this_ones_cache(db: AsyncSession) -> None:
    """§15: the worker renders every outbound message through this cache and
    never calls `invalidate_cache` itself, because the admin edit happens in the
    web process. The edit below is issued as raw SQL for exactly that reason --
    going through `set_text` would clear the cache here and simulate nothing.
    """
    assert await invalidate_if_stale(db) is False, "the first look only records the mark"

    assert await get_text(db, "en", "common.back") == "Back"

    result = await db.execute(
        update(Translation)
        .where(Translation.lang == "en", Translation.key == "common.back")
        .values(value="Go back")
    )
    assert result.rowcount == 1, "the seed must have written this row for the test to mean anything"

    # The bug this job exists to fix: the row changed, this process has not
    # noticed, and every message it renders still says the old thing.
    assert await get_text(db, "en", "common.back") == "Back"

    assert await invalidate_if_stale(db) is True
    assert await get_text(db, "en", "common.back") == "Go back"


async def test_an_unchanged_catalogue_does_not_drop_the_cache(db: AsyncSession) -> None:
    """The other half: a check that always invalidated would be a correct-looking
    way to throw the cache away on every pass."""
    await invalidate_if_stale(db)

    assert await invalidate_if_stale(db) is False
    assert await invalidate_if_stale(db) is False


async def test_missing_keys_ignores_the_english_only_admin_namespace(
    db: AsyncSession,
) -> None:
    """DESIGN.md §11 puts the admin UI in English; those keys are not missing
    from ru or hy, they are deliberately absent."""
    for key in await missing_keys(db, "ru"):
        assert not key.startswith("admin.")


async def test_a_complete_language_reports_nothing_missing(db: AsyncSession) -> None:
    """hy is fully seeded. This is the regression guard on hard rule 10: add an
    `en` key without its Armenian counterpart and the page starts flagging it."""
    assert await missing_keys(db, "hy") == []


async def test_missing_keys_reports_a_language_with_nothing_seeded(db: AsyncSession) -> None:
    """The other half of the above: a page that always reports nothing missing
    would pass the test above while being broken."""
    missing = await missing_keys(db, "xx")

    assert missing
    assert all(not key.startswith("admin.") for key in missing)
