"""practice captcha_on, captcha_difficulty

The proof-of-work gate in front of `POST /book` and `POST /waitlist`
(IMPLEMENTATION.md §17, §12.1). Two columns rather than one: the switch is
what she reaches for under abuse, and the difficulty is how hard she turns it.

`server_default='false'` is the whole point of the design. A challenge costs
every client a second of their phone's CPU and locks out a browser with
JavaScript off, which is a real price for a flood that has not happened. It is
off until it is needed.

`16` is the difficulty default: about 65 000 hashes, measured at 0.4s in a
desktop browser, spent while the form is still being typed into.
`app/channels/web/captcha.py` carries the arithmetic and what the other numbers
cost; `app/core/services/settings.py` refuses anything outside 8-24, so a typo
cannot lock the practice out of its own booking form.

Revision ID: 0006_practice_captcha
Revises: 0005_practice_social_links
Create Date: 2026-08-31 19:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_practice_captcha"
down_revision: str | None = "0005_practice_social_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "practice",
        sa.Column("captcha_on", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "practice",
        sa.Column(
            "captcha_difficulty", sa.Integer(), nullable=False, server_default=sa.text("16")
        ),
    )


def downgrade() -> None:
    op.drop_column("practice", "captcha_difficulty")
    op.drop_column("practice", "captcha_on")
