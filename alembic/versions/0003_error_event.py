"""error event

The table behind §16.9's `web_errors` and `worker_errors` checks: an exception
that left no other trace (IMPLEMENTATION.md §6.9).

The `error_source` type is created here rather than in 0001, which is where
every other enum type lives -- 0001 has shipped, and editing a migration that
has run somewhere is how a history stops being reproducible.

Revision ID: 0003_error_event
Revises: 0002_flow_state
Create Date: 2026-08-26 11:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_error_event"
down_revision: str | None = "0002_flow_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE error_source AS ENUM ('web','worker')")

    op.create_table(
        "error_event",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("practice_id", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            postgresql.ENUM("web", "worker", name="error_source", create_type=False),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column(
            "at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["practice_id"], ["practice.id"], name=op.f("fk_error_event_practice_id_practice")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_error_event")),
    )
    op.create_index("ix_error_event_at", "error_event", ["at"])


def downgrade() -> None:
    op.drop_index("ix_error_event_at", table_name="error_event")
    op.drop_table("error_event")
    op.execute("DROP TYPE error_source")
