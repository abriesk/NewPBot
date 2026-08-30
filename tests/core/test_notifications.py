"""Notification routing and the email content restriction (§10, §13.3, §13.4).

Hard rule 8 lives here. §13.4 forbids problem_text from reaching an email
payload, and the scrub is asserted field by field -- including the cases
where something adjacent is allowed to remain, such as an on-site location,
so the rule cannot quietly be implemented as "drop everything".

Hard rule 2 is the other half: rows are written in the same transaction as
the domain change that caused them, and payloads carry identifiers rather
than rendered text, leaving the wording to the worker.

Between the two sits §13.3 routing -- Telegram preferred when a client has
both channels, confirmations and reminders sent to both, an unverified
address never a target, admin rows in English while client rows follow the
client's own language -- and dedupe, which must refuse to republish an event
that already has a row.

What these rows look like once rendered is tests/channels/test_messages.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel, Modality, OutboxStatus
from app.core.models import Client, OutboxMessage, Practice, Slot
from app.core.policies import now_utc
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


async def test_a_submission_names_the_time_it_is_asking_about(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§10's "requested time". A slot request has no `scheduled_start` until it
    is approved, so without the slot the therapist was asked to approve a
    request whose message ended "requested ."."""
    await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)

    for key in ("request.submitted.admin", "request.submitted.client"):
        rows = await _rows(db, key)
        assert rows
        assert all(
            row.payload["time"] == future_slot.starts_at.isoformat() for row in rows
        ), key


async def test_a_note_reaches_telegram_with_what_it_says(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§10. This carried the identifier alone, so being told that a client had
    written something meant opening a browser to read one sentence -- on the
    surface that exists for answering away from a desk. Telegram already carries
    `problem_text` in the panel and a counter's words in `request.counter.admin`,
    so the note was the odd one out."""
    request = await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)
    await booking.client_note(db, request.id, body_text="I may be five minutes late")
    await notifications.publish(db)

    rows = [r for r in await _rows(db, "request.note.admin") if r.channel is Channel.telegram]
    assert rows
    for row in rows:
        assert row.payload["uuid"] == str(request.uuid)
        assert row.payload["note"] == "I may be five minutes late"


async def test_the_same_note_by_email_carries_only_that_one_arrived(
    db: AsyncSession,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    email_enabled: None,
) -> None:
    """§13.4, unchanged and unweakened: email is shared inboxes and lock-screen
    previews, and a negotiation body must not appear in one. `note` is already
    in `EMAIL_FORBIDDEN_FIELDS`, so the split costs the note intent no special
    case -- the Telegram row carries the body and the email row does not."""
    from app.core.models import AdminUser

    admin = (await db.execute(select(AdminUser).limit(1))).scalars().one()
    admin.email = "therapist@example.test"
    await db.flush()

    request = await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)
    await booking.client_note(db, request.id, body_text="deeply private")
    await notifications.publish(db)

    rows = [r for r in await _rows(db, "request.note.admin") if r.channel is Channel.email]
    assert rows, "the admin has an address in this configuration"
    for row in rows:
        assert row.payload["uuid"] == str(request.uuid)
        assert "deeply private" not in str(row.payload)


async def test_an_email_about_a_request_carries_a_way_into_it(
    db: AsyncSession,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    email_enabled: None,
) -> None:
    """§12.1: the link opens the request, not a sign-in form."""
    from app.core.enums import TokenPurpose
    from app.core.models import AuthToken

    # An email-only client, since §13.3 would otherwise prefer Telegram.
    by_email = await resolve_client(db, Channel.email, "viewer@example.test", verified=True)
    await _submit(db, by_email, session_type_id, future_slot)
    await notifications.publish(db)

    emails = [
        row for row in await _rows(db, "request.submitted.client") if row.channel is Channel.email
    ]
    assert emails
    minted = (
        (
            await db.execute(
                select(AuthToken).where(
                    AuthToken.client_id == by_email.id,
                    AuthToken.purpose == TokenPurpose.view_request,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(minted) == len(emails), "every email link needs its own single-use token"
    assert all(row.payload.get("view_token") for row in emails)

    assert client.id  # the Telegram half is the test below


async def test_a_telegram_row_carries_no_view_token(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The bot is the interface there, and a token in a payload nobody follows
    is a token to leak."""
    await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)

    rows = await _rows(db, "request.submitted.client")
    telegram = [row for row in rows if row.channel is Channel.telegram]
    assert telegram
    assert all("view_token" not in row.payload for row in telegram)


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
        "modality": "online",
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


def test_scrub_keeps_an_onsite_location() -> None:
    """§10: the clinic link **MAY** be sent by email, unlike a meeting room."""
    scrubbed = scrub_for_email(
        "request.confirmed.client",
        {"uuid": "u", "modality": "onsite", "join_url": "https://clinic.example.test/where"},
    )

    assert scrubbed["join_url"] == "https://clinic.example.test/where"


def test_scrub_drops_the_link_when_modality_is_absent() -> None:
    """An intent that carries no modality is treated as the private case."""
    scrubbed = scrub_for_email(
        "reminder.client", {"uuid": "u", "join_url": "https://meet.example.test/room"}
    )

    assert "join_url" not in scrubbed


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


# --- §13.5 calendar attachment, end to end ----------------------------------


async def test_a_confirmed_email_is_delivered_with_its_calendar_file(
    db: AsyncSession,
    session_type_id: int,
    future_slot: Slot,
    email_enabled: None,
) -> None:
    """M12 acceptance, through the real outbox rather than the renderer alone.

    The scrub runs when the row is written, so this is the only test that
    proves the file the client actually receives has no meeting room in it.
    """
    from email import message_from_bytes
    from email.message import EmailMessage
    from email.policy import default as default_policy

    from app.channels.base import DeliveryResult, RenderedMessage
    from app.channels.email.transport import EmailTransport
    from app.worker.jobs.outbox import deliver_one

    recipient = await resolve_client(db, Channel.email, "ics@example.test")
    await link_identity(db, recipient.id, Channel.email, "ics@example.test", verified=True)

    request = await booking.submit_slot_request(
        db,
        client_id=recipient.id,
        slot_id=future_slot.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        problem_text="deeply private",
    )
    await booking.admin_approve(db, request.id, meeting_url="https://meet.example.test/room")
    await notifications.publish(db)

    row = next(
        r
        for r in await _rows(db, "request.confirmed.client")
        if r.channel == Channel.email and r.status == OutboxStatus.pending
    )

    captured: list[RenderedMessage] = []

    class Capture(EmailTransport):
        async def send(self, address: str, message: RenderedMessage) -> DeliveryResult:
            captured.append(message)
            return DeliveryResult.success()

    result = await deliver_one(db, row, {Channel.email: Capture()})
    assert result.ok

    rendered = captured[0]
    assert len(rendered.attachments) == 1
    ics = rendered.attachments[0].content
    assert "BEGIN:VEVENT" in ics
    assert "METHOD:PUBLISH" in ics
    assert "ATTENDEE" not in ics
    # Hard rule 8 and §13.5: neither the problem text nor the meeting room.
    assert "deeply private" not in ics
    assert "meet.example.test" not in ics
    assert str(request.uuid) in ics

    # And it survives MIME assembly as its own text/calendar part.
    mail = EmailMessage()
    mail["From"] = "practice@example.test"
    mail["To"] = "ics@example.test"
    mail["Subject"] = rendered.subject or ""
    mail.set_content(rendered.text)
    for attachment in rendered.attachments:
        mail.add_attachment(
            attachment.content, subtype=attachment.subtype, filename=attachment.filename
        )

    parsed = message_from_bytes(mail.as_bytes(), policy=default_policy)
    parts = [p for p in parsed.walk() if p.get_content_type() == "text/calendar"]
    assert len(parts) == 1
    assert parts[0].get_filename() == "session.ics"
    assert "BEGIN:VCALENDAR" in parts[0].get_content()


# --- A negotiation ending reaches the right person (§12.1) ------------------


async def test_a_client_declining_reaches_the_therapist(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """The reported fault. She had no notification at all for this, so a client
    walking away from a negotiation was something she would have found out by
    noticing the request had stopped moving."""
    request = await _submit(db, client, session_type_id, future_slot)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))
    await notifications.publish(db)

    await booking.client_decline(db, request.id)
    await notifications.publish(db)

    rows = await _rows(db, "request.declined.admin")
    assert rows, "she is told a client walked away"
    assert all(row.payload["uuid"] == str(request.uuid) for row in rows)
    # And the client still hears that their request is closed.
    assert await _rows(db, "request.rejected.client")


async def test_her_own_rejection_does_not_come_back_to_her(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """Both sides emitted the same event, so an unconditional admin envelope
    would report her own action to her. That is why the event carries who."""
    request = await _submit(db, client, session_type_id, future_slot)
    await notifications.publish(db)

    await booking.admin_reject(db, request.id, reason="not taking new clients")
    await notifications.publish(db)

    assert not await _rows(db, "request.declined.admin")
    assert await _rows(db, "request.rejected.client"), "the client is still told"


async def test_the_waitlist_way_out_is_not_reported_as_a_rejection(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§12.1: the client asked to be told when something opens and is told they
    are on the list. "Your request was rejected", for an action they chose
    themselves, is the wrong sentence -- and `waitlist.joined.admin` already
    reaches her with the more useful half."""
    request = await _submit(db, client, session_type_id, future_slot)
    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))
    await notifications.publish(db)

    await booking.client_decline_to_waitlist(db, request.id)
    await notifications.publish(db)

    assert not await _rows(db, "request.rejected.client")
    assert not await _rows(db, "request.declined.admin")
    assert await _rows(db, "waitlist.joined.client")
    assert await _rows(db, "waitlist.joined.admin")


async def test_a_decline_by_a_client_with_no_name_does_not_open_on_a_blank(
    db: AsyncSession, client: Client, session_type_id: int, future_slot: Slot
) -> None:
    """§12.2, through the same no-name table the note intent uses."""
    from app.render.intents import body_key_for

    request = await _submit(db, client, session_type_id, future_slot)
    # Nameless everywhere: the request carries what it was submitted under, and
    # the fallback only reaches the client when that is empty.
    request.display_name = None
    client.display_name = None
    await db.flush()

    await booking.admin_propose(db, request.id, proposed_start=now_utc() + timedelta(days=9))
    await notifications.publish(db)
    await booking.client_decline(db, request.id)
    await notifications.publish(db)

    row = (await _rows(db, "request.declined.admin"))[0]
    assert not row.payload["name"]
    assert body_key_for("request.declined.admin", row.payload).endswith(".no_name")
