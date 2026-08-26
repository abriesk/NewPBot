"""Reading the backup directory (IMPLEMENTATION.md §16.6, §12.2).

The dumps are produced by the `backup` sidecar, not by this process: `web`
mounts the same directory read-only and does nothing here but list it and hand
a file back. There is deliberately no way to create, overwrite, or delete a
dump through the web surface (DESIGN.md §21.5).

The filename is the only untrusted input, and it is matched against a fixed
pattern rather than sanitised. A pattern that accepts nothing but what the
sidecar writes cannot be talked into `../`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Exactly what §16.6's script writes: a date, and a time only when a second
#: run lands on the same day. The in-progress temporary file the script writes
#: before `mv` does not match, so a half-written dump is never listed and never
#: served.
DUMP_PATTERN = re.compile(r"^psychobooking-\d{4}-\d{2}-\d{2}(-\d{6})?\.dump$")


@dataclass(frozen=True)
class Dump:
    name: str
    size_bytes: int
    modified_at: datetime

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


def backup_dir() -> Path:
    return Path(get_settings().backup_path)


def list_dumps() -> list[Dump]:
    """Newest first. A missing or unreadable directory is empty, not an error:
    a deployment that has not run its first backup yet still has a working
    admin page."""
    directory = backup_dir()
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        logger.warning("backup directory %s is not readable: %s", directory, exc)
        return []

    dumps: list[Dump] = []
    for entry in entries:
        if not DUMP_PATTERN.match(entry.name):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if not entry.is_file():
            continue
        dumps.append(
            Dump(
                name=entry.name,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            )
        )

    return sorted(dumps, key=lambda dump: dump.name, reverse=True)


def resolve_dump(name: str) -> Path | None:
    """The path to serve, or None if the name is not one of ours.

    Both checks matter: the pattern rejects traversal and anything the sidecar
    did not write, and the parent comparison catches the case where the
    directory itself contains a symlink out.
    """
    if not DUMP_PATTERN.match(name):
        return None

    directory = backup_dir()
    path = directory / name
    try:
        resolved = path.resolve()
        if resolved.parent != directory.resolve():
            return None
        if not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


__all__ = ["DUMP_PATTERN", "Dump", "backup_dir", "list_dumps", "resolve_dump"]
