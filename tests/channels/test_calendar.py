"""iCalendar emitter tests (IMPLEMENTATION.md §13.5).

The encoding rules are the whole risk here: a file that folds a line in the
middle of a Cyrillic letter, or forgets to escape a comma, is one a calendar
either refuses or silently mangles -- and nothing in this system would notice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.render.calendar import CRLF, FOLD_OCTETS, session_ics

START = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 26, 9, 30, tzinfo=UTC)


def _unfold(text: str) -> str:
    """What a reader does first: undo the folding (RFC 5545 §3.1)."""
    return text.replace(f"{CRLF} ", "")


def _properties(text: str) -> dict[str, str]:
    out = {}
    for line in _unfold(text).split(CRLF):
        name, _, value = line.partition(":")
        if value:
            out[name] = value
    return out


def _ics(**kwargs: object) -> str:
    return session_ics(
        **{  # type: ignore[arg-type]
            "uid": "abc@example.test",
            "start": START,
            "duration_min": 60,
            "summary": "Consultation",
            "now": NOW,
            **kwargs,
        }
    )


def test_the_envelope_is_a_published_single_event() -> None:
    props = _properties(_ics())

    assert props["VERSION"] == "2.0"
    # §13.5: never REQUEST, and never an ATTENDEE. Either one turns a copy to
    # keep into an invitation this service could not hear the answer to.
    assert props["METHOD"] == "PUBLISH"
    assert "ATTENDEE" not in props
    assert props["SEQUENCE"] == "0"
    assert _ics().count("BEGIN:VEVENT") == 1


def test_times_are_utc_with_no_timezone_block() -> None:
    props = _properties(_ics())

    assert props["DTSTART"] == "20260901T140000Z"
    assert props["DTEND"] == "20260901T150000Z"
    assert props["DTSTAMP"] == "20260826T093000Z"
    assert "VTIMEZONE" not in _ics()
    assert "TZID" not in _ics()


def test_a_local_start_is_converted_rather_than_written_as_it_stands() -> None:
    """Hard rule 4 in the one place it could quietly go wrong."""
    yerevan = START.astimezone(ZoneInfo("Asia/Yerevan"))

    assert _properties(_ics(start=yerevan))["DTSTART"] == "20260901T140000Z"


def test_the_duration_sets_the_end() -> None:
    assert _properties(_ics(duration_min=90))["DTEND"] == "20260901T153000Z"


def test_every_line_ends_crlf() -> None:
    text = _ics()

    assert text.endswith(CRLF)
    assert "\n" not in text.replace(CRLF, "")


def test_text_values_are_escaped() -> None:
    summary = "Session, part one; a\\b\nsecond line"

    line = _unfold(_ics(summary=summary)).split(CRLF)
    body = next(x for x in line if x.startswith("SUMMARY:"))

    assert body == "SUMMARY:Session\\, part one\\; a\\\\b\\nsecond line"


def test_long_lines_fold_within_the_octet_limit() -> None:
    text = _ics(summary="word " * 60)

    for line in text.split(CRLF):
        assert len(line.encode()) <= FOLD_OCTETS
    assert f"{CRLF} " in text


def test_folding_never_splits_a_character() -> None:
    """Armenian is three octets a letter, Russian two: the character-count
    version of this fold is wrong for both, and emits invalid UTF-8.

    Unfolding back to exactly the input is the assertion -- a fold placed
    inside a multi-byte sequence would not decode at all.
    """
    for summary in ("Խորհրդատվություն " * 8, "Консультация " * 10):
        text = _ics(summary=summary)

        for line in text.split(CRLF):
            assert len(line.encode()) <= FOLD_OCTETS
        assert _properties(text)["SUMMARY"] == summary


def test_location_is_omitted_when_there_is_none() -> None:
    assert "LOCATION" not in _properties(_ics(location=""))
    assert _properties(_ics(location="Online"))["LOCATION"] == "Online"
