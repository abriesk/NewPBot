"""iCalendar emitter (IMPLEMENTATION.md §13.5).

One confirmed session becomes one `VEVENT` the client can add to whatever
calendar they keep. `METHOD:PUBLISH` and no `ATTENDEE`, deliberately: the
invitation form would let them accept or decline inside their calendar, and
nothing here would ever hear the answer.

RFC 5545 is a large specification and this uses a corner of it. The three parts
that actually bite are all below: CRLF line endings, folding at 75 octets, and
escaping in text values. Times are emitted in UTC (`...Z`) so no `VTIMEZONE`
block is needed and every client converts to its own viewer's zone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: What the email attachment is called and what media type it carries.
ICS_FILENAME = "session.ics"
ICS_SUBTYPE = "calendar"

#: RFC 5545 §3.7.3. Identifies the software, not the practice -- it is not a
#: place to name a therapist.
PRODID = "-//NewPBot//Booking//EN"

#: RFC 5545 §3.1: lines SHOULD NOT exceed 75 octets, excluding the line break.
#: A continuation begins with one space, which counts towards its own 75.
FOLD_OCTETS = 75

CRLF = "\r\n"


def _escape(value: str) -> str:
    """RFC 5545 §3.3.11. Backslash first, or it doubles the other escapes."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """Break a content line at 75 octets, never mid-character.

    The limit is in octets, not characters, and Russian and Armenian summaries
    are two and three bytes a letter -- so the naive character-count version of
    this is wrong for every language this service speaks except English.
    """
    raw = line.encode()
    if len(raw) <= FOLD_OCTETS:
        return line

    chunks: list[str] = []
    start = 0
    limit = FOLD_OCTETS
    while start < len(raw):
        end = min(start + limit, len(raw))
        # 0b10xxxxxx is a UTF-8 continuation byte: walk back to the lead byte
        # rather than splitting a character across two lines.
        while end < len(raw) and raw[end] & 0xC0 == 0x80:
            end -= 1
        chunks.append(raw[start:end].decode())
        start = end
        limit = FOLD_OCTETS - 1  # the leading space costs one octet

    return f"{CRLF} ".join(chunks)


def _stamp(value: datetime) -> str:
    """A UTC date-time, RFC 5545 form. Never a local time, never a `TZID`."""
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def session_ics(
    *,
    uid: str,
    start: datetime,
    duration_min: int,
    summary: str,
    location: str = "",
    now: datetime | None = None,
) -> str:
    """The whole file, as text.

    `uid` MUST be stable for a given booking (§13.5): an outbox retry then
    updates the entry the client already added instead of leaving them with two.
    """
    end = start + timedelta(minutes=duration_min)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{_escape(uid)}",
        f"DTSTAMP:{_stamp(now or datetime.now(UTC))}",
        f"DTSTART:{_stamp(start)}",
        f"DTEND:{_stamp(end)}",
        "SEQUENCE:0",
        f"SUMMARY:{_escape(summary)}",
    ]
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]

    return CRLF.join(_fold(line) for line in lines) + CRLF
