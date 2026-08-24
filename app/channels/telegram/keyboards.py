"""Telegram keyboards (IMPLEMENTATION.md §13.1).

Callback data is `<action>:<argument>` and stays inside 64 bytes; the handler
looks the rest up (§9). Nothing here decides booking rules -- it turns already
made decisions into buttons.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
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
SKIP = "skip"
CANCEL = "cancel"

#: Negotiation actions. These names come from the intent catalogue (§10), not
#: from here: the buttons on a proposal are rendered by app/render/messages.py
#: as `<action>:<request_id>`, and this router has to answer to the same words.
ACCEPT = "accept"
COUNTER = "counter"
DECLINE = "decline"

#: The admin actions §13.2 keeps on the phone.
APPROVE = "approve"
PROPOSE = "propose"
REJECT = "reject"
CANCEL_REQUEST = "cancelreq"

CLIENT_ACTIONS = frozenset({ACCEPT, COUNTER, DECLINE})
ADMIN_ACTIONS = frozenset({APPROVE, PROPOSE, REJECT, CANCEL_REQUEST})


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


def main_menu(topic_titles: list[str], consultation_label: str) -> ReplyKeyboardMarkup:
    """§13.1 step 3: one button per menu topic, plus Consultation."""
    rows = [[KeyboardButton(text=title)] for title in topic_titles]
    rows.append([KeyboardButton(text=consultation_label)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


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


def slot_keyboard(slots: list[SlotView], tz: str) -> InlineKeyboardMarkup:
    """§13.1 step 6: grouped by day, times in the client's timezone.

    A day header row is a disabled-looking button rather than a message, so the
    whole picker stays one editable message.
    """
    zone = ZoneInfo(tz)
    by_day: dict[str, list[SlotView]] = defaultdict(list)
    for slot in slots:
        by_day[slot.starts_at_utc.astimezone(zone).strftime("%Y-%m-%d")].append(slot)

    rows: list[list[InlineKeyboardButton]] = []
    for day in sorted(by_day):
        rows.append([InlineKeyboardButton(text=_day_label(day), callback_data="noop")])
        times = by_day[day]
        # Three times per row keeps the buttons readable on a phone.
        for start in range(0, len(times), 3):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=slot.starts_at_utc.astimezone(zone).strftime("%H:%M"),
                        callback_data=f"{SLOT}:{slot.id}",
                    )
                    for slot in times[start : start + 3]
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _day_label(day: str) -> str:
    # A calendar date, not an instant: the zone was already applied when the
    # key was built, so attaching one here would be meaningless.
    parsed = date.fromisoformat(day)
    return parsed.strftime("%a %d %b")


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
