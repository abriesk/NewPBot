"""SMTP transport (IMPLEMENTATION.md §4, §9, §13.4).

Outbound only. Email is the least private channel in this system -- shared
inboxes, lock-screen previews, a partner reading over a shoulder -- so the
notification service has already stripped problem text and join links from the
payload before anything reaches here (§13.4). This module only sends what it is
given.

With `SMTP_HOST` unset the channel is disabled cleanly: no rows are created for
it (app/core/services/notifications.py), and `send` refuses rather than
pretending to succeed.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage

import aiosmtplib

from app.channels.base import DeliveryResult, RenderedMessage
from app.config import Settings, get_settings
from app.core.enums import Channel

logger = logging.getLogger(__name__)

#: SMTP 5xx means the server has made a decision it will repeat. Retrying a bad
#: mailbox six times with backoff annoys the receiving server and helps nobody.
PERMANENT_PREFIX = "5"


class EmailTransport:
    """`Transport` for the email channel."""

    channel = Channel.email

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self._settings.email_enabled

    async def send(self, address: str, message: RenderedMessage) -> DeliveryResult:
        if not self.enabled:
            # Permanent: no amount of retrying configures SMTP.
            return DeliveryResult.permanent("email channel is disabled (SMTP_HOST unset)")

        settings = self._settings
        mail = EmailMessage()
        mail["From"] = settings.smtp_from or ""
        mail["To"] = address
        mail["Subject"] = message.subject or ""
        mail.set_content(message.text)
        for attachment in message.attachments:
            # §13.5. `str` content makes this a text/<subtype> part in UTF-8,
            # which is what an .ics needs and what the renderer produces.
            mail.add_attachment(
                attachment.content,
                subtype=attachment.subtype,
                filename=attachment.filename,
            )

        try:
            await aiosmtplib.send(
                mail,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user or None,
                password=settings.smtp_password or None,
                start_tls=settings.smtp_starttls,
            )
        except aiosmtplib.SMTPResponseException as exc:
            detail = f"{exc.code} {exc.message}"
            if str(exc.code).startswith(PERMANENT_PREFIX):
                return DeliveryResult.permanent(detail)
            return DeliveryResult.transient(detail)
        except aiosmtplib.SMTPRecipientsRefused as exc:
            return DeliveryResult.permanent(str(exc))
        except (aiosmtplib.SMTPException, OSError) as exc:
            # Connection refused, DNS failure, timeout -- all worth retrying.
            return DeliveryResult.transient(f"{type(exc).__name__}: {exc}")

        # Never log the body or the subject: identifiers only (hard rule 8).
        logger.info("email delivered to %s", _redact(address))
        return DeliveryResult.success()


def _redact(address: str) -> str:
    """`a***@example.test`. Enough to correlate, not enough to leak a list."""
    local, _, domain = address.partition("@")
    if not domain:
        return "***"
    head = local[:1] if local else ""
    return f"{head}***@{domain}"
