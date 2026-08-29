"""practice online_only

The switch behind §6.1's online-only mode: the practice is not working in
person for now, so the booking flow stops asking how the client would like to
meet and the picker offers online times only (IMPLEMENTATION.md §13.1).

Deliberately its own column rather than being derived from an empty
`clinic_onsite_url`. That would have cost no migration and said the wrong
thing: an address she keeps while not working there this month is not the same
as an address she has not filled in, and the flow would flip the moment she
pasted one in to save it for later.

`server_default='false'` rather than a backfill: every existing practice was
offering both, which is what false means.

Revision ID: 0004_practice_online_only
Revises: 0003_error_event
Create Date: 2026-08-29 16:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_practice_online_only"
down_revision: str | None = "0003_error_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "practice",
        sa.Column(
            "online_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("practice", "online_only")
