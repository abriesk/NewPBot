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
from app.config import get_settings
from app.core.enums import Channel

logger = logging.getLogger(__name__)

#: The registry is rebuilt every poll, so a plain log line here would repeat
#: every WORKER_POLL_SECONDS forever. Once per process is the useful amount.
_announced: set[str] = set()


def _announce_once(message: str) -> None:
    if message not in _announced:
        _announced.add(message)
        logger.info("%s", message)


def build_transports() -> dict[Channel, Transport]:
    """The channels this deployment can actually send on.

    With SMTP_HOST unset the email transport is left out entirely (§4). The
    notification service already declines to create email rows, so an absent
    transport should never be reached -- but leaving it out means a stray row
    dies with a clear reason instead of silently pretending to send.

    """
    settings = get_settings()
    transports: dict[Channel, Transport] = {Channel.telegram: TelegramTransport(settings=settings)}

    if settings.email_enabled:
        transports[Channel.email] = EmailTransport(settings)
    else:
        _announce_once("email channel disabled: SMTP_HOST is unset")

    return transports
