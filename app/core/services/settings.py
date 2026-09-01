"""Practice settings (IMPLEMENTATION.md §8).

One practice is served. `get_practice` is the single way the rest of the core
reaches it, so no service has to decide what "the practice" means.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.models import Practice

#: Settings the admin UI may change. Anything not listed here is either an
#: environment variable (§4) or not settable at all.
MUTABLE_FIELDS = frozenset(
    {
        "name",
        "default_language",
        "timezone",
        "clinic_onsite_url",
        "online_only",
        "online_meeting_url",
        "availability_on",
        "booking_mode",
        "fallback_to_negotiation",
        "negotiation_enabled",
        "auto_confirm_slots",
        "slot_hold_minutes",
        "pending_expiry_hours",
        "cancel_window_hours",
        "reminder_offsets_min",
        "retention_months",
        "social_links",
        "captcha_on",
        "captcha_difficulty",
    }
)

#: §17: leading zero bits of SHA-256 the browser must find before the booking
#: and waitlist forms are accepted. Bounded here because the cost doubles with
#: every step: below the floor the puzzle is not work at all, and above the
#: ceiling a phone is still hashing long after its owner has given up. Refused
#: rather than clamped -- silently correcting a number she typed would leave
#: her believing a setting she cannot see.
CAPTCHA_DIFFICULTY_MIN = 8
CAPTCHA_DIFFICULTY_MAX = 24

#: §6.1. A footer, not a directory: past about this many the row stops reading
#: as "where else to find me" and starts reading as a menu.
MAX_SOCIAL_LINKS = 8
SOCIAL_LABEL_MAX = 40
SOCIAL_URL_MAX = 300

#: Only what a browser will follow as a page. `mailto:` and `tel:` are contact
#: details rather than links to a profile, and belong wherever contact details
#: end up; `javascript:` is why this list exists at all.
SOCIAL_SCHEMES = ("http", "https")


def normalise_social_links(value: Any) -> list[dict[str, str]]:
    """The footer's links, cleaned and checked (§6.1, §12.1).

    Whitespace is stripped, pairs that are entirely empty are dropped -- the
    settings form always posts its blank rows -- and order is kept, because the
    order she puts them in is the order they are shown.

    Raises `ValueError`, which is what both callers already answer for: the
    admin route turns it into the flash on the settings page, and the config
    import turns it into `ConfigInvalid` naming the file's fault.
    """
    if not isinstance(value, list):
        raise ValueError("social_links: expected a list")

    links: list[dict[str, str]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"social_links[{index}]: expected an object with label and url")

        label = str(raw.get("label", "") or "").strip()
        url = str(raw.get("url", "") or "").strip()
        if not label and not url:
            continue

        # Half a pair is a mistake in either direction: a labelled link with
        # nowhere to go, or an address with nothing to click.
        if not label:
            raise ValueError(f"social_links[{index}]: {url!r} has no label")
        if not url:
            raise ValueError(f"social_links[{index}]: {label!r} has no address")

        if len(label) > SOCIAL_LABEL_MAX:
            raise ValueError(f"social_links[{index}]: label is longer than {SOCIAL_LABEL_MAX}")
        if len(url) > SOCIAL_URL_MAX:
            raise ValueError(f"social_links[{index}]: address is longer than {SOCIAL_URL_MAX}")

        parsed = urlparse(url)
        if parsed.scheme.lower() not in SOCIAL_SCHEMES or not parsed.netloc:
            raise ValueError(
                f"social_links[{index}]: {url!r} is not a http(s) address "
                "(a link needs the https:// in front of it)"
            )

        links.append({"label": label, "url": url})

    if len(links) > MAX_SOCIAL_LINKS:
        raise ValueError(f"social_links: at most {MAX_SOCIAL_LINKS} links")
    return links


async def get_practice(session: AsyncSession) -> Practice:
    practice = (
        await session.execute(select(Practice).order_by(Practice.id).limit(1))
    ).scalar_one_or_none()
    if practice is None:
        raise NotFound("no practice row; run the seed")
    return practice


async def update_settings(session: AsyncSession, **changes: Any) -> Practice:
    """Apply admin settings changes. Unknown or immutable fields are refused
    rather than silently ignored."""
    unknown = set(changes) - MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"not settable: {sorted(unknown)}")

    # Here rather than in the adapters: the settings form and the config import
    # both write through this function, and a rule enforced in one of them is a
    # rule the other can walk around.
    if "social_links" in changes:
        changes["social_links"] = normalise_social_links(changes["social_links"])

    if "captcha_difficulty" in changes:
        difficulty = int(changes["captcha_difficulty"])
        if not CAPTCHA_DIFFICULTY_MIN <= difficulty <= CAPTCHA_DIFFICULTY_MAX:
            raise ValueError(
                f"captcha_difficulty: {difficulty} is outside "
                f"{CAPTCHA_DIFFICULTY_MIN}-{CAPTCHA_DIFFICULTY_MAX}"
            )
        changes["captcha_difficulty"] = difficulty

    practice = await get_practice(session)
    for field, value in changes.items():
        setattr(practice, field, value)
    await session.flush()
    return practice
