"""NP3c item 9: stored translations reach production (the data migration and its helper)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Practice, Translation
from app.core.services.translation_defaults import apply_default_changes

MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0008_translation_copy_fixes.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mig0008", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _value(db: AsyncSession, practice: Practice, lang: str, key: str) -> str | None:
    row = (
        await db.execute(
            select(Translation.value).where(
                Translation.practice_id == practice.id,
                Translation.lang == lang,
                Translation.key == key,
            )
        )
    ).scalar_one_or_none()
    return row


async def _set(db: AsyncSession, practice: Practice, lang: str, key: str, value: str) -> None:
    await db.execute(
        sa.update(Translation)
        .where(
            Translation.practice_id == practice.id, Translation.lang == lang, Translation.key == key
        )
        .values(value=value)
    )


async def _run(db: AsyncSession, changes: object, additions: object = ()) -> tuple[int, int]:
    def go(sync_session: sa.orm.Session) -> tuple[int, int]:
        return apply_default_changes(sync_session.connection(), changes, additions)  # type: ignore[arg-type]

    return await db.run_sync(go)


async def test_an_untouched_row_is_updated_an_edited_one_kept_a_new_key_added(
    db: AsyncSession, practice: Practice
) -> None:
    await _set(db, practice, "en", "common.yes", "OLD yes")
    await _set(db, practice, "en", "common.no", "Her own no")
    await db.execute(
        sa.delete(Translation).where(
            Translation.practice_id == practice.id, Translation.key == "np3c.brand.new"
        )
    )
    changes = [
        ("en", "common.yes", ("OLD yes", "OLDER yes"), "Yes!"),
        ("en", "common.no", ("OLD no",), "No!"),
    ]
    additions = [("en", "np3c.brand.new", "A new sentence")]

    assert await _run(db, changes, additions) == (1, 1)

    assert await _value(db, practice, "en", "common.yes") == "Yes!"  # untouched: rewritten
    assert await _value(db, practice, "en", "common.no") == "Her own no"  # edited: kept
    assert await _value(db, practice, "en", "np3c.brand.new") == "A new sentence"  # added

    # idempotent: a second run changes nothing, and does not undo her later edit
    await _set(db, practice, "en", "common.yes", "Yes, as she writes it")
    assert await _run(db, changes, additions) == (0, 0)
    assert await _value(db, practice, "en", "common.yes") == "Yes, as she writes it"


async def test_the_migration_carries_np4a_copy_to_old_rows_and_spares_edits(
    db: AsyncSession, practice: Practice
) -> None:
    mig = _migration()
    assert len(mig.CHANGES) > 20 and len(mig.ADDITIONS) > 20
    first, second = mig.CHANGES[0], mig.CHANGES[-1]
    assert (first[0], first[1]) != (second[0], second[1])
    await _set(db, practice, first[0], first[1], first[2][0])  # still the old default
    await _set(db, practice, second[0], second[1], "the therapist's own words")
    gone = mig.ADDITIONS[0]
    await db.execute(
        sa.delete(Translation).where(
            Translation.practice_id == practice.id,
            Translation.lang == gone[0],
            Translation.key == gone[1],
        )
    )

    await _run(db, mig.CHANGES, mig.ADDITIONS)

    assert await _value(db, practice, first[0], first[1]) == first[3]
    assert await _value(db, practice, second[0], second[1]) == "the therapist's own words"
    assert await _value(db, practice, gone[0], gone[1]) == gone[2]
    assert await _run(db, mig.CHANGES, mig.ADDITIONS) == (0, 0)


def test_the_migration_chain_has_a_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("path_separator", "os")
    assert ScriptDirectory.from_config(config).get_heads() == ["0008_translation_copy_fixes"]
    assert _migration().down_revision == "0007_unverified_holds"
