"""Intent catalogue (IMPLEMENTATION.md §10).

The presentation half of an intent: which translation keys carry its body, and
which actions the recipient gets. The *routing* half -- who hears about a domain
change and on which channel -- lives in app/core/services/notifications.py,
because that is a domain decision.

The two halves are kept honest by a test asserting every intent key the
notification service can emit has a spec here.

Translation keys follow `intent.<key>.<part>` (§10). The admin surface is
English by design, so its keys live under `admin.intent.<key>.<part>`
(DESIGN.md §11).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IntentSpec:
    """One row of §10's table."""

    key: str
    #: Root translation key for the body. `admin.` prefixed intents are English
    #: only and never translated.
    body_key: str
    #: Semantic actions, in the order they should be offered.
    actions: tuple[str, ...] = ()
    #: Optional extra key rendered only when its payload field is present.
    optional_parts: tuple[tuple[str, str], ...] = ()
    #: Email subject key (§13.4: neutral, and configurable via translations).
    email_subject_key: str | None = None


#: §10, exactly. Adding a channel never touches this; adding an *intent* does.
CATALOGUE: dict[str, IntentSpec] = {
    "request.submitted.admin": IntentSpec(
        key="request.submitted.admin",
        body_key="admin.intent.request.submitted.admin.body",
        actions=("approve", "propose", "reject"),
        email_subject_key="email.subject.request_update",
    ),
    "request.submitted.client": IntentSpec(
        key="request.submitted.client",
        # Reusing the in-flow confirmation copy rather than inventing a key:
        # §15 forbids new names for anything the catalogue already covers.
        body_key="booking.submitted",
        actions=("open",),
        email_subject_key="email.subject.request_update",
    ),
    "request.proposal.client": IntentSpec(
        key="request.proposal.client",
        body_key="intent.request.proposal.client.body",
        actions=("accept", "counter", "decline"),
        optional_parts=(("note", "intent.request.proposal.client.note"),),
        email_subject_key="email.subject.request_update",
    ),
    "request.counter.admin": IntentSpec(
        key="request.counter.admin",
        body_key="admin.intent.request.counter.admin.body",
        actions=("approve", "propose", "reject"),
        email_subject_key="email.subject.request_update",
    ),
    "request.confirmed.client": IntentSpec(
        key="request.confirmed.client",
        body_key="intent.request.confirmed.client.body",
        actions=("open",),
        email_subject_key="email.subject.request_confirmed",
    ),
    "request.confirmed.admin": IntentSpec(
        key="request.confirmed.admin",
        body_key="admin.intent.request.confirmed.admin.body",
        email_subject_key="email.subject.request_confirmed",
    ),
    "request.rejected.client": IntentSpec(
        key="request.rejected.client",
        body_key="intent.request.rejected.client.body",
        optional_parts=(("reason", "intent.request.rejected.client.reason"),),
        email_subject_key="email.subject.request_update",
    ),
    "request.expired.client": IntentSpec(
        key="request.expired.client",
        body_key="intent.request.expired.client.body",
        email_subject_key="email.subject.request_update",
    ),
    "request.cancelled.client": IntentSpec(
        key="request.cancelled.client",
        body_key="intent.request.cancelled.client.body",
        optional_parts=(("reason", "intent.request.cancelled.client.reason"),),
        email_subject_key="email.subject.request_cancelled",
    ),
    "reminder.client": IntentSpec(
        key="reminder.client",
        # Per-offset keys exist for 24h and 1h; anything else uses the generic
        # body. See body_key_for_reminder below.
        body_key="intent.reminder.client.body",
        actions=("open",),
        email_subject_key="email.subject.reminder",
    ),
    "waitlist.joined.client": IntentSpec(
        key="waitlist.joined.client",
        body_key="intent.waitlist.joined.client.body",
        email_subject_key="email.subject.request_update",
    ),
    "request.note.admin": IntentSpec(
        key="request.note.admin",
        body_key="admin.intent.request.note.admin.body",
        email_subject_key="email.subject.request_update",
    ),
    "waitlist.joined.admin": IntentSpec(
        key="waitlist.joined.admin",
        body_key="admin.intent.waitlist.joined.admin.body",
        email_subject_key="email.subject.request_update",
    ),
    "auth.login_link.client": IntentSpec(
        key="auth.login_link.client",
        body_key="intent.auth.login_link.client.body",
        actions=("open",),
        email_subject_key="intent.auth.login_link.client.subject",
    ),
    "auth.link_channel.client": IntentSpec(
        key="auth.link_channel.client",
        body_key="intent.auth.link_channel.client.body",
        actions=("open",),
        email_subject_key="email.subject.request_update",
    ),
    "system.delivery_failed.admin": IntentSpec(
        key="system.delivery_failed.admin",
        body_key="admin.intent.system.delivery_failed.admin.body",
    ),
}

#: Action label keys. Most intents name their own; these are the shared ones.
ACTION_LABEL_KEYS: dict[tuple[str, str], str] = {
    ("request.proposal.client", "accept"): "intent.request.proposal.client.action.accept",
    ("request.proposal.client", "counter"): "intent.request.proposal.client.action.counter",
    ("request.proposal.client", "decline"): "intent.request.proposal.client.action.decline",
    ("request.submitted.client", "open"): "request.open",
    ("request.confirmed.client", "open"): "request.open",
    ("reminder.client", "open"): "request.open",
    ("auth.login_link.client", "open"): "intent.auth.login_link.client.action.open",
    ("auth.link_channel.client", "open"): "intent.auth.login_link.client.action.telegram",
    ("request.submitted.admin", "approve"): "admin.request.approve",
    ("request.submitted.admin", "propose"): "admin.request.propose",
    ("request.submitted.admin", "reject"): "admin.request.reject",
    ("request.counter.admin", "approve"): "admin.request.approve",
    ("request.counter.admin", "propose"): "admin.request.propose",
    ("request.counter.admin", "reject"): "admin.request.reject",
}

#: §10: per-offset reminder wording where the catalogue provides it.
REMINDER_BODY_KEYS: dict[int, str] = {
    1440: "intent.reminder.client.body.24h",
    60: "intent.reminder.client.body.1h",
}


def spec_for(intent_key: str) -> IntentSpec:
    try:
        return CATALOGUE[intent_key]
    except KeyError as exc:  # pragma: no cover - guarded by a catalogue test
        raise KeyError(f"no intent spec for {intent_key!r}") from exc


def body_key_for(intent_key: str, payload: dict[str, object]) -> str:
    """The body key, allowing for the reminder offsets that have their own."""
    if intent_key == "reminder.client":
        offset = payload.get("offset_min")
        if isinstance(offset, int) and offset in REMINDER_BODY_KEYS:
            return REMINDER_BODY_KEYS[offset]
    return spec_for(intent_key).body_key


def action_label_key(intent_key: str, action: str) -> str:
    return ACTION_LABEL_KEYS.get((intent_key, action), f"common.{action}")


__all__ = [
    "ACTION_LABEL_KEYS",
    "CATALOGUE",
    "REMINDER_BODY_KEYS",
    "IntentSpec",
    "action_label_key",
    "body_key_for",
    "spec_for",
]
