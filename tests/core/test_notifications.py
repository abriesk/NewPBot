"""Notification routing and the email content restriction (§10, §13.3, §13.4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel, Modality, OutboxStatus
from app.core.models import Client, OutboxMessage, Practice, Slot
from app.core.services import booking, notifications, waitlist
from app.core.services.clients import link_identity, resolve_client
from app.core.services.notifications import (
    EMAIL_FORBIDDEN_FIELDS,
    Recipient,
    scrub_for_email,
)

LATER = datetime.now(UTC) + timedelta(days=10)


#: Rows already in the table when a test starts. `db` rolls back and ids only
#: climb, so anything above this line is the test's own work -- everything below
#: belongs to the e2e suite or to the deployment sharing this database, and
#: asserting over it would make these tests fail for reasons of their own.
_BASELINE = 0


@pytest_asyncio.fixture(autouse=True)
async def _outbox_baseline(db: AsyncSession) -> AsyncIterator[None]:
    global _BASELINE
    _BASELINE = (
        await db.execute(select(func.coalesce(func.max(OutboxMessage.id), 0)))
    ).scalar_one()
    yield


async def _rows(db: AsyncSession, intent_key: str | None = None) -> list[OutboxMessage]:
    stmt = select(OutboxMessage).where(OutboxMessage.id > _BASELINE)
    if intent_key:
        stmt = stmt.where(OutboxMessage.intent_key == intent_key)
    return list((await db.execute(stmt.order_by(OutboxMessage.id))).scalars().all())


async def _submit(db: AsyncSession, client: Client, session_type_id: int, slot: Slot):
    return await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        problem_text="deeply private",
        display_name="A. Client",
    )


# --- Events become rows -----------------------------------------------------


async def test_submission_notifies_both_the_client_and_the_admin(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)

    keys = {row.intent_key for row in await _rows(db)}
    assert "request.submitted.client" in keys
    assert "request.submitted.admin" in keys


async def test_rows_are_written_in_the_same_transaction_as_the_change(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """Hard rule 2. Nothing calls a transport; the row is the notification."""
    request = await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)

    rows = await _rows(db, "request.submitted.client")
    assert rows
    assert all(row.status is OutboxStatus.pending for row in rows)
    assert all(row.request_id == request.id for row in rows)


async def test_payloads_carry_no_rendered_text(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§10: payloads are identifiers and instants, not sentences."""
    import json

    await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)

    for row in await _rows(db):
        json.dumps(row.payload)  # MUST be JSON-serialisable
        assert "body" not in row.payload
        assert "text" not in row.payload


async def test_completion_notifies_nobody(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§7.1: complete emits no event at all."""
    request = await _submit(db, client, session_type_id, future_slot)
    confirmed = await booking.admin_approve(db, request.id)
    await notifications.publish(db)
    before = len(await _rows(db))

    await booking.complete_request(db, confirmed.id)
    await notifications.publish(db)

    assert len(await _rows(db)) == before


# --- §13.3 delivery policy --------------------------------------------------


async def test_telegram_is_preferred_when_the_client_has_both(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    await link_identity(db, client.id, Channel.email, "both@example.test", verified=True)

    request = await _submit(db, client, session_type_id, future_slot)
    await booking.admin_reject(db, request.id, reason="no")
    await notifications.publish(db)

    channels = {r.channel for r in await _rows(db, "request.rejected.client")}
    assert channels == {Channel.telegram}


async def test_confirmations_and_reminders_go_to_both_channels(
    db: AsyncSession,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    email_enabled: None,
) -> None:
    """§13.3 names exactly these two intents."""
    await link_identity(db, client.id, Channel.email, "both2@example.test", verified=True)

    request = await _submit(db, client, session_type_id, future_slot)
    await booking.admin_approve(db, request.id)
    await notifications.publish(db)

    channels = {r.channel for r in await _rows(db, "request.confirmed.client")}
    assert channels == {Channel.telegram, Channel.email}


async def test_an_unverified_email_is_never_a_target(db: AsyncSession) -> None:
    """Otherwise the service becomes a way to send unsolicited mail in the
    therapist's name (DESIGN.md §5.1)."""
    unverified = await resolve_client(db, Channel.email, "unverified@example.test")
    entry = await waitlist.join_waitlist(db, client_id=unverified.id)
    await notifications.publish(db)

    rows = await _rows(db, "waitlist.joined.client")
    assert all(r.channel != Channel.email for r in rows)
    assert entry.status.value == "new"


async def test_admin_rows_are_english(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The admin surface is English by design (DESIGN.md §11)."""
    await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)

    for row in await _rows(db, "request.submitted.admin"):
        assert row.locale == "en"


async def test_client_rows_use_the_client_language(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    client.language = "hy"
    await db.flush()

    await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)

    for row in await _rows(db, "request.submitted.client"):
        assert row.locale == "hy"
        assert row.locale != "am"  # hard rule 5


# --- §13.4 email content restriction ---------------------------------------


def test_scrub_removes_every_forbidden_field() -> None:
    payload = {
        "uuid": "u",
        "problem": "private",
        "note": "negotiation body",
        "thread": "more",
        "contact_note": "phone",
        "join_url": "https://meet.example.test/room",
        "time": "2026-01-01T00:00:00+00:00",
    }
    scrubbed = scrub_for_email("request.confirmed.client", payload)

    for field in EMAIL_FORBIDDEN_FIELDS:
        assert field not in scrubbed
    # The join link goes too: for email the client is sent to /r/{uuid}.
    assert "join_url" not in scrubbed
    assert scrubbed["uuid"] == "u"
    assert scrubbed["time"]


async def test_an_email_row_never_carries_problem_text(
    db: AsyncSession,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    email_enabled: None,
) -> None:
    """Hard rule 8 and §13.4, end to end."""
    admin_email_client = await resolve_client(db, Channel.email, "e2e@example.test")
    await link_identity(db, admin_email_client.id, Channel.email, "e2e@example.test", verified=True)

    request = await booking.submit_slot_request(
        db,
        client_id=admin_email_client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        problem_text="deeply private",
    )
    await booking.admin_approve(db, request.id)
    await notifications.publish(db)

    for row in await _rows(db):
        if row.channel == Channel.email:
            assert "deeply private" not in str(row.payload)
            assert "problem" not in row.payload
            assert "join_url" not in row.payload


async def test_a_telegram_row_may_carry_the_join_link(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    """Telegram is already on the client's own device (DESIGN.md §12)."""
    practice.online_meeting_url = "https://meet.example.test/room"
    await db.flush()

    request = await _submit(db, client, session_type_id, future_slot)
    await booking.admin_approve(db, request.id)
    await notifications.publish(db)

    telegram = [
        r for r in await _rows(db, "request.confirmed.client") if r.channel == Channel.telegram
    ]
    assert telegram
    assert telegram[0].payload["join_url"] == "https://meet.example.test/room"


async def test_join_info_prefers_the_per_request_override(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot, practice: Practice
) -> None:
    """§10: request.meeting_url, else practice.online_meeting_url."""
    practice.online_meeting_url = "https://meet.example.test/standing"
    await db.flush()

    request = await _submit(db, client, session_type_id, future_slot)
    await booking.admin_approve(db, request.id, meeting_url="https://meet.example.test/one-off")
    await notifications.publish(db)

    telegram = [
        r for r in await _rows(db, "request.confirmed.client") if r.channel == Channel.telegram
    ]
    assert telegram[0].payload["join_url"] == "https://meet.example.test/one-off"


async def test_onsite_uses_the_clinic_address(
    db: AsyncSession, client: Client, session_type_id: int, practice: Practice, future_slot: Slot
) -> None:
    """§10: for onsite, clinic_onsite_url takes its place."""
    practice.clinic_onsite_url = "https://maps.example.test/clinic"
    await db.flush()

    request = await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.onsite,
        source_channel=Channel.web,
    )
    await booking.admin_approve(db, request.id)
    await notifications.publish(db)

    telegram = [
        r for r in await _rows(db, "request.confirmed.client") if r.channel == Channel.telegram
    ]
    assert telegram[0].payload["join_url"] == "https://maps.example.test/clinic"


# --- Dedupe -----------------------------------------------------------------


async def test_republishing_the_same_event_is_refused_by_the_dedupe_key(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The unique dedupe key is what makes a worker restart mid-sweep unable to
    double-notify (DESIGN.md §12)."""
    from sqlalchemy.exc import IntegrityError

    from app.core.events import RequestSubmitted

    request = await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)

    replay = RequestSubmitted(request_id=request.id, request_uuid=request.uuid)
    with pytest.raises(IntegrityError):
        await notifications.publish(db, [replay])


async def test_the_delivery_failure_alert_names_the_address_not_the_body(
    db: AsyncSession,
) -> None:
    await notifications.alert_admin_delivery_failed(
        db, intent_key="reminder.client", address="a@example.test", error="550 no such user"
    )
    rows = await _rows(db, "system.delivery_failed.admin")
    assert rows
    assert rows[0].payload["address"] == "a@example.test"
    assert rows[0].payload["intent"] == "reminder.client"
    assert "body" not in rows[0].payload


async def test_recipient_enum_has_exactly_two_values() -> None:
    assert {r.value for r in Recipient} == {"admin", "client"}
