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
from tests.core.test_np3c import MIGRATION, _migration, _value

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
