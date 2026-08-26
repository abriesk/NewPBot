"""Reading the status file (IMPLEMENTATION.md §12.2, §16.8).

The web process **does not check anything**. Checking is the worker's job; a
page render here does no filesystem walking, no queries beyond the ones the
page already makes, and no outbound HTTP. It reads one small JSON file that the
worker rewrote within the last minute.

That split is what makes the dot affordable on every admin page, and it is also
what lets the dot be honest about the worker: the file's own `written_at` is the
liveness signal (DESIGN.md §22.3), so a worker that died two minutes ago is red
here without anything having to notice it died.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.enums import CheckState
from app.core.policies import now_utc

logger = logging.getLogger(__name__)

STATUS_FILENAME = "status.json"

#: §16.9's one reader-computed threshold. The worker rewrites the file every
#: pass (default 20s), so three minutes is many missed passes, not a slow one.
STALE_AFTER = timedelta(minutes=3)

#: §16.8: `ok`/`warn`/`fail` are the data; the colours are this line, and this
#: line only.
COLOURS = {CheckState.ok: "green", CheckState.warn: "amber", CheckState.fail: "red"}


@dataclass(frozen=True)
class Reading:
    state: CheckState
    checks: list[dict[str, Any]]
    written_at: datetime | None

    @property
    def colour(self) -> str:
        return COLOURS[self.state]

    @property
    def failing(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if c.get("state") in ("warn", "fail")]

    @property
    def healthy(self) -> bool:
        return self.state is CheckState.ok


def status_file() -> Path:
    return Path(get_settings().state_path) / STATUS_FILENAME


def read_status() -> Reading:
    """The current reading, never raising.

    A missing, unreadable, unparseable or stale file all mean the same thing to
    the person looking at the page -- the background worker is not running --
    so they all produce the same red `worker_alive` check rather than an error
    page. An admin UI that breaks when the health file does would be a poor
    health feature.
    """
    path = status_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        return _worker_down(f"status file unreadable at {path}: {type(exc).__name__}")

    written_at = _parse(payload.get("written_at"))
    if written_at is None:
        return _worker_down("status file has no readable written_at")

    age = now_utc() - written_at
    if age > STALE_AFTER:
        minutes = int(age.total_seconds() // 60)
        return _worker_down(
            f"status file last written {written_at:%Y-%m-%dT%H:%MZ}, {minutes}m ago "
            f"(stale after {int(STALE_AFTER.total_seconds() // 60)}m)",
            written_at=written_at,
        )

    checks = [c for c in payload.get("checks", []) if isinstance(c, dict)]
    alive = {
        "id": "worker_alive",
        "state": CheckState.ok.value,
        "summary": "",
        "detail": f"status written {written_at:%Y-%m-%dT%H:%M:%SZ}",
    }
    state = CheckState.worst(_state(c) for c in checks)
    return Reading(state=state, checks=[alive, *checks], written_at=written_at)


def _worker_down(detail: str, written_at: datetime | None = None) -> Reading:
    check = {
        "id": "worker_alive",
        "state": CheckState.fail.value,
        "summary": (
            "The background service has stopped. Nothing is being sent: no confirmations, "
            "no reminders. Bookings can still be made. Call for help."
        ),
        "detail": detail,
    }
    return Reading(state=CheckState.fail, checks=[check], written_at=written_at)


def _state(check: dict[str, Any]) -> CheckState:
    try:
        return CheckState(check.get("state", "warn"))
    except ValueError:
        return CheckState.warn


def _parse(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = ["COLOURS", "STALE_AFTER", "Reading", "read_status", "status_file"]
