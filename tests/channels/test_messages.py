"""Intent catalogue and message rendering (IMPLEMENTATION.md §9, §10, §13.4)."""

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
        "request.rejected.client",
        "request.expired.client",
        "request.cancelled.client",
        "reminder.client",
        "waitlist.joined.client",
        "waitlist.joined.admin",
        "auth.login_link.client",
        "auth.link_channel.client",
        "system.delivery_failed.admin",
    }
    assert set(CATALOGUE) == expected


def test_the_actions_match_section_10() -> None:
    assert spec_for("request.proposal.client").actions == ("accept", "counter", "decline")
    assert spec_for("request.submitted.admin").actions == ("approve", "propose", "reject")
    assert spec_for("request.counter.admin").actions == ("approve", "propose", "reject")
    # These carry no actions in §10.
    for key in ("request.confirmed.client", "reminder.client", "request.expired.client"):
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
        payload={"uuid": "abc", "time": WHEN.isoformat(), "request_id": 42},
        locale="en",
        channel=Channel.telegram,
        tz="UTC",
        base_url="https://example.test",
    )
    assert [a.key for a in message.actions] == ["accept", "counter", "decline"]
    assert all(a.callback_data and a.url is None for a in message.actions)
    assert message.actions[0].callback_data == "accept:42"


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
