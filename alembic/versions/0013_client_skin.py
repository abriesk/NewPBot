"""client skin: which look the client site has (default | river)

NP6. Adds `practice.client_skin` (text, NOT NULL, default 'default'). Additive: every existing
practice keeps the look it has. The allowed names are checked in `update_settings`, not by a
database constraint, so a later skin is a code change only.

Revision ID: 0013_client_skin
Revises: 0012_week_template
Create Date: 2026-11-15 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_client_skin"
down_revision: str | None = "0012_week_template"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "practice",
        sa.Column("client_skin", sa.Text(), nullable=False, server_default="default"),
    )


def downgrade() -> None:
    op.drop_column("practice", "client_skin")
