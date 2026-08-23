"""Booking policy tests (DESIGN.md §6, IMPLEMENTATION.md §8).

Pure functions, so every branch of the §6 matrix is covered without touching a
database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.enums import BookingMode
from app.core.models import Practice
from app.core.policies import (
    BookingPath,
    hold_expiry,
    is_within_cancellation_window,
    pending_expiry,
    reminder_schedule,
    resolve_booking_mode,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _practice(**overrides: object) -> Practice:
    """An unsaved Practice with the §6.1 defaults. Policies never touch the
    database, so this never needs to be persisted."""
    defaults: dict[str, object] = {
        "name": "Practice",
        "default_language": "ru",
        "timezone": "Asia/Yerevan",
        "availability_on": True,
        "booking_mode": BookingMode.slots,
        "fallback_to_negotiation": True,
        "negotiation_enabled": True,
        "auto_confirm_slots": False,
        "slot_hold_minutes": 15,
        "pending_expiry_hours": 48,
        "cancel_window_hours": 24,
        "reminder_offsets_min": [1440, 60],
        "retention_months": 12,
    }
    return Practice(**{**defaults, **overrides})


# --- The §6 matrix, row by row ----------------------------------------------


@pytest.mark.parametrize("slots_exist", [True, False])
@pytest.mark.parametrize("mode", [BookingMode.slots, BookingMode.negotiation])
def test_availability_off_always_means_waitlist(mode: BookingMode, slots_exist: bool) -> None:
    practice = _practice(availability_on=False, booking_mode=mode)
    assert resolve_booking_mode(practice, slots_exist=slots_exist).path is BookingPath.waitlist


def test_slots_mode_with_slots_offers_the_picker() -> None:
    result = resolve_booking_mode(_practice(), slots_exist=True)
    assert result.path is BookingPath.slots
    assert result.reason == "slots_available"


def test_slots_mode_without_slots_falls_back_to_negotiation() -> None:
    result = resolve_booking_mode(_practice(fallback_to_negotiation=True), slots_exist=False)
    assert result.path is BookingPath.negotiation
    assert result.reason == "no_slots_fallback"


def test_slots_mode_without_slots_or_fallback_offers_the_waitlist() -> None:
    result = resolve_booking_mode(_practice(fallback_to_negotiation=False), slots_exist=False)
    assert result.path is BookingPath.waitlist
    assert result.reason == "no_slots_no_fallback"


@pytest.mark.parametrize("slots_exist", [True, False])
def test_negotiation_mode_goes_straight_to_free_text(slots_exist: bool) -> None:
    practice = _practice(booking_mode=BookingMode.negotiation)
    assert resolve_booking_mode(practice, slots_exist=slots_exist).path is BookingPath.negotiation


def test_negotiation_disabled_never_yields_a_negotiation_path() -> None:
    """The interpretation flagged in policies.resolve_booking_mode: offering a
    free-text request the therapist has switched off would produce a request
    nobody can answer."""
    disabled = _practice(booking_mode=BookingMode.negotiation, negotiation_enabled=False)
    assert resolve_booking_mode(disabled, slots_exist=False).path is BookingPath.waitlist

    no_fallback = _practice(negotiation_enabled=False)
    assert resolve_booking_mode(no_fallback, slots_exist=False).path is BookingPath.waitlist


# --- Expiry -----------------------------------------------------------------


def test_hold_and_pending_expiry_use_the_configured_windows() -> None:
    practice = _practice(slot_hold_minutes=15, pending_expiry_hours=48)
    assert hold_expiry(practice, at=NOW) == NOW + timedelta(minutes=15)
    assert pending_expiry(practice, at=NOW) == NOW + timedelta(hours=48)


def test_expiry_windows_are_settings_not_constants() -> None:
    practice = _practice(slot_hold_minutes=5, pending_expiry_hours=12)
    assert hold_expiry(practice, at=NOW) == NOW + timedelta(minutes=5)
    assert pending_expiry(practice, at=NOW) == NOW + timedelta(hours=12)


# --- Reminders --------------------------------------------------------------


def test_reminder_schedule_is_one_entry_per_offset() -> None:
    start = NOW + timedelta(days=3)
    schedule = reminder_schedule(_practice(), start, at=NOW)
    assert [offset for offset, _, _ in schedule] == [1440, 60]
    assert [due for _, due, _ in schedule] == [
        start - timedelta(minutes=1440),
        start - timedelta(minutes=60),
    ]
    assert not any(past for _, _, past in schedule)


def test_an_empty_offset_array_disables_reminders() -> None:
    assert reminder_schedule(_practice(reminder_offsets_min=[]), NOW + timedelta(days=1)) == []


def test_a_reminder_already_due_is_flagged_rather_than_fired_late() -> None:
    """DESIGN.md §13: mark it skipped, do not fire it late."""
    start = NOW + timedelta(minutes=30)  # sooner than the 60-minute offset
    schedule = reminder_schedule(_practice(reminder_offsets_min=[1440, 60]), start, at=NOW)
    past = {offset: already_past for offset, _, already_past in schedule}
    assert past == {1440: True, 60: True}


def test_a_third_offset_needs_no_schema_change() -> None:
    schedule = reminder_schedule(
        _practice(reminder_offsets_min=[10080, 1440, 60]), NOW + timedelta(days=30), at=NOW
    )
    assert [offset for offset, _, _ in schedule] == [10080, 1440, 60]


def test_duplicate_offsets_produce_one_reminder_each() -> None:
    # The (request_id, offset_min) unique constraint would reject the second.
    schedule = reminder_schedule(
        _practice(reminder_offsets_min=[60, 60]), NOW + timedelta(days=1), at=NOW
    )
    assert len(schedule) == 1


# --- Cancellation window ----------------------------------------------------


def test_cancellation_window_compares_against_the_setting() -> None:
    practice = _practice(cancel_window_hours=24)
    assert is_within_cancellation_window(practice, NOW + timedelta(hours=25), at=NOW)
    assert not is_within_cancellation_window(practice, NOW + timedelta(hours=23), at=NOW)
