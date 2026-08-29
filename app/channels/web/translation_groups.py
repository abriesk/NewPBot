"""How the translations page arranges the catalogue (IMPLEMENTATION.md §15).

Presentation only. Keys are not renamed and nothing outside this page knows
these groups exist -- `get_text` resolves by key, and §15 makes `en.yaml` the
normative catalogue of those names.

The grouping is by *where the therapist sees the text*, which is close to the
key prefixes but not the same. Two of them mislead if left raw: `intent.` means
nothing to her while being the most consequential group -- it is the wording
clients actually receive -- and `request.` sounds like a booking request when it
is really her client's own web page.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Group:
    """One box on the page."""

    slug: str
    title: str
    blurb: str
    prefixes: tuple[str, ...]
    #: Open on arrival. The two she edits most, and nothing else -- seven open
    #: boxes would be the wall of keys this replaces.
    start_open: bool = False


#: Ordered by how likely she is to want it, not alphabetically.
GROUPS: tuple[Group, ...] = (
    Group(
        slug="booking",
        title="Booking conversation",
        blurb="Everything asked while someone books: the format, the timezone, "
        "choosing a time, and the confirmation at the end.",
        prefixes=("booking.",),
        start_open=True,
    ),
    Group(
        slug="messages",
        title="Messages sent to clients",
        blurb="What actually arrives in their Telegram or inbox — proposals, "
        "confirmations, refusals, reminders, sign-in links.",
        prefixes=("intent.",),
        start_open=True,
    ),
    Group(
        slug="pages",
        title="Pages the client sees",
        blurb="Labels a client sees around their own account: their request "
        "page, signing in, the menu buttons, the titles of your topic pages, "
        "and the question the bot asks before joining a booking made by email "
        "to someone's Telegram.",
        prefixes=("request.", "auth.", "menu.", "content.", "merge."),
    ),
    Group(
        slug="waitlist",
        title="Waitlist",
        blurb="What someone reads when there is nothing free to book.",
        prefixes=("waitlist.",),
    ),
    Group(
        slug="email",
        title="Email subjects, footer and calendar file",
        blurb="Kept together on purpose: a subject line is the one part of an "
        "email a stranger can read, and the calendar wording ends up on a "
        "client's lock screen, so these stay neutral (§13.4, §13.5).",
        prefixes=("email.", "calendar."),
    ),
    Group(
        slug="chrome",
        title="Buttons, errors and dates",
        blurb="Small words the service reuses everywhere: Skip, Back, error "
        "lines, and the short weekday and month names.",
        prefixes=("common.", "date.", "lang."),
    ),
    Group(
        slug="admin",
        title="Admin interface",
        blurb="This screen and its neighbours. English only by design, so "
        "there is nothing to translate here.",
        prefixes=("admin.",),
    ),
)

#: Anything a future namespace adds lands here rather than vanishing off the
#: page. A test asserts the catalogue never actually needs it.
OTHER = Group(
    slug="other",
    title="Everything else",
    blurb="Keys that do not belong to a group yet.",
    prefixes=(),
)


@dataclass(frozen=True, slots=True)
class Entry:
    """One editable line."""

    key: str
    value: str
    #: The English wording, shown beside the field on the `ru` and `hy` tabs so
    #: she is not translating a bare key name from memory.
    english: str = ""
    #: No row for this language yet: it falls back to the practice default and
    #: then to English (§15). Expected, not an error -- but worth marking.
    missing: bool = False


@dataclass(slots=True)
class GroupedEntries:
    """A group and the lines that fell into it."""

    group: Group
    entries: list[Entry] = field(default_factory=list)
    missing: int = 0


def group_for(key: str) -> Group:
    for group in GROUPS:
        if key.startswith(group.prefixes):
            return group
    return OTHER


def arrange(entries: list[Entry]) -> list[GroupedEntries]:
    """Sort `entries` into the groups above.

    Order follows `GROUPS`; an empty group is left out entirely, which is what
    hides the admin box on the `ru` and `hy` tabs -- those languages carry no
    `admin.` keys at all (DESIGN.md §11).
    """
    buckets: dict[str, GroupedEntries] = {}
    for entry in entries:
        group = group_for(entry.key)
        bucket = buckets.setdefault(group.slug, GroupedEntries(group=group))
        bucket.entries.append(entry)
        bucket.missing += 1 if entry.missing else 0

    ordered = [buckets[g.slug] for g in GROUPS if g.slug in buckets]
    if OTHER.slug in buckets:
        ordered.append(buckets[OTHER.slug])
    return ordered


__all__ = [
    "GROUPS",
    "OTHER",
    "Entry",
    "Group",
    "GroupedEntries",
    "arrange",
    "group_for",
]
