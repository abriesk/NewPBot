"""stored translations: privacy.default.2 names the optional contact field of the details form

NP5a (from NP4b's report): the default privacy text listed what is kept but not the optional "how
would you like me to reach you" field. Same rule as 0008/0009: a row is rewritten only where it still
equals the previous default, so a therapist's own edit survives. Additive and idempotent; the
downgrade does nothing.

Frozen like 0009: the data and the mechanism are embedded, no app code is imported.

Revision ID: 0011_privacy_contact_copy
Revises: 0010_her_content
Create Date: 2026-11-14 20:00:00.000000
"""

# ruff: noqa: E501, RUF001
from __future__ import annotations

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_privacy_contact_copy"
down_revision: str | None = "0010_her_content"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (lang, key, previous repo defaults, new default)
Change = tuple[str, str, Sequence[str], str]
#: (lang, key, default)
Addition = tuple[str, str, str]

CHANGES: tuple[Change, ...] = (
    ("en", "privacy.default.2", ("What I keep: your name if you give one, your email address, the time you chose and the words you write in your request. If you use Telegram, your Telegram account is kept as well.",), "What I keep: your name if you give one, your email address, the time you chose, the words you write in your request, and how you would like me to reach you if you fill that in. If you use Telegram, your Telegram account is kept as well."),
    ("ru", "privacy.default.2", ("Что я храню: имя, если вы его указали, адрес почты, выбранное время и слова из вашей заявки. Если вы пользуетесь Telegram, сохраняется и ваш аккаунт Telegram.",), "Что я храню: имя, если вы его указали, адрес почты, выбранное время, слова из вашей заявки и способ связи, если вы его указали. Если вы пользуетесь Telegram, сохраняется и ваш аккаунт Telegram."),
    ("hy", "privacy.default.2", ("Ինչ եմ պահում՝ ձեր անունը, եթե նշել եք, էլ. փոստի հասցեն, ընտրված ժամը և հայտում գրածը։ Եթե օգտվում եք Telegram-ից, պահվում է նաև ձեր Telegram-ի հաշիվը։",), "Ինչ եմ պահում՝ ձեր անունը, եթե նշել եք, էլ. փոստի հասցեն, ընտրված ժամը, հայտում գրածը և կապի ձևը, եթե այն նշել եք։ Եթե օգտվում եք Telegram-ից, պահվում է նաև ձեր Telegram-ի հաշիվը։"),
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
