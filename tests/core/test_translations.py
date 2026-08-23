"""UI string lookup (IMPLEMENTATION.md §15)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.services import translations
from app.core.services.translations import get_text, invalidate_cache, missing_keys, set_text


def setup_function() -> None:
    """Each test starts from a cold cache; the module-level one is a process
    cache by design."""
    invalidate_cache()
    translations._warned.clear()


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
    """hy ships some entries empty behind a `# TODO`, so they are not seeded.
    The lookup must fall through rather than return ""."""
    value = await get_text(db, "hy", "common.error.not_found")
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


async def test_missing_keys_ignores_the_english_only_admin_namespace(
    db: AsyncSession,
) -> None:
    """DESIGN.md §11 puts the admin UI in English; those keys are not missing
    from ru or hy, they are deliberately absent."""
    for key in await missing_keys(db, "ru"):
        assert not key.startswith("admin.")


async def test_missing_keys_reports_the_untranslated_armenian_entries(
    db: AsyncSession,
) -> None:
    missing = await missing_keys(db, "hy")
    # hy.yaml still carries `# TODO` entries, which are not seeded.
    assert missing
    assert all(not key.startswith("admin.") for key in missing)
