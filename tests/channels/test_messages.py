"""Intent catalogue and message rendering (IMPLEMENTATION.md §9, §10, §13.4).

The catalogue is the join between something happening and something a person
reads, so the tests come in two halves. The first asserts the catalogue
against §10 itself -- every intent has a spec, and every body, part, subject
and action label it names exists in the English catalogue -- because a key
missing here fails at delivery time, in the worker, for one recipient.

The second half renders, and the risk there is per-channel divergence. The
same intent must reach Telegram as HTML in the supported tag subset with
callback data inside the 64-byte limit (measured in bytes, not characters),
and reach email as a neutral subject with a tokenised link and no join
information at all. The §13.5 attachment and the client-note path are here
for the same reason: both are asserted once per channel.

Routing -- which channel an intent goes to, and the §13.4 scrub that decides
what may travel -- is tests/core/test_notifications.py. This file owns what
the message looks like once routing has chosen it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import Action, RenderedMessage
from app.channels.email.transport import EmailTransport, _redact
from app.core.enums import Channel
from app.render.intents import CATALOGUE, action_label_key, body_key_for, spec_for
from app.render.markdown import TELEGRAM_TAGS
from app.render.messages import format_instant, render
from app.seed import load_locale_catalogue

WHEN = datetime(2026, 9, 15, 14, 30, tzinfo=UTC)


# --- The catalogue matches §10 ---------------------------------------------


def test_every_intent_in_section_10_has_a_spec() -> None:
    """§10's table, restated. If an intent is added there and not here, the
    dispatcher would fail permanently at send time."""
    expected = {
        "request.submitted.admin",
        "request.submitted.client",
        "request.proposal.client",
        "request.counter.admin",
        "request.confirmed.client",
        "request.confirmed.admin",
        "request.rescheduled.client",
        "request.rejected.client",
        "request.declined.admin",
        "request.expired.client",
        "request.cancelled.client",
        "reminder.client",
        "request.accepted.admin",
        "waitlist.joined.client",
        "waitlist.joined.admin",
        "waitlist.contacted.client",
        "auth.login_link.client",
        "auth.link_channel.client",
        "request.note.admin",
        "system.delivery_failed.admin",
        "system.health.degraded.admin",
        "system.health.recovered.admin",
    }
    assert set(CATALOGUE) == expected


def test_the_actions_match_section_10() -> None:
    assert spec_for("request.proposal.client").actions == ("accept", "counter", "decline")
    assert spec_for("request.submitted.admin").actions == ("approve", "propose", "reject")
    assert spec_for("request.counter.admin").actions == ("approve", "propose", "reject")
    # §10 gives the client's own request messages a way back into the request.
    for key in ("request.submitted.client", "request.confirmed.client", "reminder.client"):
        assert spec_for(key).actions == ("open",)
    # These carry no actions in §10.
    for key in ("request.expired.client", "request.cancelled.client", "request.note.admin"):
        assert spec_for(key).actions == ()


def test_every_body_key_exists_in_the_english_catalogue() -> None:
    """A body key with no translation renders as the bare key in front of a
    client, which is exactly what §15's chain is meant to avoid."""
    english = load_locale_catalogue()["en"]
    missing = [spec.body_key for spec in CATALOGUE.values() if spec.body_key not in english]
    assert not missing, f"body keys absent from en.yaml: {missing}"


def test_every_optional_part_and_subject_key_exists() -> None:
    english = load_locale_catalogue()["en"]
    missing: list[str] = []
    for spec in CATALOGUE.values():
        for _field, key in spec.optional_parts:
            if key not in english:
                missing.append(key)
        if spec.email_subject_key and spec.email_subject_key not in english:
            missing.append(spec.email_subject_key)
    assert not missing, f"keys absent from en.yaml: {missing}"


def test_every_action_label_key_exists() -> None:
    english = load_locale_catalogue()["en"]
    missing = [
        action_label_key(spec.key, action)
        for spec in CATALOGUE.values()
        for action in spec.actions
        if action_label_key(spec.key, action) not in english
    ]
    assert not missing, f"action labels absent from en.yaml: {missing}"


def test_reminder_offsets_use_their_own_wording_where_it_exists() -> None:
    assert body_key_for("reminder.client", {"offset_min": 1440}).endswith(".24h")
    assert body_key_for("reminder.client", {"offset_min": 60}).endswith(".1h")
    # A third offset is legal (§6.1) and falls back to the generic body.
    assert body_key_for("reminder.client", {"offset_min": 10080}) == ("intent.reminder.client.body")


def test_an_unknown_intent_raises_rather_than_rendering_nothing() -> None:
    with pytest.raises(KeyError):
        spec_for("no.such.intent")


# --- §9 contracts -----------------------------------------------------------


def test_callback_data_over_the_telegram_limit_is_refused() -> None:
    """§9: 64 bytes. A silently truncated callback is a button that does the
    wrong thing."""
    Action(key="accept", label="Accept", callback_data="accept:1234")
    with pytest.raises(ValueError, match="64-byte"):
        Action(key="accept", label="Accept", callback_data="x" * 65)


def test_a_multibyte_callback_is_measured_in_bytes_not_characters() -> None:
    with pytest.raises(ValueError, match="64-byte"):
        Action(key="a", label="l", callback_data="ы" * 33)  # 66 bytes


def test_rendered_message_joins_its_parts() -> None:
    message = RenderedMessage(parts=["one", "two"])
    assert message.text == "one\n\ntwo"


# --- Rendering --------------------------------------------------------------


def test_instants_are_formatted_in_the_recipient_timezone() -> None:
    """Storage is UTC; conversion happens at the edge (DESIGN.md §8)."""
    assert format_instant(WHEN, "Asia/Yerevan") == "2026-09-15 18:30"
    assert format_instant(WHEN, "Europe/London") == "2026-09-15 15:30"


def test_free_text_times_are_returned_as_the_client_wrote_them() -> None:
    """A client who wrote "some evening next week" gets their own words back."""
    assert format_instant("some evening next week", "UTC") == "some evening next week"


async def test_a_telegram_message_carries_only_supported_tags(db: AsyncSession) -> None:
    message = await render(
        db,
        intent_key="request.confirmed.client",
        payload={"uuid": "abc", "time": WHEN.isoformat(), "modality": "online"},
        locale="en",
        channel=Channel.telegram,
        tz="Asia/Yerevan",
        base_url="https://example.test",
    )
    tags = {m.group(1) for m in re.finditer(r"</?([a-z]+)[^>]*>", message.text)}
    assert tags <= TELEGRAM_TAGS
    assert message.parse_mode == "HTML"


async def test_telegram_actions_become_callback_data(db: AsyncSession) -> None:
    message = await render(
        db,
        intent_key="request.proposal.client",
        payload={"uuid": "abc", "time": WHEN.isoformat()},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
        request_id=42,
    )
    assert [a.key for a in message.actions] == ["accept", "counter", "decline"]
    assert all(a.callback_data and a.url is None for a in message.actions)
    assert message.actions[0].callback_data == "accept:42"


async def test_a_telegram_message_without_a_request_gets_no_dead_buttons(
    db: AsyncSession,
) -> None:
    """A button whose callback carries no id is one the router cannot act on."""
    message = await render(
        db,
        intent_key="request.proposal.client",
        payload={"uuid": "abc", "time": WHEN.isoformat()},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )
    assert message.actions == []


async def test_an_email_link_carries_the_view_token(db: AsyncSession) -> None:
    """§12.1: following it opens the request instead of the sign-in form."""
    message = await render(
        db,
        intent_key="request.confirmed.client",
        payload={"uuid": "abc-123", "time": WHEN.isoformat(), "view_token": "tok en/+"},
        locale="en",
        channel=Channel.email,
        tz="UTC",
        base_url="https://example.test",
    )
    assert message.actions
    # Percent-encoded: a raw token is url-safe base64, but the link must not
    # break if that ever changes.
    assert message.actions[0].url == "https://example.test/r/abc-123?token=tok%20en/%2B"


async def test_the_same_intent_gets_no_open_button_on_telegram(db: AsyncSession) -> None:
    """The bot is the interface there, and a Telegram client has no web session."""
    message = await render(
        db,
        intent_key="request.confirmed.client",
        payload={"uuid": "abc-123", "time": WHEN.isoformat()},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
        request_id=7,
    )
    assert message.actions == []


async def test_email_actions_become_links(db: AsyncSession) -> None:
    """§9: email renders the same intent as signed links."""
    message = await render(
        db,
        intent_key="request.proposal.client",
        payload={"uuid": "abc-123", "time": WHEN.isoformat()},
        locale="en",
        channel=Channel.email,
        tz="UTC",
        base_url="https://example.test",
    )
    assert all(a.url and a.callback_data is None for a in message.actions)
    assert message.actions[0].url == "https://example.test/r/abc-123"


async def test_an_email_gets_a_neutral_subject(db: AsyncSession) -> None:
    """§13.4: neutral, and configurable via a translation key."""
    message = await render(
        db,
        intent_key="request.confirmed.client",
        payload={"uuid": "abc", "time": WHEN.isoformat()},
        locale="en",
        channel=Channel.email,
        tz="UTC",
        base_url="https://example.test",
    )
    assert message.subject == "Your session is confirmed"
    # Nothing clinical in the subject line.
    assert "problem" not in message.subject.lower()


async def test_email_never_shows_the_join_link(db: AsyncSession) -> None:
    """§10: for email the client is linked to /r/{uuid} and sees it after
    authenticating."""
    payload = {
        "uuid": "abc",
        "time": WHEN.isoformat(),
        "modality": "online",
        "join_url": "https://meet.example.test/room",
    }
    email = await render(
        db,
        intent_key="request.confirmed.client",
        payload=payload,
        locale="en",
        channel=Channel.email,
        tz="UTC",
        base_url="https://example.test",
    )
    telegram = await render(
        db,
        intent_key="request.confirmed.client",
        payload=payload,
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )

    assert "meet.example.test" not in email.text
    assert "meet.example.test" in telegram.text


async def test_a_reminder_email_carries_the_date_and_time(db: AsyncSession) -> None:
    """§13.4 states this explicitly: a reminder that requires a click is not a
    reminder."""
    message = await render(
        db,
        intent_key="reminder.client",
        payload={"uuid": "abc", "time": WHEN.isoformat(), "offset_min": 60},
        locale="en",
        channel=Channel.email,
        tz="Asia/Yerevan",
        base_url="https://example.test",
    )
    assert "2026-09-15 18:30" in message.text


async def test_an_optional_part_appears_only_when_its_field_is_present(
    db: AsyncSession,
) -> None:
    without = await render(
        db,
        intent_key="request.rejected.client",
        payload={"uuid": "abc", "reason": None},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )
    with_reason = await render(
        db,
        intent_key="request.rejected.client",
        payload={"uuid": "abc", "reason": "not taking new clients"},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )
    assert "Reason" not in without.text
    assert "not taking new clients" in with_reason.text


async def test_waitlist_contacted_message_appears_only_when_present(
    db: AsyncSession,
) -> None:
    without = await render(
        db,
        intent_key="waitlist.contacted.client",
        payload={"uuid": "abc", "message": None},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )
    with_message = await render(
        db,
        intent_key="waitlist.contacted.client",
        payload={"uuid": "abc", "message": "I have an opening next week"},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )
    assert "I have an opening next week" not in without.text
    assert "I have an opening next week" in with_message.text


# --- Email transport (§4) ---------------------------------------------------


async def test_the_transport_refuses_when_smtp_is_unset(
    email_disabled: None,
) -> None:
    """M4 acceptance: with SMTP_HOST unset the channel is disabled cleanly."""
    transport = EmailTransport()
    assert transport.enabled is False

    result = await transport.send("a@example.test", RenderedMessage(parts=["hi"]))
    assert not result.ok
    # Permanent: no amount of retrying configures SMTP.
    assert result.permanent_failure


def test_the_transport_is_enabled_once_smtp_is_configured(email_enabled: None) -> None:
    assert EmailTransport().enabled is True


def test_addresses_are_redacted_in_logs() -> None:
    """Hard rule 8: log identifiers, not a mailing list."""
    assert _redact("someone@example.test") == "s***@example.test"
    assert _redact("nonsense") == "***"


def test_the_transport_registry_omits_email_when_disabled(email_disabled: None) -> None:
    from app.worker.transports import build_transports

    assert Channel.email not in build_transports()


def test_the_transport_registry_includes_email_when_enabled(email_enabled: None) -> None:
    from app.worker.transports import build_transports

    assert Channel.email in build_transports()


# --- §13.5 calendar attachment ----------------------------------------------


CONFIRMED = {
    "uuid": "8b1f0c3e-0000-4000-8000-000000000001",
    "time": WHEN.isoformat(),
    "duration_min": 50,
    "session_type": "individual",
    "modality": "online",
}


async def _confirmed(db: AsyncSession, channel: Channel, **payload: object) -> RenderedMessage:
    return await render(
        db,
        intent_key="request.confirmed.client",
        payload={**CONFIRMED, **payload},
        locale="en",
        channel=channel,
        tz="Asia/Yerevan",
        base_url="https://booking.example.test",
    )


async def test_a_confirmation_by_email_carries_one_calendar_file(db: AsyncSession) -> None:
    message = await _confirmed(db, Channel.email)

    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.filename == "session.ics"
    assert attachment.subtype == "calendar"
    assert "BEGIN:VEVENT" in attachment.content


async def test_the_event_matches_the_request_in_utc(db: AsyncSession) -> None:
    ics = (await _confirmed(db, Channel.email)).attachments[0].content

    assert "DTSTART:20260915T143000Z" in ics
    assert "DTEND:20260915T152000Z" in ics
    assert "TZID" not in ics


async def test_the_uid_is_stable_across_redeliveries(db: AsyncSession) -> None:
    """§13.5: an outbox retry must update the entry, not add a second one."""
    first = (await _confirmed(db, Channel.email)).attachments[0].content
    second = (await _confirmed(db, Channel.email)).attachments[0].content

    uid = f"UID:{CONFIRMED['uuid']}@booking.example.test"
    assert uid in first
    assert uid in second


async def test_no_other_channel_attaches_anything(db: AsyncSession) -> None:
    for channel in (Channel.telegram, Channel.web):
        assert (await _confirmed(db, channel)).attachments == []


async def test_no_other_intent_attaches_anything(db: AsyncSession) -> None:
    for intent_key in ("reminder.client", "request.cancelled.client", "request.rejected.client"):
        message = await render(
            db,
            intent_key=intent_key,
            payload=CONFIRMED,
            locale="en",
            channel=Channel.email,
            tz="UTC",
            base_url="https://booking.example.test",
        )
        assert message.attachments == []


async def test_an_online_session_names_no_meeting_room(db: AsyncSession) -> None:
    """§13.5: the join link must not ride out inside the file either."""
    message = await _confirmed(
        db, Channel.email, join_url="https://meet.example.test/private-room"
    )

    ics = message.attachments[0].content
    assert "meet.example.test" not in ics
    assert "LOCATION:Online" in ics


async def test_an_onsite_session_carries_the_clinic_link(db: AsyncSession) -> None:
    message = await _confirmed(
        db, Channel.email, modality="onsite", join_url="https://clinic.example.test/where"
    )

    assert "LOCATION:https://clinic.example.test/where" in message.attachments[0].content


async def test_a_free_text_time_produces_no_file(db: AsyncSession) -> None:
    """There is no honest DTSTART to make out of "some evening next week"."""
    message = await _confirmed(db, Channel.email, time="some evening next week")

    assert message.attachments == []


async def test_a_missing_duration_produces_no_file(db: AsyncSession) -> None:
    payload = {k: v for k, v in CONFIRMED.items() if k != "duration_min"}
    message = await render(
        db,
        intent_key="request.confirmed.client",
        payload=payload,
        locale="en",
        channel=Channel.email,
        tz="UTC",
        base_url="https://booking.example.test",
    )

    assert message.attachments == []


async def test_a_cancellation_email_asks_for_the_entry_to_be_removed(db: AsyncSession) -> None:
    """§13.5: nothing withdraws the event, so the message has to say so."""
    message = await render(
        db,
        intent_key="request.cancelled.client",
        payload={"uuid": "abc", "time": WHEN.isoformat()},
        locale="en",
        channel=Channel.email,
        tz="UTC",
        base_url="https://booking.example.test",
    )

    assert "calendar" in message.text.lower()


async def test_telegram_is_not_told_about_a_calendar_it_never_got(db: AsyncSession) -> None:
    message = await render(
        db,
        intent_key="request.cancelled.client",
        payload={"uuid": "abc", "time": WHEN.isoformat()},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://booking.example.test",
    )

    assert "calendar" not in message.text.lower()


# --- The merge link rides on the sign-in email (DESIGN.md §5.1) -------------


async def test_the_sign_in_email_carries_the_telegram_merge_link(db: AsyncSession) -> None:
    """The deep link attaches a Telegram account to a client permanently, so it
    belongs in the one message only the address owner can read.

    `telegram_url` sat in the payload for a long time without any renderer
    reading it, which meant the merge was reachable from the booking
    confirmation page and nowhere else -- exactly backwards.
    """
    message = await render(
        db,
        intent_key="auth.login_link.client",
        payload={
            "url": "https://example.test/auth/callback?token=abc",
            "minutes": 30,
            "telegram_url": "https://t.me/testbot?start=link_xyz",
        },
        locale="en",
        channel=Channel.email,
        tz="UTC",
        base_url="https://example.test",
    )

    assert "https://t.me/testbot?start=link_xyz" in message.text
    # And the sign-in link is still there beside it.
    assert "https://example.test/auth/callback?token=abc" in message.text


async def test_a_sign_in_email_without_a_merge_link_offers_no_telegram_action(
    db: AsyncSession,
) -> None:
    """The fallback for an action with no payload URL is the request page. On
    this intent that would put "connect Telegram" on a link to somebody's
    booking, so the action is dropped instead.
    """
    message = await render(
        db,
        intent_key="auth.login_link.client",
        payload={"url": "https://example.test/auth/callback?token=abc", "minutes": 30},
        locale="en",
        channel=Channel.email,
        tz="UTC",
        base_url="https://example.test",
    )

    assert "t.me" not in message.text
    assert [action.key for action in message.actions] == ["open"]


# --- A note reaches the therapist as the note (§10, §13.4) ------------------


async def test_a_note_reaches_telegram_as_the_note_itself(db: AsyncSession) -> None:
    """It carried the identifier alone, so the one sentence a client wanted read
    before the session meant opening a browser to read it."""
    message = await render(
        db,
        intent_key="request.note.admin",
        payload={"uuid": "abc", "name": "Ab", "note": "I may be five minutes late."},
        locale="en",
        channel=Channel.telegram,
        tz="Asia/Yerevan",
        base_url="https://example.test",
    )

    assert "Ab added information to request abc." in message.text
    assert "I may be five minutes late." in message.text
    # §13.4's reasoning is about inboxes, and this is not one -- but a short
    # note and its announcement are still one message, not two.
    assert len(message.parts) == 1


async def test_the_same_note_by_email_stays_an_announcement(db: AsyncSession) -> None:
    """§13.4: negotiation bodies never reach an inbox. The notification service
    strips `note` from an email payload, so this renders what is left."""
    message = await render(
        db,
        intent_key="request.note.admin",
        payload={"uuid": "abc", "name": "Ab"},
        locale="en",
        channel=Channel.email,
        tz="Asia/Yerevan",
        base_url="https://example.test",
    )

    assert "Ab added information to request abc." in message.text
    assert "five minutes" not in message.text
    assert "They wrote" not in message.text


async def test_a_note_from_a_client_with_no_name_does_not_open_on_a_blank(
    db: AsyncSession,
) -> None:
    """§12.2. The body opened on `{name}`, so a client the service cannot name
    produced a sentence beginning with a space."""
    message = await render(
        db,
        intent_key="request.note.admin",
        payload={"uuid": "abc", "name": None, "note": "a thought"},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )

    assert message.text.startswith("New information on request abc.")
    assert "a thought" in message.text


async def test_a_note_at_the_cap_is_split_rather_than_refused_by_telegram(
    db: AsyncSession,
) -> None:
    """§17 lets a client write 4096 characters and §11.2 caps a part at 3500, so
    the longest legal note does not fit in one message -- and escaping makes it
    worse. Concatenating the lines built a message Telegram rejects outright,
    which is a notification the therapist simply never receives."""
    from app.core.policies import CLIENT_TEXT_MAX_CHARS
    from app.render.markdown import TELEGRAM_PART_LIMIT

    # Every character escapes to five, which is the worst case §11.2 has to
    # survive: 4096 ampersands are 20480 characters on the wire.
    note = "&" * CLIENT_TEXT_MAX_CHARS
    message = await render(
        db,
        intent_key="request.note.admin",
        payload={"uuid": "abc", "name": "Ab", "note": note},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )

    assert len(message.parts) > 1
    assert all(len(part) <= TELEGRAM_PART_LIMIT for part in message.parts)
    # Nothing is lost in the splitting, and no part carries half an entity --
    # `&amp;` cut into `&am` and `p;` is not markup Telegram accepts.
    assert "".join(message.parts).count("&amp;") == CLIENT_TEXT_MAX_CHARS
    assert all(part.count("&") == part.count("&amp;") for part in message.parts)


async def test_a_counter_of_4096_characters_also_survives(db: AsyncSession) -> None:
    """The same fault, and it was live before any of this: `request.counter.admin`
    has carried a client's words since it was written."""
    from app.render.markdown import TELEGRAM_PART_LIMIT

    message = await render(
        db,
        intent_key="request.counter.admin",
        payload={"uuid": "abc", "time": None, "note": "ж" * 4096},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )

    assert len(message.parts) > 1
    assert all(len(part) <= TELEGRAM_PART_LIMIT for part in message.parts)


async def test_a_client_cannot_put_formatting_in_the_therapists_chat(
    db: AsyncSession,
) -> None:
    """The packing is shared with the markdown emitter and the parsing is not.
    Rendering a note as markdown would make `**shouting**` bold in her chat, and
    a `<b>` in it something Telegram either honours or rejects the message for.
    """
    message = await render(
        db,
        intent_key="request.note.admin",
        payload={"uuid": "abc", "name": "Ab", "note": "**shout** <b>and</b> & more"},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )

    assert "**shout**" in message.text
    assert "&lt;b&gt;and&lt;/b&gt;" in message.text
    assert "&amp; more" in message.text
