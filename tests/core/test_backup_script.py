"""The backup sidecar's start-up decision (IMPLEMENTATION.md §16.6; F9).

`backup.sh --decide-startup DIR NOW_EPOCH` prints `dump` or `skip` and exits: the
rule "dump at start when the newest dump's *name* is over 24 h old" as a pure
function of a directory listing and a clock, so it is testable without
Postgres. File mtimes are deliberately ignored -- a folder copy resets them all.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "backup.sh"


def _decide(directory: Path, now: datetime, hour: int | str = 3) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "BACKUP_HOUR_UTC": str(hour),
        "BACKUP_TARGET": str(directory),
    }
    done = subprocess.run(  # noqa: S603
        ["/bin/sh", str(SCRIPT), "--decide-startup", str(directory), str(int(now.timestamp()))],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        check=False,
    )
    return done.stdout.strip()


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    directory = tmp_path / "backups"
    directory.mkdir()
    return directory


def test_an_empty_folder_dumps_at_start(folder: Path) -> None:
    assert _decide(folder, _at("2026-11-13T20:00:00")) == "dump"


def test_a_dump_from_this_morning_is_not_repeated(folder: Path) -> None:
    (folder / "psychobooking-2026-11-13.dump").write_bytes(b"x")
    assert _decide(folder, _at("2026-11-13T20:00:00")) == "skip"


def test_a_date_only_dump_over_24h_old_is_repeated(folder: Path) -> None:
    (folder / "psychobooking-2026-11-13.dump").write_bytes(b"x")
    # Dated the 13th, taken at the 03:00 schedule hour: 25 h before this.
    assert _decide(folder, _at("2026-11-14T04:00:00")) == "dump"


def test_a_timed_name_is_read_to_the_second(folder: Path) -> None:
    (folder / "psychobooking-2026-11-13-100000.dump").write_bytes(b"x")
    assert _decide(folder, _at("2026-11-14T09:59:59")) == "skip"
    assert _decide(folder, _at("2026-11-14T10:00:01")) == "dump"


def test_mtimes_are_ignored(folder: Path) -> None:
    for day in ("2026-09-21", "2026-09-22", "2026-09-23"):
        path = folder / f"psychobooking-{day}.dump"
        path.write_bytes(b"x")  # mtime is now, as after a folder copy
    assert _decide(folder, datetime.now(UTC)) == "dump"


def test_the_newest_name_decides_not_the_listing_order(folder: Path) -> None:
    (folder / "psychobooking-2026-09-23.dump").write_bytes(b"x")
    (folder / "psychobooking-2026-11-13.dump").write_bytes(b"x")
    assert _decide(folder, _at("2026-11-13T20:00:00")) == "skip"


def test_a_timed_dump_beats_a_date_only_one_from_the_same_day(folder: Path) -> None:
    """`sort` on the names puts `...-13-100000.dump` before `...-13.dump`; the
    newest by time is the timed one (10:00), not the date-only (03:00)."""
    (folder / "psychobooking-2026-11-13.dump").write_bytes(b"x")
    (folder / "psychobooking-2026-11-13-100000.dump").write_bytes(b"x")
    assert _decide(folder, _at("2026-11-14T09:00:00")) == "skip"


def test_a_date_only_dump_beats_an_earlier_timed_one_the_same_day(folder: Path) -> None:
    (folder / "psychobooking-2026-11-13-010000.dump").write_bytes(b"x")
    (folder / "psychobooking-2026-11-13.dump").write_bytes(b"x")  # 03:00 by schedule
    assert _decide(folder, _at("2026-11-14T02:00:00")) == "skip"
    assert _decide(folder, _at("2026-11-14T03:00:01")) == "dump"


@pytest.mark.parametrize("hour", ["08", "09"])
def test_a_zero_padded_hour_is_not_read_as_octal(folder: Path, hour: str) -> None:
    (folder / "psychobooking-2026-11-13.dump").write_bytes(b"x")  # counts from hour:00
    assert _decide(folder, _at(f"2026-11-14T{int(hour) - 1:02d}:59:59"), hour) == "skip"
    assert _decide(folder, _at(f"2026-11-14T{hour}:00:01"), hour) == "dump"
