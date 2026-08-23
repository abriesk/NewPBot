"""Schema tests (IMPLEMENTATION.md §6, M1 acceptance).

These assert the properties that are easy to lose in a later migration and
expensive to discover in production: the native enum types, the row-level
invariants, and the two index behaviours the booking rules depend on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import PG_ENUM_NAMES

EXPECTED_TABLES = {
    "admin_session",
    "admin_user",
    "audit_log",
    "auth_token",
    "booking_request",
    "client",
    "content_block",
    "content_block_revision",
    "content_topic",
    "identity",
    "negotiation_message",
    "outbox_attempt",
    "outbox_message",
    "practice",
    "reminder",
    "session_type",
    "slot",
    "slot_session_type",
    "timezone_option",
    "translation",
    "waitlist_entry",
}


async def test_every_enum_type_from_section_5_exists(db: AsyncSession) -> None:
    rows = await db.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'"))
    present = {row[0] for row in rows}
    assert set(PG_ENUM_NAMES.values()) <= present


async def test_enum_values_are_lowercase_not_member_names(db: AsyncSession) -> None:
    # values_callable is what guarantees this. Without it SQLAlchemy persists
    # the Python member names and the schema quietly disagrees with §5.
    rows = await db.execute(
        text(
            "SELECT e.enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'request_status'"
        )
    )
    labels = {row[0] for row in rows}
    assert labels == {
        "pending",
        "negotiating",
        "confirmed",
        "rejected",
        "expired",
        "cancelled",
        "completed",
    }


async def test_every_table_from_section_6_exists(db: AsyncSession) -> None:
    rows = await db.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    )
    assert EXPECTED_TABLES <= {row[0] for row in rows}


async def test_every_timestamp_column_is_timezone_aware(db: AsyncSession) -> None:
    """Hard rule 4. A naive `timestamp` column anywhere reintroduces exactly
    the class of bug the rewrite exists to remove."""
    rows = await db.execute(
        text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND data_type LIKE 'timestamp%'"
        )
    )
    naive = [(t, c) for t, c, dtype in rows if dtype != "timestamp with time zone"]
    assert not naive, f"naive timestamp columns: {naive}"


async def _a_practice_id(db: AsyncSession) -> int:
    row = await db.execute(text("SELECT id FROM practice ORDER BY id LIMIT 1"))
    practice_id = row.scalar_one_or_none()
    assert practice_id is not None, "the practice row should have been seeded"
    return int(practice_id)


async def _insert_slot(db: AsyncSession, practice_id: int, **overrides: object) -> None:
    values: dict[str, object] = {
        "practice_id": practice_id,
        "starts_at": datetime.now(UTC) + timedelta(days=1),
        "modality": None,
        "status": "available",
        "hold_expires_at": None,
        "held_by_request": None,
        "booked_request": None,
    }
    values.update(overrides)
    await db.execute(
        text(
            "INSERT INTO slot (practice_id, starts_at, modality, status, hold_expires_at,"
            " held_by_request, booked_request) VALUES (:practice_id, :starts_at,"
            " CAST(:modality AS modality), CAST(:status AS slot_status), :hold_expires_at,"
            " :held_by_request, :booked_request)"
        ),
        values,
    )


async def test_slot_unique_offer_treats_null_modality_as_one_value(db: AsyncSession) -> None:
    """NULLS NOT DISTINCT. modality IS NULL means "either"; without it
    PostgreSQL treats every NULL as distinct and the practice can accumulate
    unlimited duplicate slots at the same instant."""
    practice_id = await _a_practice_id(db)
    starts_at = datetime.now(UTC) + timedelta(days=3)

    await _insert_slot(db, practice_id, starts_at=starts_at)
    with pytest.raises(IntegrityError):
        await _insert_slot(db, practice_id, starts_at=starts_at)


async def test_a_held_slot_must_carry_its_hold_fields(db: AsyncSession) -> None:
    practice_id = await _a_practice_id(db)
    with pytest.raises(IntegrityError):
        # 'held' without hold_expires_at / held_by_request violates §6.4.
        await _insert_slot(db, practice_id, status="held")


async def test_an_available_slot_must_not_carry_a_reservation(db: AsyncSession) -> None:
    practice_id = await _a_practice_id(db)
    with pytest.raises(IntegrityError):
        await _insert_slot(db, practice_id, status="available", hold_expires_at=datetime.now(UTC))


async def test_a_confirmed_request_requires_a_scheduled_start(db: AsyncSession) -> None:
    """§6.5. A confirmed booking with no time is not a booking."""
    practice_id = await _a_practice_id(db)
    client_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO client (id, practice_id, language) VALUES (:id, :practice_id, 'ru')"),
        {"id": client_id, "practice_id": practice_id},
    )
    session_type_id = (
        await db.execute(text("SELECT id FROM session_type ORDER BY id LIMIT 1"))
    ).scalar_one()

    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO booking_request (practice_id, client_id, session_type_id,"
                " modality, status, source_channel) VALUES (:practice_id, :client_id,"
                " :session_type_id, 'online', 'confirmed', 'web')"
            ),
            {
                "practice_id": practice_id,
                "client_id": client_id,
                "session_type_id": session_type_id,
            },
        )


async def test_partial_index_on_confirmed_requests_exists(db: AsyncSession) -> None:
    row = await db.execute(
        text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_booking_request_scheduled_start'"
        )
    )
    indexdef = row.scalar_one_or_none()
    assert indexdef is not None
    assert "WHERE" in indexdef.upper()
    assert "confirmed" in indexdef


async def test_the_deferred_slot_foreign_keys_were_added(db: AsyncSession) -> None:
    """§6.5 adds these by ALTER TABLE once booking_request exists."""
    rows = await db.execute(
        text(
            "SELECT conname FROM pg_constraint WHERE contype = 'f' AND conrelid = 'slot'::regclass"
        )
    )
    names = {row[0] for row in rows}
    assert "fk_slot_held_by_request_booking_request" in names
    assert "fk_slot_booked_request_booking_request" in names
