"""The request page's three-point timeline (NP4b): sent, confirmed, session.

A pure mapping from the request's status to the state of each point, so the page and its test read
the same table. Statuses that end the request without a session have no timeline: a row of empty
dots under "this session was cancelled" would promise something that is not coming.
"""

from __future__ import annotations

POINTS = ("sent", "confirmed", "session")

_STATES: dict[str, tuple[str, str, str]] = {
    "pending": ("done", "now", "todo"),
    "negotiating": ("done", "now", "todo"),
    "confirmed": ("done", "done", "now"),
    "completed": ("done", "done", "done"),
}


def timeline_states(status: str) -> list[tuple[str, str]] | None:
    """[(point, "done" | "now" | "todo")] in order, or `None` when the request ended without one."""
    states = _STATES.get(status)
    return list(zip(POINTS, states, strict=True)) if states else None
