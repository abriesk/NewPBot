"""Carrying copy fixes to rows that already exist (the stored-translations problem).

`app.seed` adds only keys that are *new*: a deploy never rewrites a stored row, so a corrected
sentence in `locales/*.yaml` never reaches a site whose rows were seeded earlier. A data migration
that calls `apply_default_changes` does, safely:

* a stored row is rewritten **only where its value still equals a previous repo default** for that
  key -- a row the therapist edited holds some other text and is left alone;
* a key that is missing is added (the same `ON CONFLICT DO NOTHING` the seed uses);
* it is idempotent: after one run no row equals a previous default, and a second run changes
  nothing.

The data goes *into the migration* (previous and new defaults as literals), never read from the
repo's locale files or from git at run time: a migration must say the same thing in a year.
Synchronous, because Alembic hands a migration a plain connection.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import sqlalchemy as sa

#: (lang, key, previous repo defaults, new default)
Change = tuple[str, str, Sequence[str], str]
#: (lang, key, default)
Addition = tuple[str, str, str]


def apply_default_changes(
    conn: sa.Connection, changes: Iterable[Change], additions: Iterable[Addition] = ()
) -> tuple[int, int]:
    """Returns (rows rewritten, rows added)."""
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
