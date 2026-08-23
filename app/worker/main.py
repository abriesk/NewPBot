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
from app.db import dispose_engine

logger = logging.getLogger(__name__)

Job = Callable[[], Awaitable[None]]

# Populated in M4: dispatch_outbox, expire_slot_holds, expire_requests,
# fire_reminders, complete_requests, purge_content, prune_tokens,
# prune_revisions. Every job MUST be idempotent and safe to run concurrently.
JOBS: list[Job] = []


async def run_once() -> None:
    """One pass over every job. A failing job MUST NOT stop the others."""
    for job in JOBS:
        try:
            await job()
        except Exception:
            logger.exception("job %s failed", getattr(job, "__name__", job))


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
