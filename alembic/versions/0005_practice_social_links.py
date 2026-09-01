"""practice social_links

The footer's links (IMPLEMENTATION.md §6.1, §12.1). A JSONB list of
`{"label", "url"}` in the order they are shown.

A column and not a table, which was the choice worth making rather than
assuming. A `social_link` table would buy ordering and per-row activation and
cost a CRUD page, a `config_io` section, and an import merge policy -- for at
most half a dozen pairs that have no relationships, are never joined to, and
have no life apart from the practice that has them. As a column they are one
fieldset on the settings page and travel with the configuration export for
free, since it iterates `MUTABLE_FIELDS` (§16.7).

`server_default='[]'` rather than a backfill: no practice had links before
this, and an empty list is exactly what that means. Validation of what may go
in is in `app/core/services/settings.py`, not here -- a database default
cannot say that a `javascript:` URL is not a link to anyone's Telegram.

Revision ID: 0005_practice_social_links
Revises: 0004_practice_online_only
Create Date: 2026-08-31 17:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_practice_social_links"
down_revision: str | None = "0004_practice_online_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "practice",
        sa.Column(
            "social_links",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("practice", "social_links")
