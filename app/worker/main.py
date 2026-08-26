"""The worker loop.

One process, one loop, every WORKER_POLL_SECONDS (IMPLEMENTATION.md §14). Jobs
are registered here in M4; M0 establishes the loop and its shutdown behaviour.

APScheduler, Celery, and Redis are forbidden (§2). Scheduled work is a database
sweep claiming rows with FOR UPDATE SKIP LOCKED -- an in-memory schedule does
not survive a deploy, and a broker is a whole service for a handful of jobs a
day.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable

from app.config import get_settings
from app.core.enums import ErrorSource
from app.core.services.status import record_error, where
from app.db import dispose_engine, unit_of_work
from app.worker.jobs.outbox import dispatch_outbox
from app.worker.jobs.status import write_status
from app.worker.jobs.sweeps import (
    complete_requests,
    expire_requests,
    expire_slot_holds,
    fire_reminders,
    prune_error_events,
    prune_revisions,
    prune_tokens,
    purge_content,
)

logger = logging.getLogger(__name__)

Job = Callable[[], Awaitable[object]]

#: §14, in the order a pass runs them. Expiry before dispatch, so a request that
#: lapsed since the last pass has its notification in the outbox by the time the
#: dispatcher looks. Every job is idempotent and safe beside a second worker.
JOBS: list[Job] = [
    expire_slot_holds,
    expire_requests,
    complete_requests,
    fire_reminders,
    dispatch_outbox,
    purge_content,
    prune_tokens,
    prune_revisions,
    prune_error_events,
    # Last, so the file it writes describes the pass that has just finished
    # rather than the one before it (§16.8).
    write_status,
]


async def run_once() -> None:
    """One pass over every job. A failing job MUST NOT stop the others."""
    for job in JOBS:
        name = getattr(job, "__name__", str(job))
        try:
            await job()
        except Exception as exc:
            logger.exception("job %s failed", name)
            # §6.9: this `except` is why a broken job can fail every twenty
            # seconds for a month unnoticed. Recording it is what makes
            # `worker_errors` (§16.9) able to see it at all.
            await _record(exc, name)


async def _record(exc: Exception, job: str) -> None:
    """Best-effort, and never allowed to become the failure it is reporting."""
    try:
        async with unit_of_work() as session:
            await record_error(
                session, source=ErrorSource.worker, exc=exc, location=where(exc, job)
            )
    except Exception:
        logger.warning("could not record the failure of job %s", job)


async def run_forever(stop: asyncio.Event) -> None:
    settings = get_settings()
    interval = settings.worker_poll_seconds
    logger.info("worker started: %d job(s), poll interval %ds", len(JOBS), interval)

    while not stop.is_set():
        await run_once()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)

    logger.info("worker stopping")


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

    try:
        await run_forever(stop)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
