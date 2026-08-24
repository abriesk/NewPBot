"""Telegram webhook (IMPLEMENTATION.md §12.3, §16.1).

An ordinary route on the one ASGI app, not a separate process. That is what
lets the outbox be about durability rather than plumbing (DESIGN.md §3.3).

The secret header is checked **before the body is parsed**: an unauthenticated
caller must not be able to make the process do work, and JSON parsing is work.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Header, Request, Response

from app.channels.telegram.keyboards import parse_callback  # noqa: F401  (re-export)
from app.channels.telegram.router import Reply, Update, handle
from app.channels.telegram.transport import PARSE_MODE, inline_keyboard
from app.config import get_settings
from app.db import unit_of_work

logger = logging.getLogger(__name__)

# The header name, not a secret -- the value it carries is the secret.
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105


def build_router() -> APIRouter:
    settings = get_settings()
    router = APIRouter()

    @router.post(settings.telegram_webhook_path, include_in_schema=False)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> Response:
        # §12.3: reject with 403 on mismatch, before parsing the body.
        # compare_digest so a wrong secret cannot be found a byte at a time.
        expected = settings.telegram_webhook_secret
        supplied = x_telegram_bot_api_secret_token or ""
        if not secrets.compare_digest(supplied, expected):
            logger.warning("telegram webhook called with a bad secret token")
            return Response(status_code=403)

        payload = await request.json()
        update = _parse(payload)
        if update is None:
            # An update type this router does not handle. 200, or Telegram
            # retries it forever.
            return Response(status_code=200)

        try:
            async with unit_of_work() as session:
                reply = await handle(session, update)
        except Exception:
            # An unexpected failure is this deployment's bug, and a 500 would
            # make Telegram redeliver the same update indefinitely. Log it with
            # the traceback (identifiers only -- the update body is not logged)
            # and acknowledge, so one broken message does not become a loop.
            # The transaction rolled back, so nothing partial was written.
            logger.exception("telegram update from chat %s failed", update.chat_id)
            return Response(status_code=200)

        if reply is not None:
            await _send(update.chat_id, reply)

        # §12.3: return 200 quickly.
        return Response(status_code=200)

    return router


def _parse(payload: dict[str, object]) -> Update | None:
    """Pull the parts the router uses out of a raw update."""
    message = payload.get("message") or payload.get("edited_message")
    if isinstance(message, dict):
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict):
            return None
        return Update(
            chat_id=int(chat["id"]),
            text=message.get("text"),
            display_name=_display_name(sender),
        )

    callback = payload.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(chat, dict):
            return None
        return Update(
            chat_id=int(chat["id"]),
            callback_data=callback.get("data"),
            display_name=_display_name(callback.get("from")),
        )

    return None


def _display_name(sender: object) -> str | None:
    if not isinstance(sender, dict):
        return None
    parts = [sender.get("first_name"), sender.get("last_name")]
    name = " ".join(str(p) for p in parts if p)
    return name or None


async def _send(chat_id: int, reply: Reply) -> None:
    """Send a router reply.

    This is a *response* to an inbound update, not an outbound notification, so
    it does not go through the outbox: there is no domain change to be atomic
    with, and a client waiting on a keypress should not wait for a worker poll.
    Every notification still goes through the outbox (hard rule 2).
    """
    from aiogram import Bot

    settings = get_settings()
    bot = Bot(token=settings.telegram_bot_token)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=reply.text,
            parse_mode=PARSE_MODE,
            reply_markup=reply.keyboard or inline_keyboard([]),
        )
        for part in reply.extra:
            await bot.send_message(chat_id=chat_id, text=part, parse_mode=PARSE_MODE)
    except Exception as exc:  # a reply that fails must not fail the webhook
        logger.warning("could not reply to chat %s: %s", chat_id, type(exc).__name__)
    finally:
        await bot.session.close()


async def register_webhook() -> bool:
    """Point Telegram at this deployment.

    §16.1: with `plain` and nothing terminating TLS in front, this refuses and
    says so, rather than failing silently. Long-polling is the supported
    fallback there.
    """
    settings = get_settings()

    if settings.telegram_mode != "webhook":
        logger.info("TELEGRAM_MODE=%s: not registering a webhook", settings.telegram_mode)
        return False

    if not settings.base_url.startswith("https://"):
        logger.error(
            "refusing to register a Telegram webhook: BASE_URL is %r, and Telegram "
            "requires a publicly reachable HTTPS URL. Put TLS in front of this "
            "deployment, or set TELEGRAM_MODE=polling for local development.",
            settings.base_url,
        )
        return False

    from aiogram import Bot

    bot = Bot(token=settings.telegram_bot_token)
    try:
        await bot.set_webhook(
            url=f"{settings.base_url}{settings.telegram_webhook_path}",
            secret_token=settings.telegram_webhook_secret,
            drop_pending_updates=False,
        )
    except Exception as exc:
        logger.error("could not register the Telegram webhook: %s", exc)
        return False
    finally:
        await bot.session.close()

    logger.info("Telegram webhook registered")
    return True
