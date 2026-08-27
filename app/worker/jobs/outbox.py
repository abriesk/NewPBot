"""Outbox dispatch (IMPLEMENTATION.md §14).

    status='pending' AND next_attempt_at <= now(), limit 50

Render, send, record the attempt. On success `sent`. On transient failure
`attempts += 1` and back off `min(2^attempts, 60)` minutes; after 6 attempts
`dead` plus a `system.delivery_failed.admin` alert. On permanent failure `dead`
immediately, with no alert unless `attempts = 0`.

Rows are claimed with FOR UPDATE SKIP LOCKED so a second worker -- or a restart
mid-send -- never sends the same row twice.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import DeliveryResult, Transport
from app.config import get_settings
from app.core.enums import Channel, OutboxStatus
from app.core.models import Client, OutboxAttempt, OutboxMessage
from app.core.policies import now_utc
from app.core.services.notifications import alert_admin_delivery_failed
from app.core.services.settings import get_practice
from app.db import unit_of_work
from app.render.messages import render

logger = logging.getLogger(__name__)

BATCH_SIZE = 50

#: §14. After this many attempts a message is dead and the therapist is told.
MAX_ATTEMPTS = 6

#: §14: min(2^attempts, 60) minutes.
MAX_BACKOFF_MINUTES = 60


def backoff_minutes(attempts: int) -> int:
    """§14's exponential backoff, capped."""
    return int(min(2**attempts, MAX_BACKOFF_MINUTES))


async def claim_batch(session: AsyncSession, limit: int = BATCH_SIZE) -> list[OutboxMessage]:
    """Take up to `limit` due rows, locked against other workers.

    Claimed rows move to `sending` immediately so a crash between here and the
    send leaves them visibly stuck rather than silently re-sent.
    """
    rows = (
        (
            await session.execute(
                select(OutboxMessage)
                .where(
                    OutboxMessage.status == OutboxStatus.pending,
                    OutboxMessage.next_attempt_at <= now_utc(),
                )
                .order_by(OutboxMessage.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = OutboxStatus.sending
    await session.flush()
    return list(rows)


async def _recipient_timezone(session: AsyncSession, message: OutboxMessage) -> str:
    practice = await get_practice(session)
    if message.client_id is None:
        return practice.timezone
    client = (
        await session.execute(select(Client).where(Client.id == message.client_id))
    ).scalar_one_or_none()
    return (client.timezone if client and client.timezone else None) or practice.timezone


async def deliver_one(
    session: AsyncSession, message: OutboxMessage, transports: dict[Channel, Transport]
) -> DeliveryResult:
    """Render and hand to the transport. Never raises: a failure is a result."""
    transport = transports.get(message.channel)
    if transport is None:
        return DeliveryResult.permanent(f"no transport for {message.channel.value}")

    settings = get_settings()
    try:
        rendered = await render(
            session,
            intent_key=message.intent_key,
            payload=message.payload,
            locale=message.locale,
            channel=message.channel,
            tz=await _recipient_timezone(session, message),
            base_url=settings.base_url,
            # §9: what the Telegram buttons carry back. It lives on the row,
            # not in the payload, so it has to be handed over explicitly --
            # without it every inline button arrives with nothing to act on.
            request_id=message.request_id,
        )
    except KeyError as exc:
        # An intent with no spec is a bug, not a transient condition.
        return DeliveryResult.permanent(f"render failed: {exc}")

    return await transport.send(message.address, rendered)


async def record_outcome(
    session: AsyncSession, message: OutboxMessage, result: DeliveryResult
) -> None:
    """Apply §14's state transitions and write the attempt row."""
    session.add(OutboxAttempt(message_id=message.id, ok=result.ok, error=result.error))
    message.attempts += 1
    message.last_error = result.error

    if result.ok:
        message.status = OutboxStatus.sent
        message.sent_at = now_utc()
        return

    if result.permanent_failure:
        message.status = OutboxStatus.dead
        # §14: no alert unless this was the very first attempt. A permanent
        # failure after retries is usually a condition she already knows about.
        if message.attempts == 1:
            await alert_admin_delivery_failed(
                session,
                intent_key=message.intent_key,
                address=message.address,
                error=result.error or "permanent failure",
            )
        return

    if message.attempts >= MAX_ATTEMPTS:
        message.status = OutboxStatus.dead
        await alert_admin_delivery_failed(
            session,
            intent_key=message.intent_key,
            address=message.address,
            error=result.error or "gave up after retries",
        )
        return

    message.status = OutboxStatus.pending
    message.next_attempt_at = now_utc() + timedelta(minutes=backoff_minutes(message.attempts))


async def dispatch_outbox(transports: dict[Channel, Transport] | None = None) -> int:
    """One pass. Returns how many rows were attempted."""
    from app.worker.transports import build_transports

    active = transports if transports is not None else build_transports()

    # The claim commits on its own, before anything reaches a transport. This is
    # what the `sending` state is for: claiming and sending in one transaction
    # means a crash after a successful send rolls the row back to `pending` and
    # the next pass sends it a second time. `dedupe_key` cannot help there --
    # it dedupes what goes *into* the outbox, not what leaves it.
    async with unit_of_work() as session:
        claimed = [message.id for message in await claim_batch(session)]

    if not claimed:
        return 0

    # One transaction per message, so an outcome already recorded cannot be
    # undone by a later message in the same batch failing.
    for message_id in claimed:
        async with unit_of_work() as session:
            message = (
                await session.execute(select(OutboxMessage).where(OutboxMessage.id == message_id))
            ).scalar_one()
            result = await deliver_one(session, message, active)
            await record_outcome(session, message, result)
            if not result.ok:
                logger.warning(
                    "outbox %s to %s failed (attempt %s): %s",
                    message.intent_key,
                    message.channel.value,
                    message.attempts,
                    result.error,
                )
    return len(claimed)
