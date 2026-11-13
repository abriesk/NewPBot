"""Outbox resilience (IMPLEMENTATION.md §14; NP2: REVIEW A1, C1, F4, F7, F8).

One bad row never strands its batch; credential failures are retried, not
buried; SMTP never waits without a timeout; the Delivery page can requeue a dead
row. Fakes only: nothing here reaches a real SMTP server or Telegram.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosmtplib
import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import SendMessage
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.channels.base import DeliveryResult, RenderedMessage
from app.channels.email.transport import EmailTransport
from app.channels.telegram.transport import TelegramTransport
from app.config import get_settings
from app.core.enums import Channel, OutboxStatus
from app.core.models import OutboxAttempt, OutboxMessage, Practice
from app.db import dispose_engine
from app.main import create_app
from app.worker.jobs import outbox as job
from tests.e2e.test_admin_web import _csrf, _sign_in


@pytest_asyncio.fixture(autouse=True)
async def _dispose_shared_engine() -> AsyncIterator[None]:
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def committed() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


async def _make_rows(session: AsyncSession, addresses: list[str]) -> list[int]:
    practice = (await session.execute(select(Practice).limit(1))).scalar_one()
    ids = []
    for address in addresses:
        row = OutboxMessage(
            practice_id=practice.id,
            channel=Channel.telegram,
            address=address,
            intent_key="request.expired.client",
            payload={"uuid": "11111111-1111-1111-1111-111111111111"},
            locale="en",
            status=OutboxStatus.pending,
            next_attempt_at=datetime.now(UTC) + timedelta(days=1),
        )
        session.add(row)
        await session.flush()
        ids.append(row.id)
    await session.commit()
    return ids


async def _drop(session: AsyncSession, ids: list[int]) -> None:
    await session.rollback()
    await session.execute(delete(OutboxAttempt).where(OutboxAttempt.message_id.in_(ids)))
    await session.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(ids)))
    await session.commit()


class _Selective:
    channel = Channel.telegram

    def __init__(self, bad: str) -> None:
        self.bad = bad
        self.sent: list[str] = []

    async def send(self, address: str, message: RenderedMessage) -> DeliveryResult:
        if address == self.bad:
            raise RuntimeError("transport blew up")
        self.sent.append(address)
        return DeliveryResult.success()


async def test_one_raising_row_never_strands_the_rest_of_the_batch(
    committed: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _make_rows(committed, ["101", "BAD", "303"])
    try:

        async def ours(session: AsyncSession, limit: int = 0) -> list[OutboxMessage]:
            rows = (
                (
                    await session.execute(
                        select(OutboxMessage)
                        .where(OutboxMessage.id.in_(ids))
                        .order_by(OutboxMessage.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.status = OutboxStatus.sending
            await session.flush()
            return list(rows)

        monkeypatch.setattr(job, "claim_batch", ours)
        transport = _Selective("BAD")

        await job.dispatch_outbox(transports={Channel.telegram: transport})

        assert transport.sent == ["101", "303"]
        committed.expire_all()
        rows = {
            r.address: r
            for r in (
                await committed.execute(select(OutboxMessage).where(OutboxMessage.id.in_(ids)))
            )
            .scalars()
            .all()
        }
        assert rows["101"].status is OutboxStatus.sent
        assert rows["303"].status is OutboxStatus.sent
        assert rows["BAD"].status is OutboxStatus.pending, "the bad row must retry, not sit in sending"
        assert rows["BAD"].attempts == 1
        assert "RuntimeError" in (rows["BAD"].last_error or "")
    finally:
        await _drop(committed, ids)


# --- SMTP (F7, F8, A1) ---------------------------------------------------------


async def _smtp_send_raising(
    monkeypatch: pytest.MonkeyPatch, exc: Exception | None
) -> tuple[DeliveryResult, dict[str, Any]]:
    seen: dict[str, Any] = {}

    async def fake_send(message: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        if exc is not None:
            raise exc

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    result = await EmailTransport().send("a@example.test", RenderedMessage(parts=["hi"]))
    return result, seen


async def test_smtp_calls_carry_a_timeout(
    email_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, seen = await _smtp_send_raising(monkeypatch, None)
    assert result.ok
    assert seen.get("timeout") == 15


async def test_smtp_535_is_transient_and_says_why(
    email_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = aiosmtplib.SMTPAuthenticationError(535, "5.7.8 Username and Password not accepted")
    result, _ = await _smtp_send_raising(monkeypatch, exc)
    assert not result.ok
    assert not result.permanent_failure
    assert "535" in (result.error or "")


async def test_a_refused_recipient_is_still_permanent(
    email_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = await _smtp_send_raising(monkeypatch, aiosmtplib.SMTPResponseException(550, "no"))
    assert result.permanent_failure


async def test_a_telegram_unauthorized_error_is_transient() -> None:
    class Bot:
        async def send_message(self, **kwargs: Any) -> None:
            raise TelegramUnauthorizedError(
                method=SendMessage(chat_id=1, text="x"), message="Unauthorized"
            )

    transport = TelegramTransport(bot=Bot())  # type: ignore[arg-type]
    result = await transport.send("1", RenderedMessage(parts=["hi"]))
    assert not result.ok
    assert not result.permanent_failure
    assert "unauthorized" in (result.error or "").lower()


# --- The Delivery page: credential rows are visible, dead rows can be retried -------


@pytest.fixture
def web() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


async def test_the_delivery_page_shows_credential_trouble_and_retries_a_dead_row(
    committed: AsyncSession, web: TestClient
) -> None:
    ids = await _make_rows(committed, ["555", "556"])
    try:
        dead_id, waiting_id = ids
        for message_id, status, error in (
            (dead_id, OutboxStatus.dead, "boom"),
            (waiting_id, OutboxStatus.pending, "SMTP authentication failed: 535 nope"),
        ):
            row = (
                await committed.execute(select(OutboxMessage).where(OutboxMessage.id == message_id))
            ).scalar_one()
            row.status = status
            row.last_error = error
            row.attempts = 3
        await committed.commit()

        _sign_in(web)
        page = web.get("/admin/delivery")
        assert page.status_code == 200
        assert f"/admin/delivery/{dead_id}/retry" in page.text
        assert f"/admin/delivery/{waiting_id}/retry" not in page.text
        assert "credentials-warning" in page.text

        refused = web.post(f"/admin/delivery/{dead_id}/retry", data={}, follow_redirects=False)
        assert refused.status_code == 403

        ok = web.post(
            f"/admin/delivery/{dead_id}/retry",
            data={"csrf_token": _csrf(web)},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        committed.expire_all()
        row = (
            await committed.execute(select(OutboxMessage).where(OutboxMessage.id == dead_id))
        ).scalar_one()
        assert row.status is OutboxStatus.pending
        assert row.attempts == 0
    finally:
        await _drop(committed, ids)


# --- Addresses are validated where they are typed (F4, C15) -------------------


@pytest.mark.parametrize("path", ["/auth/email", "/waitlist"])
@pytest.mark.parametrize(
    "bad", ["a@b.example\nBcc: x@y.example", "a@b.example\r\nSubject: x", "not an address", "a b@c.d"]
)
async def test_a_bad_address_is_refused_at_the_form_with_a_readable_error(
    committed: AsyncSession, web: TestClient, path: str, bad: str
) -> None:
    web.get("/auth/email")
    response = web.post(
        path, data={"csrf_token": _csrf(web), "email": bad}, follow_redirects=False
    )
    assert response.status_code == 400
    assert "<h1>" in response.text
    queued = (
        await committed.execute(
            select(OutboxMessage).where(OutboxMessage.address.like("%Bcc:%"))
        )
    ).scalars().all()
    assert not queued


async def test_the_email_transport_refuses_a_line_break_without_retrying(
    email_enabled: None,
) -> None:
    result = await EmailTransport().send("a@b.example\nBcc: x@y.example", RenderedMessage(parts=["hi"]))
    assert result.permanent_failure
