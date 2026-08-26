"""The health signal (IMPLEMENTATION.md §16.8-§16.10, DESIGN.md §22).

Runs every pass, composes the database checks from `app/core/services/status.py`
with the filesystem ones that do not belong in the domain, and writes the
result to `STATE_PATH/status.json`.

The file rather than a table is the whole design (DESIGN.md §22.3): the most
consequential thing a check can discover is that the database is unreachable,
and a checker reporting into the database cannot report that. It also makes
worker liveness free -- the reader treats a stale file as a dead worker, so
nothing here has to prove it is alive.

This job **MUST NOT** raise. A failing check is a `warn` inside the file; a job
that stops writing the file would be indistinguishable from a dead worker, and
would raise a false alarm about itself.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.enums import CheckState
from app.core.policies import now_utc
from app.core.services.notifications import alert_admin_health
from app.core.services.status import Check, ok, run_checks
from app.db import unit_of_work

logger = logging.getLogger(__name__)

STATUS_FILENAME = "status.json"

#: §16.8. Bumped only when a reader would misread the previous shape.
STATUS_FORMAT_VERSION = 1

#: §16.9's filesystem thresholds.
DUMP_WARN_AGE = timedelta(hours=36)
DUMP_FAIL_AGE = timedelta(days=7)
DISK_WARN_RATIO = 0.15
DISK_WARN_BYTES = 2 * 1024**3
DISK_FAIL_RATIO = 0.05
DISK_FAIL_BYTES = 500 * 1024**2

#: §16.10. While the state stays `fail`, at most one further notification per
#: this long -- a fault that takes a day to fix must not cost the therapist a
#: message every minute.
NOTIFY_FLOOR = timedelta(hours=6)

DUMP_GLOB = "psychobooking-*.dump"
VERIFY_MARKER = ".last-verify"


def status_path() -> Path:
    return Path(get_settings().state_path) / STATUS_FILENAME


async def write_status() -> None:
    """One pass: check, write, and tell the therapist if it just went red."""
    try:
        previous = _read_previous()
        checks = await _collect()
        state = CheckState.worst(check.state for check in checks)
        notified_state, notified_at = await _maybe_notify(state, checks, previous)
        _write(state, checks, notified_state, notified_at)
    except Exception:
        # §16.8: never raise. The loop catches this too, but a status job that
        # relied on that would stop writing the file, which reads to everyone
        # downstream as a dead worker.
        logger.exception("status job failed")


async def _collect() -> list[Check]:
    checks: list[Check] = []

    try:
        async with unit_of_work() as session:
            checks.extend(await run_checks(session, code_head=_code_head()))
    except Exception as exc:
        # The database is the thing that broke. This is exactly the case the
        # file exists for (DESIGN.md §22.3), so it is reported, not raised.
        logger.warning("health checks could not reach the database: %s", type(exc).__name__)
        checks.append(
            Check(
                "database",
                CheckState.fail,
                "The service cannot reach its database. Nothing can be booked or sent. "
                "Call for help now.",
                f"database: {type(exc).__name__}",
            )
        )

    checks.extend(_filesystem_checks())
    return checks


def _code_head() -> str | None:
    """The revision this code expects, read from `alembic/versions`.

    Deployment machinery, so it happens here rather than in the core service
    that compares it (§16.9).
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config("alembic.ini"))
        heads = script.get_heads()
        return heads[0] if len(heads) == 1 else None
    except Exception as exc:
        logger.warning("could not read the code's alembic head: %s", type(exc).__name__)
        return None


# --- Filesystem -------------------------------------------------------------


def _filesystem_checks() -> list[Check]:
    directory = Path(get_settings().backup_path)
    dumps = _dumps(directory)
    return [
        _backup_fresh(dumps),
        _backup_verified(directory),
        _disk_space(directory),
    ]


def _dumps(directory: Path) -> list[tuple[str, datetime]]:
    """Newest first. Unreadable is empty, and the checks below say so."""
    try:
        entries = sorted(directory.glob(DUMP_GLOB), key=lambda p: p.name, reverse=True)
    except OSError:
        return []

    found: list[tuple[str, datetime]] = []
    for entry in entries:
        try:
            found.append(
                (entry.name, datetime.fromtimestamp(entry.stat().st_mtime, tz=now_utc().tzinfo))
            )
        except OSError:
            continue
    return found


def _backup_fresh(dumps: list[tuple[str, datetime]]) -> Check:
    if not dumps:
        return Check(
            "backup_fresh",
            CheckState.fail,
            "There are no backups at all. Nothing could be recovered if the server "
            "were lost. Call for help.",
            "backups: none found",
        )

    name, taken = dumps[0]
    age = now_utc() - taken
    hours = int(age.total_seconds() // 3600)

    if age >= DUMP_FAIL_AGE:
        return Check(
            "backup_fresh",
            CheckState.fail,
            f"The last backup is {age.days} days old. Anything since then would be "
            f"lost. Call for help.",
            f"backups: newest {name}, age {hours}h, fail at {DUMP_FAIL_AGE.days}d",
        )
    if age >= DUMP_WARN_AGE:
        return Check(
            "backup_fresh",
            CheckState.warn,
            f"The last backup is {age.days} day(s) old. Backups may have stopped.",
            f"backups: newest {name}, age {hours}h, warn at "
            f"{int(DUMP_WARN_AGE.total_seconds() // 3600)}h",
        )
    return ok("backup_fresh", f"newest {name}, age {hours}h")


def _backup_verified(directory: Path) -> Check:
    """`backup.sh` writes the marker after asking `pg_restore` to read what it
    just wrote (§16.6). Dumps that exist and cannot be read are the corruption
    that actually ruins a practice."""
    marker = directory / VERIFY_MARKER
    try:
        line = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return Check(
            "backup_verified",
            CheckState.warn,
            "The self-check could not confirm the last backup is readable.",
            f"backups: no {VERIFY_MARKER} marker",
        )

    verdict, _, name = line.partition(" ")
    if verdict == "ok":
        return ok("backup_verified", f"{name or 'last dump'} verified")

    return Check(
        "backup_verified",
        CheckState.fail,
        "The last backup could not be read back. Backups exist but may be unusable. "
        "Call for help.",
        f"backups: verification {line!r}",
    )


def _disk_space(directory: Path) -> Check:
    try:
        usage = shutil.disk_usage(directory)
    except OSError as exc:
        return Check(
            "disk_space",
            CheckState.warn,
            "The self-check could not read how much disk space is left.",
            f"disk: {type(exc).__name__} on {directory}",
        )

    ratio = usage.free / usage.total if usage.total else 0.0
    gb = usage.free / 1024**3
    detail = f"disk: {gb:.1f} GB free ({ratio:.0%}) on {directory}"

    if ratio < DISK_FAIL_RATIO or usage.free < DISK_FAIL_BYTES:
        return Check(
            "disk_space",
            CheckState.fail,
            "The server is nearly out of disk space. Bookings and backups will start "
            "failing. Call for help now.",
            detail,
        )
    if ratio < DISK_WARN_RATIO or usage.free < DISK_WARN_BYTES:
        return Check(
            "disk_space",
            CheckState.warn,
            "The server is running low on disk space.",
            detail,
        )
    return ok("disk_space", detail)


# --- Notification -----------------------------------------------------------


async def _maybe_notify(
    state: CheckState, checks: list[Check], previous: dict[str, Any]
) -> tuple[str | None, str | None]:
    """§16.10: on the way into `fail`, and on the way back out.

    Returns what to record in the file, so the next pass can tell a transition
    from a continuation without a table.
    """
    was = previous.get("state")
    notified_state = previous.get("notified_state")
    notified_at = _parse(previous.get("notified_at"))

    if state is CheckState.fail:
        entering = was != CheckState.fail.value
        stale = notified_at is None or now_utc() - notified_at >= NOTIFY_FLOOR
        if not entering and not stale:
            return notified_state, _iso(notified_at)

        # §16.10: check ids, never a `detail` string -- those are written for
        # a different reader, under looser rules than §13.4 allows for email.
        failing = ", ".join(check.id for check in checks if check.state is CheckState.fail)
        if await _send(
            "system.health.degraded.admin",
            {
                "state": state.value,
                "checks": failing,
                "url": f"{get_settings().base_url}/admin/status",
            },
        ):
            return state.value, _iso(now_utc())
        return notified_state, _iso(notified_at)

    if notified_state == CheckState.fail.value and state is CheckState.ok:
        if await _send("system.health.recovered.admin", {"state": state.value}):
            return None, None

    return notified_state, _iso(notified_at)


async def _send(intent_key: str, payload: dict[str, Any]) -> bool:
    """A row in the outbox, never a direct send (hard rule 2).

    If the database is unreachable this cannot be written -- which is the case
    §16.10 hands to the external uptime check rather than to code.
    """
    try:
        async with unit_of_work() as session:
            await alert_admin_health(
                session,
                intent_key=intent_key,
                payload=payload,
                dedupe_key=f"health:{payload['state']}:{now_utc():%Y-%m-%dT%H}",
            )
        return True
    except Exception as exc:
        logger.warning("could not queue %s: %s", intent_key, type(exc).__name__)
        return False


# --- The file ---------------------------------------------------------------


def _read_previous() -> dict[str, Any]:
    try:
        loaded = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write(
    state: CheckState,
    checks: list[Check],
    notified_state: str | None,
    notified_at: str | None,
) -> None:
    """Atomic: a temporary name in the same directory, then `rename`. A reader
    must never see a partial file (§16.8)."""
    payload = {
        "written_at": _iso(now_utc()),
        "state": state.value,
        "version": STATUS_FORMAT_VERSION,
        "checks": [check.as_dict() for check in checks],
        "notified_state": notified_state,
        "notified_at": notified_at,
    }

    target = status_path()
    tmp = target.with_name(f".{STATUS_FILENAME}.{os.getpid()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        logger.warning("could not write %s: %s", target, exc)
        tmp.unlink(missing_ok=True)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _parse(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


__all__ = ["STATUS_FILENAME", "status_path", "write_status"]
