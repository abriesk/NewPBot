"""NP3d items 1, 4 and 7b: the frozen migration, the outbox detail line, the unused admin label.

Enforces IMPLEMENTATION.md §14 (the outbox), §15 (the catalogue) and §18 (health checks).
"""

from __future__ import annotations

import ast
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel, OutboxStatus
from app.core.models import OutboxMessage, Practice, Translation
from app.core.policies import now_utc
from app.core.services.status import STUCK_GRACE, run_checks
from tests.core.test_locales import LABELS_WITH_A_PLACEHOLDER, _labels_table
from tests.core.test_np3c import MIGRATION, _migration, _set, _value

# --- 1: migration 0008 is frozen: it imports nothing from the app ------------------------------


def test_migration_0008_imports_nothing_from_the_app() -> None:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported, "no imports found: the check is not looking at the file"
    assert [name for name in imported if name == "app" or name.startswith("app.")] == []


async def test_migration_0008_still_does_what_it_did(db: AsyncSession, practice: Practice) -> None:
    """The same rows as before the copy: 44 changes, 72 additions, an edited row spared."""
    mig = _migration()
    assert (len(mig.CHANGES), len(mig.ADDITIONS)) == (44, 72)
    first = mig.CHANGES[0]
    await db.execute(sa.delete(Translation).where(Translation.practice_id == practice.id))
    await db.execute(
        sa.insert(Translation).values(
            practice_id=practice.id, lang=first[0], key=first[1], value=first[2][0]
        )
    )

    def go(sync_session: sa.orm.Session) -> tuple[int, int]:
        return mig.apply_default_changes(  # type: ignore[no-any-return]
            sync_session.connection(), mig.CHANGES, mig.ADDITIONS
        )

    assert await db.run_sync(go) == (1, 72)
    assert await _value(db, practice, first[0], first[1]) == first[3]
    assert await db.run_sync(go) == (0, 0)


# --- 4: the detail line names the claim time, as the WHERE does --------------------------------


async def test_the_stuck_outbox_detail_gives_the_claim_time_not_the_creation_time(
    db: AsyncSession, practice: Practice
) -> None:
    from app.core.models import OutboxAttempt, Reminder

    await db.execute(sa.delete(OutboxAttempt))
    await db.execute(sa.delete(OutboxMessage))
    await db.execute(sa.delete(Reminder))
    claimed = now_utc() - STUCK_GRACE - timedelta(minutes=30)
    created = claimed - timedelta(days=3)
    db.add(
        OutboxMessage(
            practice_id=practice.id,
            channel=Channel.telegram,
            address="100200300",
            intent_key="request.confirmed.client",
            payload={},
            locale="en",
            status=OutboxStatus.sending,
            created_at=created,
            next_attempt_at=claimed,
        )
    )
    await db.flush()
    checks = await run_checks(db)
    detail = next(c for c in checks if c.id == "outbox_stuck").detail
    assert f"{claimed:%Y-%m-%dT%H:%MZ}" in detail
    assert f"{created:%Y-%m-%dT%H:%MZ}" not in detail


# --- 7b: the admin `_labels` key nobody reads is gone ------------------------------------------


def test_the_unread_invalid_markdown_label_is_gone() -> None:
    assert "invalid_markdown" not in _labels_table()
    assert "invalid_markdown" not in LABELS_WITH_A_PLACEHOLDER


# --- NP3d follow-up: common.error.stale no longer promises kept text; migration 0009 ------------

MIGRATION_0009 = MIGRATION.with_name("0009_translation_copy_fixes_2.py")


def _migration_0009():  # type: ignore[no-untyped-def]
    import importlib.util

    spec = importlib.util.spec_from_file_location("mig0009", MIGRATION_0009)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0009_is_frozen_and_chains_after_0008() -> None:
    tree = ast.parse(MIGRATION_0009.read_text(encoding="utf-8"))
    imported = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + [
        n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    ]
    assert [m for m in imported if m == "app" or m.startswith("app.")] == []
    mod = _migration_0009()
    assert mod.revision == "0009_translation_copy_fixes_2"
    assert mod.down_revision == "0008_translation_copy_fixes"


def test_the_stale_text_no_longer_says_the_typed_text_is_kept() -> None:
    from app.seed import LANGUAGES, load_locale_catalogue

    catalogue = load_locale_catalogue()
    mod = _migration_0009()
    by_lang = {c[0]: c for c in mod.CHANGES if c[1] == "common.error.stale"}
    assert set(by_lang) == set(LANGUAGES)
    earlier = {a[0]: a[2] for a in _migration().ADDITIONS if a[1] == "common.error.stale"}
    for lang, (_, _, old, new) in by_lang.items():
        assert old == (earlier[lang],)  # exactly the text 0008 stored, nothing else is touched
        assert catalogue[lang]["common.error.stale"] == new  # the seed and the migration agree
        assert new != old[0]
    assert "still here" not in by_lang["en"][3]


async def test_migration_0009_rewrites_the_old_stale_text_and_spares_an_edit(
    db: AsyncSession, practice: Practice
) -> None:
    mod = _migration_0009()
    en = next(c for c in mod.CHANGES if c[0] == "en")
    ru = next(c for c in mod.CHANGES if c[0] == "ru")
    await _set(db, practice, "en", "common.error.stale", en[2][0])
    await _set(db, practice, "ru", "common.error.stale", "her own wording")

    def go(sync_session: sa.orm.Session) -> tuple[int, int]:
        return mod.apply_default_changes(  # type: ignore[no-any-return]
            sync_session.connection(), mod.CHANGES, mod.ADDITIONS
        )

    assert (await db.run_sync(go))[0] == 1
    assert await _value(db, practice, "en", "common.error.stale") == en[3]
    assert await _value(db, practice, "ru", "common.error.stale") == "her own wording"
    assert ru[3] != "her own wording"
    assert await db.run_sync(go) == (0, 0)
