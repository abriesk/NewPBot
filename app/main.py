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

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.channels.telegram.webhook import build_router as telegram_router
from app.channels.telegram.webhook import register_webhook
from app.channels.web.admin import build_router as admin_router
from app.channels.web.client import build_router as web_router
from app.config import get_settings
from app.core.enums import ErrorSource
from app.core.services.status import record_error, where
from app.db import dispose_engine, get_session_factory, unit_of_work

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
    #
    # §16.1: registering the webhook refuses loudly rather than failing quietly
    # when nothing is terminating TLS in front of this deployment.
    await register_webhook()
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

    @app.exception_handler(Exception)
    async def record_unhandled(request: Request, exc: Exception) -> Response:
        """§6.9: the one failure that otherwise leaves no trace at all.

        A client who gets a 500 does not file a ticket -- they conclude the
        practice is disorganised and book elsewhere (DESIGN.md §22.1). Every
        other failure in this system is already a row; this makes that one a
        row too.

        Recording is best-effort and deliberately silent on its own failure:
        if the database is what broke, the insert breaks with it, and masking
        the original exception would be worse than losing the record.
        """
        logger.exception("unhandled error serving %s", request.url.path)
        try:
            async with unit_of_work() as session:
                await record_error(
                    session,
                    source=ErrorSource.web,
                    exc=exc,
                    location=where(exc, request.url.path),
                )
        except Exception:
            logger.warning("could not record the unhandled error")

        return PlainTextResponse("Internal Server Error", status_code=500)

    # One ASGI ingress: the client UI, the Telegram webhook, and (from M7) the
    # admin UI are all routes on this one app (DESIGN.md §3.3).
    app.mount(
        "/static",
        StaticFiles(directory="app/channels/web/static"),
        name="static",
    )
    app.include_router(telegram_router())

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

    # Admin before the client router: "/admin/..." must not be swallowed by the
    # client's catch-all-ish "/t/{topic_code}".
    app.include_router(admin_router())

    # Mounted last: its "/" and "/t/{code}" routes must not shadow anything.
    app.include_router(web_router())

    return app


app = create_app()
