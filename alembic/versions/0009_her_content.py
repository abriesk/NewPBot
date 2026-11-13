"""her content for the client site: a photo and the languages she works in

Adds `practice.languages` (free text, empty until she writes it) and the `practice_photo` table
(one row per practice: the resized image bytes, its type and an etag). Both are additive: no
existing row's value changes and nothing is invented -- with no photo and no languages the client
site shows a neutral illustration and leaves the fact out.

New content topics (`about_me`, `cancellation`, `privacy`) and the new translation keys reach the
live site through `python -m app.seed` (it adds what is missing, never rewrites), so this migration
has no copy to carry: NP4b changes no existing text.

Revision ID: 0009_her_content
Revises: 0008_translation_copy_fixes
Create Date: 2026-11-14 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_her_content"
down_revision: str | None = "0008_translation_copy_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("practice", sa.Column("languages", sa.Text(), nullable=True))
    op.create_table(
        "practice_photo",
        sa.Column("practice_id", sa.Integer(), sa.ForeignKey("practice.id"), primary_key=True),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("practice_photo")
    op.drop_column("practice", "languages")
