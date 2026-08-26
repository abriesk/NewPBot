"""The status file and its notifications (IMPLEMENTATION.md §16.8, §16.10).

The properties that matter are about what happens when things are *not* fine:
the file is still written when the database is unreachable, a failing check
never becomes an exception, and a fault that lasts a day does not cost the
therapist a message every minute.

In tests/core because the worker is not a channel -- the same place §14's other
job tests live.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from app.core.enums import CheckState
from app.core.policies import now_utc
from app.core.services.status import Check
from app.worker.jobs import status as job


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from app.config import get_settings

    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv("STATE_PATH", str(directory))
    monkeypatch.setenv("BACKUP_PATH", str(tmp_path / "backups"))
    (tmp_path / "backups").mkdir()
    get_settings.cache_clear()
    try:
        yield directory
    finally:
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def never_really_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    """These call the real `write_status`, which commits to the real database.

    Without this, a test run on an installation whose checks are unhappy queues
    a genuine `system.health.degraded.admin` row -- and the therapist's phone
    buzzes because somebody ran the test suite. The notification tests below
    patch `_send` themselves; this is the default for everything else.
    """

    async def refuse(intent_key: str, payload: dict) -> bool:
        return False  # as if the queue were unavailable: nothing is written

    monkeypatch.setattr(job, "_send", refuse)


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_between_tests() -> AsyncIterator[None]:
    """These call the top-level job, which opens its own `unit_of_work` rather
    than borrowing the test's session -- writing the file is the behaviour
    under test. Each test gets its own event loop, so the engine that job built
    has to go with it, or asyncpg connections are collected on a dead loop."""
    yield
    from app.db import dispose_engine

    await dispose_engine()


def _written() -> dict:
    return json.loads(job.status_path().read_text(encoding="utf-8"))


# --- The file ---------------------------------------------------------------


async def test_a_pass_writes_a_readable_file(state_dir: Path) -> None:
    await job.write_status()

    payload = _written()
    assert payload["version"] == job.STATUS_FORMAT_VERSION
    assert payload["state"] in {"ok", "warn", "fail"}
    assert payload["written_at"]
    assert {c["id"] for c in payload["checks"]} >= {"backup_fresh", "disk_space", "outbox_dead"}


async def test_every_check_carries_both_sentences(state_dir: Path) -> None:
    await job.write_status()

    for check in _written()["checks"]:
        assert set(check) == {"id", "state", "summary", "detail"}
        if check["state"] != "ok":
            assert check["summary"], f"{check['id']} has no sentence for the therapist"


async def test_the_file_is_rewritten_even_when_nothing_changed(state_dir: Path) -> None:
    """Its `written_at` is the worker's liveness signal (§16.8), so a pass that
    finds nothing new must still touch it."""
    await job.write_status()
    first = _written()["written_at"]

    await job.write_status()

    assert _written()["written_at"] != first


async def test_no_partial_file_is_left_behind(state_dir: Path) -> None:
    await job.write_status()

    leftovers = sorted(
        os.listdir(state_dir)  # not Path.iterdir: ASYNC240 bans it in async tests
    )
    leftovers = [name for name in leftovers if name != job.STATUS_FILENAME]
    assert leftovers == []


async def test_the_job_never_raises(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16.8: a status job that stopped writing would be indistinguishable from
    a dead worker, and would raise a false alarm about itself."""

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk gone")

    monkeypatch.setattr(job, "_write", boom)

    await job.write_status()  # must not raise


async def test_an_unreachable_database_is_reported_not_raised(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the file exists for at all (DESIGN.md §22.3)."""

    class Boom:
        async def __aenter__(self) -> None:
            raise OSError("connection refused")

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(job, "unit_of_work", lambda: Boom())

    await job.write_status()

    payload = _written()
    assert payload["state"] == "fail"
    database = next(c for c in payload["checks"] if c["id"] == "database")
    assert "cannot reach its database" in database["summary"]
    # The filesystem checks still ran: losing the database must not cost the
    # other half of the picture.
    assert any(c["id"] == "disk_space" for c in payload["checks"])


# --- Backups ----------------------------------------------------------------


def _dump(tmp: Path, name: str, age: timedelta) -> Path:
    import os

    path = tmp / name
    path.write_bytes(b"PGDMP")
    when = (now_utc() - age).timestamp()
    os.utime(path, (when, when))
    return path


async def test_backup_freshness_is_amber_then_red(state_dir: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"

    assert job._backup_fresh(job._dumps(backups)).state is CheckState.fail  # none at all

    _dump(backups, "psychobooking-2026-08-26.dump", timedelta(hours=1))
    assert job._backup_fresh(job._dumps(backups)).state is CheckState.ok

    _dump(backups, "psychobooking-2026-08-26.dump", job.DUMP_WARN_AGE + timedelta(hours=1))
    assert job._backup_fresh(job._dumps(backups)).state is CheckState.warn

    _dump(backups, "psychobooking-2026-08-26.dump", job.DUMP_FAIL_AGE + timedelta(hours=1))
    assert job._backup_fresh(job._dumps(backups)).state is CheckState.fail


async def test_verification_failure_is_red(state_dir: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"

    assert job._backup_verified(backups).state is CheckState.warn  # no marker yet

    (backups / job.VERIFY_MARKER).write_text("ok psychobooking-2026-08-26.dump\n")
    assert job._backup_verified(backups).state is CheckState.ok

    (backups / job.VERIFY_MARKER).write_text("failed psychobooking-2026-08-26.dump\n")
    check = job._backup_verified(backups)
    assert check.state is CheckState.fail
    assert "could not be read back" in check.summary


# --- Notifications ----------------------------------------------------------


async def test_a_transition_into_fail_notifies_once(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_send(intent_key: str, payload: dict) -> bool:
        sent.append(intent_key)
        return True

    monkeypatch.setattr(job, "_send", fake_send)
    failing = [Check("outbox_dead", CheckState.fail, "clients affected", "1 dead")]

    state, at = await job._maybe_notify(CheckState.fail, failing, {})
    assert sent == ["system.health.degraded.admin"]
    assert state == "fail" and at is not None

    # Still failing, inside the floor: silence.
    previous = {"state": "fail", "notified_state": state, "notified_at": at}
    await job._maybe_notify(CheckState.fail, failing, previous)
    assert sent == ["system.health.degraded.admin"]


async def test_a_long_outage_notifies_again_after_the_floor(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_send(intent_key: str, payload: dict) -> bool:
        sent.append(intent_key)
        return True

    monkeypatch.setattr(job, "_send", fake_send)
    failing = [Check("outbox_dead", CheckState.fail, "clients affected", "1 dead")]
    stale = (now_utc() - job.NOTIFY_FLOOR - timedelta(minutes=1)).isoformat()

    await job._maybe_notify(
        CheckState.fail, failing, {"state": "fail", "notified_state": "fail", "notified_at": stale}
    )

    assert sent == ["system.health.degraded.admin"]


async def test_recovery_notifies_and_clears(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_send(intent_key: str, payload: dict) -> bool:
        sent.append(intent_key)
        return True

    monkeypatch.setattr(job, "_send", fake_send)

    state, at = await job._maybe_notify(
        CheckState.ok,
        [],
        {"state": "fail", "notified_state": "fail", "notified_at": now_utc().isoformat()},
    )

    assert sent == ["system.health.recovered.admin"]
    assert state is None and at is None


async def test_a_healthy_pass_notifies_nothing(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_send(intent_key: str, payload: dict) -> bool:
        sent.append(intent_key)
        return True

    monkeypatch.setattr(job, "_send", fake_send)

    await job._maybe_notify(CheckState.ok, [], {"state": "ok"})

    assert sent == []


async def test_the_alert_payload_carries_ids_and_no_detail(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16.10: `detail` is written for a different reader, under looser rules
    than §13.4 allows for email."""
    captured: list[dict] = []

    async def fake_send(intent_key: str, payload: dict) -> bool:
        captured.append(payload)
        return True

    monkeypatch.setattr(job, "_send", fake_send)
    checks = [
        Check("outbox_dead", CheckState.fail, "clients affected", "smtp said 535 for lena@x.test"),
        Check("disk_space", CheckState.fail, "nearly full", "0.2 GB free"),
    ]

    await job._maybe_notify(CheckState.fail, checks, {})

    payload = captured[0]
    assert payload["checks"] == "outbox_dead, disk_space"
    assert "lena@x.test" not in json.dumps(payload)
    assert "535" not in json.dumps(payload)
