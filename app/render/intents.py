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
    # §7.1: the client agreed to a proposal that named no instant. Nothing can
    # be confirmed from words, so this asks the therapist to put a time to it.
    "request.accepted.admin": IntentSpec(
        key="request.accepted.admin",
        body_key="admin.intent.request.accepted.admin.body",
        actions=("approve", "propose", "reject"),
        email_subject_key="email.subject.request_update",
    ),
    "request.counter.admin": IntentSpec(
        key="request.counter.admin",
        body_key="admin.intent.request.counter.admin.body",
        actions=("approve", "propose", "reject"),
        # Its own line rather than inlined in the body: a counter may carry a
        # time, words, or both, and one template with `{time} {note}` in it
        # rendered every absent half as a gap in the sentence.
        optional_parts=(("note", "admin.intent.request.counter.admin.note"),),
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
        # §10: the note itself, on its own line. Telling her that a note exists
        # and making her open a browser to read one sentence is not a
        # notification -- and Telegram is an admin surface that already carries
        # `problem_text` in the panel and a counter's words in
        # `request.counter.admin`. §13.4 keeps it out of email, which
        # `EMAIL_FORBIDDEN_FIELDS` does by stripping this very field, so email
        # falls back to the announcement without needing an exception here.
        optional_parts=(("note", "admin.intent.request.note.admin.note"),),
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
        # The merge link rides on this intent because this is the message only
        # the address owner can read (DESIGN.md §5.1). Offering it anywhere the
        # address has not proved itself hands a Telegram account the ability to
        # attach itself to somebody else's client record.
        actions=("open", "telegram"),
        optional_parts=(("telegram_url", "intent.auth.login_link.client.telegram_hint"),),
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
    # §16.10. Admin-namespaced, so English only like every operational
    # message (DESIGN.md §11) -- and carrying check ids, never a `detail`
    # string, which is written under looser rules than email allows (§13.4).
    "system.health.degraded.admin": IntentSpec(
        key="system.health.degraded.admin",
        body_key="admin.intent.system.health.degraded.admin.body",
        actions=("open",),
    ),
    "system.health.recovered.admin": IntentSpec(
        key="system.health.recovered.admin",
        body_key="admin.intent.system.health.recovered.admin.body",
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
    ("auth.login_link.client", "telegram"): "intent.auth.login_link.client.action.telegram",
    ("auth.link_channel.client", "open"): "intent.auth.login_link.client.action.telegram",
    ("system.health.degraded.admin", "open"): "admin.intent.system.health.action.open",
    ("request.submitted.admin", "approve"): "admin.request.approve",
    ("request.submitted.admin", "propose"): "admin.request.propose",
    ("request.submitted.admin", "reject"): "admin.request.reject",
    ("request.accepted.admin", "approve"): "admin.request.approve",
    ("request.accepted.admin", "propose"): "admin.request.propose",
    ("request.accepted.admin", "reject"): "admin.request.reject",
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


#: §12.2: bodies that name a client, and so need a variant for the client who
#: has no name anywhere. Value is the body key the `.no_name` suffix attaches
#: to, which is the spec's own -- kept as data so adding such a body is one
#: entry and one pair of translations rather than an `if` nobody remembers.
_NAMED_BODIES = {
    "request.submitted.admin": "admin.intent.request.submitted.admin.body",
    "request.note.admin": "admin.intent.request.note.admin.body",
}


def body_key_for(intent_key: str, payload: dict[str, object]) -> str:
    """The body key, allowing for the reminder offsets that have their own."""
    if intent_key == "reminder.client":
        offset = payload.get("offset_min")
        if isinstance(offset, int) and offset in REMINDER_BODY_KEYS:
            return REMINDER_BODY_KEYS[offset]
    # §7.1: a reply of words only is ordinary on both sides of a negotiation.
    # Announcing a time and leaving a blank where it should be is not.
    if intent_key == "request.proposal.client" and not payload.get("time"):
        return "intent.request.proposal.client.body.no_time"
    if intent_key == "request.counter.admin" and not payload.get("time"):
        return "admin.intent.request.counter.admin.body.no_time"
    # A client who gave no name anywhere. "from <nothing>" is worse than not
    # mentioning it (§12.2 supplies the client's own name where there is one),
    # and a sentence that *opens* on the name is worse still: it began with a
    # blank space and read as though the message had lost its first word.
    if intent_key in _NAMED_BODIES and not payload.get("name"):
        return f"{_NAMED_BODIES[intent_key]}.no_name"
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
