"""Telegram transport (IMPLEMENTATION.md §9, §11.2).

`parse_mode='HTML'` with the tag subset the renderer emits. The MarkdownV2 parse
mode MUST NOT be used (hard rule 6) -- it fails on the first `.` or `-` in
ordinary Russian text.

One `RenderedMessage` may become several Telegram messages: the renderer already
split it at block boundaries, and this sends the parts in order with the actions
attached to the last one, so the buttons sit under the text they belong to.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.channels.base import Action, DeliveryResult, RenderedMessage
from app.config import Settings, get_settings
from app.core.enums import Channel

logger = logging.getLogger(__name__)

PARSE_MODE = "HTML"


def inline_keyboard(actions: list[Action]) -> InlineKeyboardMarkup | None:
    """One button per action, one per row.

    A `url` action becomes a link button; everything else carries callback data,
    which `Action` already checked fits Telegram's 64-byte limit.
    """
    if not actions:
        return None
    rows = [
        [
            InlineKeyboardButton(text=action.label, url=action.url)
            if action.url and not action.callback_data
            else InlineKeyboardButton(
                text=action.label, callback_data=action.callback_data or action.key
            )
        ]
        for action in actions
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class TelegramTransport:
    """`Transport` for the Telegram channel."""

    channel = Channel.telegram

    def __init__(self, bot: Bot | None = None, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._bot = bot or Bot(token=self._settings.telegram_bot_token)

    async def send(self, address: str, message: RenderedMessage) -> DeliveryResult:
        markup = inline_keyboard(message.actions)
        try:
            for index, part in enumerate(message.parts):
                is_last = index == len(message.parts) - 1
                await self._bot.send_message(
                    chat_id=int(address),
                    text=part,
                    parse_mode=PARSE_MODE,
                    reply_markup=markup if is_last else None,
                )
        except TelegramForbiddenError as exc:
            # The client blocked the bot. Retrying six times helps nobody.
            return DeliveryResult.permanent(f"forbidden: {exc}")
        except TelegramUnauthorizedError as exc:
            return DeliveryResult.permanent(f"unauthorized: {exc}")
        except TelegramBadRequest as exc:
            # Malformed HTML or a bad chat id: the same request will fail again.
            return DeliveryResult.permanent(f"bad request: {exc}")
        except TelegramRetryAfter as exc:
            return DeliveryResult.transient(f"flood control, retry after {exc.retry_after}s")
        except (ValueError, OSError) as exc:
            return DeliveryResult.transient(f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # network layer, aiogram wraps many things
            return DeliveryResult.transient(f"{type(exc).__name__}: {exc}")

        # Identifiers, never content (hard rule 8).
        logger.info("telegram delivered to chat %s", address)
        return DeliveryResult.success()

    async def close(self) -> None:
        await self._bot.session.close()
