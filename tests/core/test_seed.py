"""Seed tests (IMPLEMENTATION.md §20, M1 acceptance).

The acceptance criterion is that seeding is idempotent and loads every key from
locales/*.yaml. Idempotency matters because the seed runs on every boot of the
web container.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import (
    AdminUser,
    ContentTopic,
    Practice,
    SessionType,
    TimezoneOption,
    Translation,
)
from app.seed import (
    CONTENT_TOPICS,
    SESSION_TYPES,
    TIMEZONE_OPTIONS,
    load_locale_catalogue,
    seed_all,
)


async def _count(db: AsyncSession, model: type) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def test_exactly_one_practice_is_seeded(db: AsyncSession) -> None:
    # One practice is served. practice_id exists so a tenant key never has to be
    # retrofitted -- not so that practices can be added (DESIGN.md §18).
    assert await _count(db, Practice) == 1


async def test_session_types_topics_and_timezones_are_seeded(db: AsyncSession) -> None:
    """Scoped to the rows seeding names, not to a total of the table.

    All three are rows rather than constants so that the therapist can add one
    without a migration (§6.4), and both the admin form and the config import
    do. A count therefore counts her work as well as the seed's and fails on a
    practice in use -- which is every practice this suite runs against, since
    the tests share the deployment's database (conftest). What M1 asks is that
    seeding loaded what it ships, and that is a subset, not an equality. Same
    fix tests/core/test_config_io.py carries for the translation catalogue.
    """
    stored_types = set((await db.execute(select(SessionType.code))).scalars())
    assert {row["code"] for row in SESSION_TYPES} <= stored_types

    stored_topics = set((await db.execute(select(ContentTopic.code))).scalars())
    assert {row["code"] for row in CONTENT_TOPICS} <= stored_topics

    stored_zones = set((await db.execute(select(TimezoneOption.iana_name))).scalars())
    assert {row["iana_name"] for row in TIMEZONE_OPTIONS} <= stored_zones


async def test_the_home_topic_is_hidden_from_the_menu(db: AsyncSession) -> None:
    """§12.1: it is the front page, not a section of it. In the menu it would
    be a link back to the page the client is already on."""
    show = (
        await db.execute(select(ContentTopic.show_in_menu).where(ContentTopic.code == "home"))
    ).scalar_one()
    assert show is False


async def test_references_topic_is_hidden_from_the_menu(db: AsyncSession) -> None:
    """§20: it is sent with waitlist confirmations, not browsed."""
    show = (
        await db.execute(select(ContentTopic.show_in_menu).where(ContentTopic.code == "references"))
    ).scalar_one()
    assert show is False


async def test_timezones_are_iana_names_not_offsets(db: AsyncSession) -> None:
    names = (await db.execute(select(TimezoneOption.iana_name))).scalars().all()
    assert names, "timezone options should have been seeded"
    for name in names:
        assert "/" in name, f"{name!r} is not an IANA zone name"
        assert "UTC" not in name.upper()


async def test_the_admin_password_is_hashed_with_argon2id(db: AsyncSession) -> None:
    """The plaintext MUST NOT be persisted or logged (§20)."""
    stored = (await db.execute(select(AdminUser.password_hash))).scalars().all()
    assert stored
    for password_hash in stored:
        assert password_hash.startswith("$argon2id$")
        assert "test-admin-password" not in password_hash


async def test_every_translated_locale_key_is_loaded(db: AsyncSession) -> None:
    """M1 acceptance: seeding loads every key from locales/*.yaml.

    Every *translated* key, that is. Untranslated entries ship as `""` behind a
    `# TODO` marker and are deliberately not stored -- see
    test_untranslated_keys_are_not_seeded for why.
    """
    catalogue = load_locale_catalogue()
    for lang, entries in catalogue.items():
        stored = set(
            (await db.execute(select(Translation.key).where(Translation.lang == lang)))
            .scalars()
            .all()
        )
        expected = {key for key, value in entries.items() if value.strip()}
        missing = expected - stored
        assert not missing, f"{lang}: {len(missing)} key(s) not seeded, e.g. {sorted(missing)[:5]}"


async def test_untranslated_keys_are_not_seeded(db: AsyncSession, practice: Practice) -> None:
    """An empty row would satisfy the `translation` step of §15's lookup chain
    and return "" to the client. Leaving the row out lets the lookup fall
    through to the practice default language, which is the intended behaviour.

    Driven by a synthetic catalogue rather than by whatever gaps the locale
    files happen to have. This test used to assert that hy.yaml still carried
    untranslated keys, which stopped being true the moment it was finished --
    the property has to hold for the next key that arrives empty, not for the
    state of the files on any particular day.
    """
    from app.seed import _seed_translations

    inserted = await _seed_translations(
        db,
        practice.id,
        {"hy": {"test.seed.blank": "", "test.seed.spaces": "   ", "test.seed.filled": "value"}},
    )
    assert inserted == 1, "only the key with a value should have been written"

    empties = (
        await db.execute(select(Translation.lang, Translation.key).where(Translation.value == ""))
    ).all()
    assert not empties, f"empty translation rows defeat the fallback chain: {empties}"


async def test_seeding_twice_changes_nothing(db: AsyncSession) -> None:
    before = {
        model.__name__: await _count(db, model)
        for model in (Practice, AdminUser, SessionType, ContentTopic, TimezoneOption, Translation)
    }

    await seed_all(db)
    await seed_all(db)

    after = {
        model.__name__: await _count(db, model)
        for model in (Practice, AdminUser, SessionType, ContentTopic, TimezoneOption, Translation)
    }
    assert before == after


async def test_seeding_does_not_overwrite_an_edited_translation(db: AsyncSession) -> None:
    """§15: the therapist's edits win over the repository defaults."""
    row = (await db.execute(select(Translation).limit(1))).scalar_one()
    edited = "edited by the therapist"
    row.value = edited
    await db.flush()

    await seed_all(db)

    refreshed = (
        await db.execute(select(Translation.value).where(Translation.id == row.id))
    ).scalar_one()
    assert refreshed == edited
