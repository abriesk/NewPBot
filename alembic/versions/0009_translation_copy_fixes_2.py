"""stored translations: common.error.stale no longer promises that the typed text is kept

NP3c stopped a failed-CSRF re-render of /book from echoing what was posted, so "Your text is still
here" became untrue. Same rule as 0008: a row is rewritten only where it still equals the text 0008
(or the seed) stored, so a therapist's own edit survives. Additive and idempotent; the downgrade
does nothing.

Frozen like 0008: the data and the mechanism are embedded, no app code is imported.

Revision ID: 0009_translation_copy_fixes_2
Revises: 0008_translation_copy_fixes
Create Date: 2026-11-14 18:00:00.000000
"""

# ruff: noqa: E501, RUF001
from __future__ import annotations

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_translation_copy_fixes_2"
down_revision: str | None = "0008_translation_copy_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (lang, key, previous repo defaults, new default)
Change = tuple[str, str, Sequence[str], str]
#: (lang, key, default)
Addition = tuple[str, str, str]

CHANGES: tuple[Change, ...] = (
    ("en", "common.error.stale", ("This page was open for too long, so the form was not sent. Your text is still here — please press the button again.",), "This page was open for too long, so the form was not sent. The form has been refreshed — please check the fields and press the button again."),
    ("hy", "common.error.stale", ("Էջը չափազանց երկար բաց է մնացել, ուստի ձևը չուղարկվեց։ Ձեր տեքստը պահպանված է․ սեղմեք կոճակը նորից։",), "Էջը չափազանց երկար բաց է մնացել, ուստի ձևը չուղարկվեց։ Ձևը թարմացվել է․ ստուգեք դաշտերը և սեղմեք կոճակը նորից։"),
    ("ru", "common.error.stale", ("Страница была открыта слишком долго, поэтому форма не отправилась. Ваш текст сохранён — нажмите кнопку ещё раз.",), "Страница была открыта слишком долго, поэтому форма не отправилась. Форма обновлена — проверьте поля и нажмите кнопку ещё раз."),
)

ADDITIONS: tuple[Addition, ...] = ()


def apply_default_changes(
    conn: sa.Connection, changes: Iterable[Change], additions: Iterable[Addition] = ()
) -> tuple[int, int]:
    """Rewrite a row only where it still holds a previous default; add the keys that are missing.

    Returns (rows rewritten, rows added). A frozen copy of the helper this revision was written
    with: idempotent, and a therapist's own edit is never touched.
    """
    rewrite = sa.text(
        "UPDATE translation SET value = :new, updated_at = now() "
        "WHERE lang = :lang AND key = :key AND value IN :old AND value <> :new"
    ).bindparams(sa.bindparam("old", expanding=True))
    add = sa.text(
        "INSERT INTO translation (practice_id, lang, key, value) "
        "SELECT id, :lang, :key, :value FROM practice "
        "ON CONFLICT (practice_id, lang, key) DO NOTHING"
    )
    rewritten = added = 0
    for lang, key, old, new in changes:
        result = conn.execute(rewrite, {"lang": lang, "key": key, "old": list(old), "new": new})
        rewritten += result.rowcount
    for lang, key, value in additions:
        result = conn.execute(add, {"lang": lang, "key": key, "value": value})
        added += result.rowcount
    return rewritten, added


def upgrade() -> None:
    apply_default_changes(op.get_bind(), CHANGES, ADDITIONS)


def downgrade() -> None:
    """Deliberately nothing."""
