"""NP3 tuning (IMPLEMENTATION.md §14): the retention jobs run hourly, the pool
recycles connections."""

from __future__ import annotations

from app.worker import main as worker


def test_due_is_pure() -> None:
    assert worker.due(None, 100.0, 3600.0)
    assert not worker.due(100.0, 3699.0, 3600.0)
    assert worker.due(100.0, 3700.0, 3600.0)


async def test_an_hourly_job_runs_once_per_hour_and_at_start() -> None:
    calls: list[int] = []

    async def sweep() -> None:
        calls.append(1)

    gated = worker.every(3600.0, sweep)

    await gated()
    await gated()
    await gated()

    assert calls == [1]
    assert gated.__name__ == "sweep"


def test_the_four_retention_jobs_are_hourly_and_the_rest_are_not() -> None:
    hourly = {"purge_content", "prune_tokens", "prune_revisions", "prune_error_events"}
    gated = {job.__name__ for job in worker.JOBS if "gated" in repr(job)}
    assert gated == hourly


def test_the_pool_recycles_connections() -> None:
    from app.db import get_engine

    assert get_engine().sync_engine.pool._recycle == 1800  # noqa: SLF001
