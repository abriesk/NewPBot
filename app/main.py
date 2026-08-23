"""FastAPI application assembly.

This is the single ASGI ingress (DESIGN.md §3.3): client UI, admin UI, internal
API, and the Telegram webhook are all routes on this one app. There is
deliberately no separate bot process -- that split is what forced v1.0's
cross-process notification queue to exist.

Routers are mounted from M5 onward; M0 provides the health surface only.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import dispose_engine, get_session_factory

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "starting web ingress: telegram_mode=%s email_enabled=%s",
        settings.telegram_mode,
        settings.email_enabled,
    )
    # Migrating and seeding happen in the web container's command, before
    # uvicorn starts -- not here. Keeping startup free of database work is what
    # lets /healthz answer without one.
    try:
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Psychotherapy Booking Service",
        version="2.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness. Deliberately does not touch the database -- the Compose
        healthcheck gates the worker on this, and a slow database should not
        make the process look dead."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        """Readiness. Confirms the database answers."""
        try:
            async with get_session_factory()() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # reported, not raised: a probe must not 500
            logger.warning("readiness probe failed: %s", type(exc).__name__)
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "detail": type(exc).__name__},
            )
        return JSONResponse(status_code=200, content={"status": "ok"})

    return app


app = create_app()
