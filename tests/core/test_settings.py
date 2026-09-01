"""Practice settings (IMPLEMENTATION.md §6.1, §8).

`update_settings` is the single gate: the settings form and the configuration
import (§16.7) both write through it, so what may be saved is asserted here
rather than in either adapter — a rule enforced in one of them is a rule the
other can walk around.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.services.settings import (
    MAX_SOCIAL_LINKS,
    get_practice,
    normalise_social_links,
    update_settings,
)


def test_links_keep_their_order_and_lose_their_whitespace() -> None:
    """The order she puts them in is the order they are shown."""
    links = normalise_social_links(
        [
            {"label": "  Telegram ", "url": " https://t.me/example "},
            {"label": "Instagram", "url": "https://instagram.com/example"},
        ]
    )
    assert links == [
        {"label": "Telegram", "url": "https://t.me/example"},
        {"label": "Instagram", "url": "https://instagram.com/example"},
    ]


def test_the_forms_blank_rows_are_dropped_not_refused() -> None:
    """The settings page posts every row it rendered, blanks included. Three
    empty pairs are how a link is added, not three mistakes."""
    assert normalise_social_links(
        [
            {"label": "Telegram", "url": "https://t.me/example"},
            {"label": "", "url": ""},
            {"label": "   ", "url": ""},
        ]
    ) == [{"label": "Telegram", "url": "https://t.me/example"}]


@pytest.mark.parametrize(
    "half",
    [
        {"label": "Telegram", "url": ""},
        {"label": "", "url": "https://t.me/example"},
    ],
)
def test_half_a_pair_is_refused_in_either_direction(half: dict[str, str]) -> None:
    """A label with nowhere to go, or an address with nothing to click. Both
    reach the footer as something broken, so neither is saved."""
    with pytest.raises(ValueError):
        normalise_social_links([half])


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "t.me/example",  # no scheme: an href a browser reads as a relative path
        "https://",  # a scheme and nothing else
    ],
)
def test_only_a_followable_web_address_is_a_link(url: str) -> None:
    with pytest.raises(ValueError):
        normalise_social_links([{"label": "Somewhere", "url": url}])


def test_a_footer_is_not_a_directory() -> None:
    too_many = [
        {"label": f"Network {n}", "url": f"https://example.test/{n}"}
        for n in range(MAX_SOCIAL_LINKS + 1)
    ]
    with pytest.raises(ValueError):
        normalise_social_links(too_many)


def test_a_list_is_the_only_shape_accepted() -> None:
    """The configuration file is written by hand often enough to be wrong."""
    with pytest.raises(ValueError):
        normalise_social_links({"label": "Telegram", "url": "https://t.me/example"})
    with pytest.raises(ValueError):
        normalise_social_links(["https://t.me/example"])


async def test_the_captcha_difficulty_is_bounded(db: AsyncSession) -> None:
    """§17: below the floor the puzzle is not work, above the ceiling a phone
    is still hashing after its owner has given up. Refused rather than clamped
    — silently correcting the number would leave her trusting a setting she
    cannot see."""
    from app.core.services.settings import CAPTCHA_DIFFICULTY_MAX, CAPTCHA_DIFFICULTY_MIN

    await update_settings(db, captcha_difficulty=CAPTCHA_DIFFICULTY_MIN)
    await update_settings(db, captcha_difficulty=CAPTCHA_DIFFICULTY_MAX)

    with pytest.raises(ValueError):
        await update_settings(db, captcha_difficulty=CAPTCHA_DIFFICULTY_MIN - 1)
    with pytest.raises(ValueError):
        await update_settings(db, captcha_difficulty=CAPTCHA_DIFFICULTY_MAX + 1)

    assert (await get_practice(db)).captcha_difficulty == CAPTCHA_DIFFICULTY_MAX


async def test_update_settings_stores_the_cleaned_links(db: AsyncSession) -> None:
    await update_settings(
        db, social_links=[{"label": " Telegram ", "url": "https://t.me/example"}]
    )
    practice = await get_practice(db)
    assert practice.social_links == [{"label": "Telegram", "url": "https://t.me/example"}]


async def test_update_settings_refuses_a_bad_link_and_writes_nothing(db: AsyncSession) -> None:
    await update_settings(db, social_links=[{"label": "Telegram", "url": "https://t.me/example"}])

    with pytest.raises(ValueError):
        await update_settings(
            db,
            name="Renamed by a refused save",
            social_links=[{"label": "Bad", "url": "javascript:alert(1)"}],
        )

    practice = await get_practice(db)
    # The refusal happens before anything is assigned, so the name it was asked
    # to change in the same call is untouched too.
    assert practice.social_links == [{"label": "Telegram", "url": "https://t.me/example"}]
    assert practice.name != "Renamed by a refused save"
