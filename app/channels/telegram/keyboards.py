"""Telegram keyboards (IMPLEMENTATION.md §13.1).

Callback data is `<action>:<argument>` and stays inside 64 bytes; the handler
looks the rest up (§9). Nothing here decides booking rules -- it turns already
made decisions into buttons.
"""

from __future__ import annotations

from collections import defaultdict
from zoneinfo import ZoneInfo

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.core.services.slots import SlotView

#: Callback action names. Short, because the 64-byte budget is shared with the
#: argument.
LANG = "lang"
TOPIC = "topic"
SLOT = "slot"
STYPE = "stype"
MODE = "mode"
TZ = "tz"
CONTACT = "contact"
SKIP = "skip"
CANCEL = "cancel"

#: §13.1 step 7's contact choices, as callback arguments.
CONTACT_TELEGRAM = "tg"
CONTACT_EMAIL = "email"
CONTACT_OTHER = "other"

#: Negotiation actions. These names come from the intent catalogue (§10), not
#: from here: the buttons on a proposal are rendered by app/render/messages.py
#: as `<action>:<request_id>`, and this router has to answer to the same words.
ACCEPT = "accept"
COUNTER = "counter"
DECLINE = "decline"

#: §13.1: a slot tapped while answering a proposal. Its own action rather than
#: the booking picker's `SLOT`, because a tap here means "I suggest this", not
#: "hold this for me" -- and one action doing both would have to consult parked
#: flow state to know which it was.
COUNTER_SLOT = "cslot"
#: §12.1's way out where a counter may not be words.
COUNTER_WAITLIST = "cwait"

#: The admin actions §13.2 keeps on the phone.
APPROVE = "approve"
PROPOSE = "propose"
REJECT = "reject"
CANCEL_REQUEST = "cancelreq"

#: §13.2's panel navigation. Short names: the argument shares the 64 bytes.
PANEL = "apanel"
PANEL_REQUESTS = "areq"
PANEL_OPEN = "aopen"
PANEL_SESSIONS = "asess"
PANEL_WAITLIST = "awl"
PANEL_AVAILABILITY = "aavail"
PANEL_SKIP = "askip"

CLIENT_ACTIONS = frozenset({ACCEPT, COUNTER, DECLINE, COUNTER_SLOT, COUNTER_WAITLIST})
ADMIN_ACTIONS = frozenset(
    {
        APPROVE,
        PROPOSE,
        REJECT,
        CANCEL_REQUEST,
        PANEL,
        PANEL_REQUESTS,
        PANEL_OPEN,
        PANEL_SESSIONS,
        PANEL_WAITLIST,
        PANEL_AVAILABILITY,
        PANEL_SKIP,
    }
)


def panel_keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """§13.2: `(label, callback_data)` pairs, laid out as given.

    The panel builds its own rows because the shape carries meaning there --
    actions on one line, navigation on the next.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def language_keyboard() -> InlineKeyboardMarkup:
    """§13.1 step 2. Shown on first contact only.

    Language names are not translated -- a client who cannot read the current
    language still has to recognise their own.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Русский", callback_data=f"{LANG}:ru")],
            [InlineKeyboardButton(text="Հայերեն", callback_data=f"{LANG}:hy")],
        ]
    )


def main_menu(
    topic_titles: list[str], consultation_label: str, appointments_label: str
) -> ReplyKeyboardMarkup:
    """§13.1 step 3: one button per menu topic, plus Consultation and My
    appointments."""
    rows = [[KeyboardButton(text=title)] for title in topic_titles]
    rows.append([KeyboardButton(text=consultation_label)])
    rows.append([KeyboardButton(text=appointments_label)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def negotiation_keyboard(request_id: int, labels: dict[str, str]) -> InlineKeyboardMarkup:
    """§13.1 step 9: the proposal's own buttons, offered again.

    Same callback data the notification carries (`<action>:<request_id>`), so
    the router answers it with the handler that already exists.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=labels[action], callback_data=f"{action}:{request_id}")
                for action in (ACCEPT, COUNTER, DECLINE)
            ]
        ]
    )


def timezone_keyboard(options: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """§13.1 step 6: a therapist-curated list, since Telegram gives no automatic
    source for a client's timezone.

    `options` is `(iana_name, display_name)`. The callback carries the row id
    rather than the name -- `America/Los_Angeles` plus a prefix is a lot of the
    64-byte budget.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=display, callback_data=f"{TZ}:{iana}")]
            for iana, display in options
        ]
    )


def slot_keyboard(
    slots: list[SlotView],
    tz: str,
    day_labels: dict[str, str],
    *,
    action: str = SLOT,
    prefix: str = "",
    extra: list[list[tuple[str, str]]] | None = None,
) -> InlineKeyboardMarkup:
    """§13.1 step 6: grouped by day, times in the client's timezone.

    A day header row is a disabled-looking button rather than a message, so the
    whole picker stays one editable message.

    `day_labels` maps `YYYY-MM-DD` to the heading for that day. They arrive
    already written because this module is synchronous and holds no session,
    and a translated name needs both (§15) -- which is why the headings used to
    come out of `strftime` in English whatever the client's language was.

    `action` and `prefix` let the same picker mean two things (§13.1): booking a
    slot, and suggesting one in answer to a proposal. `extra` appends rows
    beneath -- the waitlist button, where §12.1's gate leaves nothing else to
    offer.
    """
    zone = ZoneInfo(tz)
    by_day: dict[str, list[SlotView]] = defaultdict(list)
    for slot in slots:
        by_day[slot.starts_at_utc.astimezone(zone).strftime("%Y-%m-%d")].append(slot)

    rows: list[list[InlineKeyboardButton]] = []
    for day in sorted(by_day):
        rows.append(
            [InlineKeyboardButton(text=day_labels.get(day, day), callback_data="noop")]
        )
        times = by_day[day]
        # Three times per row keeps the buttons readable on a phone.
        for start in range(0, len(times), 3):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=slot.starts_at_utc.astimezone(zone).strftime("%H:%M"),
                        callback_data=f"{action}:{prefix}{slot.id}",
                    )
                    for slot in times[start : start + 3]
                ]
            )
    for row in extra or ():
        rows.append([InlineKeyboardButton(text=label, callback_data=data) for label, data in row])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def choice_keyboard(action: str, options: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """`(value, label)` pairs as one button per row."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"{action}:{value}")]
            for value, label in options
        ]
    )


def skip_keyboard(label: str) -> InlineKeyboardMarkup:
    """§13.1 step 7: optional answers are skippable."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=SKIP)]]
    )


def parse_callback(data: str) -> tuple[str, str]:
    """`<action>:<argument>` -> `(action, argument)`."""
    action, _, argument = data.partition(":")
    return action, argument
