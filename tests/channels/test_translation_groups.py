"""How the translations page arranges the catalogue (§15).

Presentation only: these assert the arrangement, never the key names, which
en.yaml owns.
"""

from __future__ import annotations

from app.channels.web.translation_groups import GROUPS, OTHER, Entry, arrange, group_for
from app.seed import load_locale_catalogue


def test_every_key_in_the_catalogue_has_a_group() -> None:
    """A key no group claims would vanish off the page rather than appear
    ungrouped, which is a worse failure than an ugly heading."""
    homeless = sorted(
        key for key in load_locale_catalogue()["en"] if group_for(key) is OTHER
    )
    assert not homeless, f"no group claims: {homeless}"


def test_a_key_from_a_namespace_nobody_claimed_still_appears() -> None:
    """The catch-all exists so that adding a namespace and forgetting this file
    is a cosmetic mistake, not a disappearance."""
    grouped = arrange([Entry(key="newthing.hello", value="x")])

    assert [g.group.slug for g in grouped] == [OTHER.slug]
    assert grouped[0].entries[0].key == "newthing.hello"


def test_groups_keep_their_declared_order() -> None:
    entries = [
        Entry(key="admin.nav.settings", value="Settings"),
        Entry(key="booking.ask_name", value="What should I call you?"),
        Entry(key="intent.request.confirmed.client.body", value="Confirmed."),
    ]

    assert [g.group.slug for g in arrange(entries)] == ["booking", "messages", "admin"]


def test_an_empty_group_is_left_out() -> None:
    """Which is what hides the admin box on the ru and hy tabs: those languages
    carry no admin keys at all (DESIGN.md §11)."""
    grouped = arrange([Entry(key="booking.ask_name", value="x")])

    assert [g.group.slug for g in grouped] == ["booking"]


def test_missing_entries_are_counted_per_group() -> None:
    grouped = arrange(
        [
            Entry(key="booking.ask_name", value="", missing=True),
            Entry(key="booking.ask_problem", value="written"),
            Entry(key="email.footer", value="", missing=True),
        ]
    )
    counts = {g.group.slug: g.missing for g in grouped}

    assert counts == {"booking": 1, "email": 1}


def test_the_two_groups_she_edits_most_start_open() -> None:
    open_slugs = [g.slug for g in GROUPS if g.start_open]

    assert open_slugs == ["booking", "messages"]
