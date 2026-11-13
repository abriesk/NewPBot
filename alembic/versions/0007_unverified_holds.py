"""short holds for unverified web bookings; captcha on for new practices

Adds `practice.unverified_hold_minutes` (15), `practice.book_per_ip_per_hour`
(10) and `booking_request.awaiting_verification` (false).

`practice.captcha_on` gets `server_default true`: a practice created from now on
starts with the proof-of-work gate on. The existing row is NOT touched -- an
`ALTER ... SET DEFAULT` changes only what future inserts get, so the live
practice keeps whichever value it has.

Revision ID: 0007_unverified_holds
Revises: 0006_practice_captcha
Create Date: 2026-11-13 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_unverified_holds"
down_revision: str | None = "0006_practice_captcha"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "practice",
        sa.Column(
            "unverified_hold_minutes", sa.Integer(), nullable=False, server_default=sa.text("15")
        ),
    )
    op.add_column(
        "practice",
        sa.Column(
            "book_per_ip_per_hour", sa.Integer(), nullable=False, server_default=sa.text("10")
        ),
    )
    op.add_column(
        "booking_request",
        sa.Column(
            "awaiting_verification", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.alter_column("practice", "captcha_on", server_default=sa.text("true"))


def downgrade() -> None:
    op.alter_column("practice", "captcha_on", server_default=sa.text("false"))
    op.drop_column("booking_request", "awaiting_verification")
    op.drop_column("practice", "book_per_ip_per_hour")
    op.drop_column("practice", "unverified_hold_minutes")
