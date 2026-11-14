"""week templates: a named weekly slot pattern, saved from a real week and applied to a date range

NP5b. Adds the `week_template` table (additive only; nothing existing changes). One row per named
pattern; `entries` is a JSON list of {weekday, time, duration_min, modality} in the practice's own
clock. Unique per (practice, name): saving under an existing name replaces that template.

Revision ID: 0012_week_template
Revises: 0011_privacy_contact_copy
Create Date: 2026-11-14 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_week_template"
down_revision: str | None = "0011_privacy_contact_copy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "week_template",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("practice_id", sa.Integer(), sa.ForeignKey("practice.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("entries", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("practice_id", "name"),
    )


def downgrade() -> None:
    op.drop_table("week_template")
