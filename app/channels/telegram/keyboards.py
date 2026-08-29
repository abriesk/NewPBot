"""Telegram keyboards (IMPLEMENTATION.md §13.1).

Callback data is `<action>:<argument>` and stays inside 64 bytes; the handler
looks the rest up (§9). Nothing here decides booking rules -- it turns already
made decisions into buttons.
"""

from __future__ import annotations

from calendar import month_name, monthrange
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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

#: The same way out, offered under the booking picker. Its own action rather
#: than `COUNTER_WAITLIST`: that one closes a request that already exists, while
#: this one belongs to somebody who has not made one -- times can all be wrong
#: for a client without being absent, and until this existed the only answer to
#: that was to close the app.
WAITLIST = "wait"

#: §12.1's picker, the client's half of §13.2's. Same three screens, their own
#: language, their own timezone, and never a word about the therapist's diary.
COUNTER_MONTHS = "cm"
COUNTER_DAYS = "cmd"
COUNTER_HOURS = "cd"
COUNTER_AT = "ch"

#: The admin actions §13.2 keeps on the phone.
APPROVE = "approve"
PROPOSE = "propose"
REJECT = "reject"
CANCEL_REQUEST = "cancelreq"

#: §13.2's propose picker: month -> day -> hour, and the slots offered above it.
#: Every screen's callback carries the whole answer so far, so the picker holds
#: no state and a button tapped in an old message still means what it said.
PROPOSE_SLOT = "pslot"
PROPOSE_MONTHS = "pm"
PROPOSE_DAYS = "pmd"
PROPOSE_HOURS = "pd"
PROPOSE_AT = "ph"
#: What the therapist presses to type a time instead. §7.1 still allows a
#: proposal of words, and `18:30` is not on an hour grid.
PROPOSE_TYPE = "ptype"

#: A button that is there to be read, not pressed -- a day heading, or an hour
#: §13.2 says is taken. The webhook answers every callback carrying an id, so
#: these resolve rather than leaving the therapist's client spinning.
NOOP = "noop"

#: Telegram inline keyboards have no colour and no rendered disabled state, so a
#: label is the only way to say "there is something here already".
TAKEN_MARK = "✕"

#: §13.2's panel navigation. Short names: the argument shares the 64 bytes.
PANEL = "apanel"
PANEL_REQUESTS = "areq"
PANEL_OPEN = "aopen"
PANEL_SESSIONS = "asess"
PANEL_WAITLIST = "awl"
PANEL_AVAILABILITY = "aavail"
PANEL_SKIP = "askip"

CLIENT_ACTIONS = frozenset(
    {
        ACCEPT,
        COUNTER,
        DECLINE,
        COUNTER_SLOT,
        COUNTER_WAITLIST,
        COUNTER_MONTHS,
        COUNTER_DAYS,
        COUNTER_HOURS,
        COUNTER_AT,
    }
)
ADMIN_ACTIONS = frozenset(
    {
        APPROVE,
        PROPOSE,
        PROPOSE_SLOT,
        PROPOSE_MONTHS,
        PROPOSE_DAYS,
        PROPOSE_HOURS,
        PROPOSE_AT,
        PROPOSE_TYPE,
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


@dataclass(frozen=True, slots=True)
class Picker:
    """The callback names and the words one month -> day -> hour picker is drawn
    with.

    Two exist, and they differ in every way a picker can: §13.2's belongs to the
    therapist, is English because her surface is, and marks the hours she has
    already filled. §12.1's belongs to a client answering a proposal, is written
    in their language, and says **nothing** about her diary -- marking her taken
    hours there would tell a client when other people have sessions, and quietly
    omitting them would say the same thing by the gap it left.

    Words arrive already written because this module is synchronous and holds no
    session, which is the same reason `slot_keyboard` takes its day headings
    that way (§15).
    """

    #: Callback actions, in the order the screens go.
    days: str
    hours: str
    at: str
    months: str
    #: Complete callback data for backing out altogether.
    cancel: str
    #: How far ahead to offer. Two for a client, three for the therapist: a
    #: suggestion four months out is not one she can act on.
    months_ahead: int
    month_names: Mapping[int, str]
    #: Seven, Monday first, matching `date.weekday()`.
    weekdays: Sequence[str]
    back_to_months: str
    back_to_days: str
    cancel_label: str


#: §13.2's. English throughout, from `calendar` rather than the catalogue --
#: DESIGN.md §11 never translates the admin surface.
ADMIN_PICKER = Picker(
    days=PROPOSE_DAYS,
    hours=PROPOSE_HOURS,
    at=PROPOSE_AT,
    months=PROPOSE_MONTHS,
    cancel=PANEL_OPEN,
    months_ahead=3,
    month_names={number: month_name[number] for number in range(1, 13)},
    weekdays=("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"),
    back_to_months="« Months",
    back_to_days="« Days",
    cancel_label="✕ Cancel",
)


def _dead(label: str) -> InlineKeyboardButton:
    """A button that is there to be read. See `NOOP`."""
    return InlineKeyboardButton(text=label, callback_data=NOOP)


def _cancel(picker: Picker, request_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=picker.cancel_label, callback_data=f"{picker.cancel}:{request_id}"
    )


def months_keyboard(picker: Picker, request_id: int, today: date) -> InlineKeyboardMarkup:
    """Screen one: this month and the next `months_ahead - 1`."""
    rows: list[list[InlineKeyboardButton]] = []
    year, month = today.year, today.month
    for _ in range(picker.months_ahead):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{picker.month_names[month]} {year}",
                    callback_data=f"{picker.days}:{request_id}:{year:04d}-{month:02d}",
                )
            ]
        )
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    rows.append([_cancel(picker, request_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def days_keyboard(
    picker: Picker, request_id: int, year: int, month: int, *, today: date
) -> InlineKeyboardMarkup:
    """Screen two: that month's days, Monday first.

    A day already past is dead rather than missing, so the grid keeps the shape
    of a calendar instead of starting halfway through a row somewhere. A whole
    week already gone is dropped, though: late in a month the alignment was
    being paid for with five rows of dots and four live days.
    """
    first = date(year, month, 1)
    _, length = monthrange(year, month)

    weeks: list[list[InlineKeyboardButton]] = []
    week: list[InlineKeyboardButton] = [_dead(" ") for _ in range(first.weekday())]

    for number in range(1, length + 1):
        day = date(year, month, number)
        week.append(
            _dead("·")
            if day < today
            else InlineKeyboardButton(
                text=str(number),
                callback_data=f"{picker.hours}:{request_id}:{day.isoformat()}",
            )
        )
        if len(week) == 7:
            weeks.append(week)
            week = []

    if week:
        weeks.append(week + [_dead(" ") for _ in range(7 - len(week))])

    def _live(row: list[InlineKeyboardButton]) -> bool:
        return any(button.callback_data != NOOP for button in row)

    while weeks and not _live(weeks[0]):
        weeks.pop(0)

    rows: list[list[InlineKeyboardButton]] = [[_dead(day) for day in picker.weekdays], *weeks]
    rows.append(
        [
            InlineKeyboardButton(
                text=picker.back_to_months,
                callback_data=f"{picker.months}:{request_id}",
            ),
            _cancel(picker, request_id),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hours_keyboard(
    picker: Picker, request_id: int, day: date, taken: frozenset[int] = frozenset()
) -> InlineKeyboardMarkup:
    """Screen three: all twenty-four hours of `day`.

    Never filtered to working hours: none are stored, a practice's are flexible
    month to month, and a model of them would hide hours she can actually work.

    `taken` is dead **in place** and marked, so the absence reads as "there is
    something there" rather than as a gap. It is empty for a client, and must
    stay so -- see `Picker`.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for start in range(0, 24, 6):
        rows.append(
            [
                _dead(f"{TAKEN_MARK}{hour:02d}")
                if hour in taken
                else InlineKeyboardButton(
                    text=f"{hour:02d}",
                    callback_data=f"{picker.at}:{request_id}:{day.isoformat()}T{hour:02d}",
                )
                for hour in range(start, start + 6)
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text=picker.back_to_days,
                callback_data=f"{picker.days}:{request_id}:{day.year:04d}-{day.month:02d}",
            ),
            _cancel(picker, request_id),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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

    **A day offering one time is one button**, carrying the day and the time
    together, rather than a heading with a single time under it. Reported in
    use: the heading is a `NOOP` -- there to be read -- and on a one-slot day it
    is the wider, upper half of what looks like one control, so it is what gets
    tapped, and tapping it does nothing at all. That reads as a broken bot
    rather than as a label. A toast on the heading was the other candidate and
    is not needed here: where the ambiguity exists there is now no heading to
    tap, and where a heading remains the times are visibly plural beneath it.
    """
    zone = ZoneInfo(tz)
    by_day: dict[str, list[SlotView]] = defaultdict(list)
    for slot in slots:
        by_day[slot.starts_at_utc.astimezone(zone).strftime("%Y-%m-%d")].append(slot)

    rows: list[list[InlineKeyboardButton]] = []
    for day in sorted(by_day):
        times = by_day[day]
        heading = day_labels.get(day, day)

        if len(times) == 1:
            only = times[0]
            rows.append(
                [
                    InlineKeyboardButton(
                        text=(
                            f"{heading} · "
                            f"{only.starts_at_utc.astimezone(zone).strftime('%H:%M')}"
                        ),
                        callback_data=f"{action}:{prefix}{only.id}",
                    )
                ]
            )
            continue

        rows.append([_dead(heading)])
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
