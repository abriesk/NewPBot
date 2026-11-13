"""Transport registry for the worker.

Kept out of app/worker/jobs/ so a job never decides which channels exist -- it
is handed a mapping and uses it. That is what makes adding a channel one
`Transport` implementation and one line here (§9).
"""

from __future__ import annotations

import logging

from app.channels.base import Transport
from app.channels.email.transport import EmailTransport
from app.channels.telegram.transport import TelegramTransport
from app.config import Settings, get_settings
from app.core.enums import Channel

logger = logging.getLogger(__name__)

#: The registry is rebuilt every poll, so a plain log line here would repeat
#: every WORKER_POLL_SECONDS forever. Once per process is the useful amount.
_announced: set[str] = set()


def _announce_once(message: str) -> None:
    if message not in _announced:
        _announced.add(message)
        logger.info("%s", message)


#: One Telegram transport per token for the life of the process: every pass used
#: to build a `Bot` and never close it, leaking an aiohttp session per pass.
_telegram: dict[str, TelegramTransport] = {}


def _telegram_transport(settings: Settings) -> TelegramTransport:
    token = settings.telegram_bot_token
    if token not in _telegram:
        _telegram[token] = TelegramTransport(settings=settings)
    return _telegram[token]


def build_transports() -> dict[Channel, Transport]:
    """The channels this deployment can actually send on.

    With SMTP_HOST unset the email transport is left out entirely (§4). The
    notification service already declines to create email rows, so an absent
    transport should never be reached -- but leaving it out means a stray row
    dies with a clear reason instead of silently pretending to send.

    """
    settings = get_settings()
    transports: dict[Channel, Transport] = {Channel.telegram: _telegram_transport(settings)}

    if settings.email_enabled:
        transports[Channel.email] = EmailTransport(settings)
    else:
        _announce_once("email channel disabled: SMTP_HOST is unset")

    return transports
