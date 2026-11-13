"""Smaller review fixes (IMPLEMENTATION.md §11.2, §12.2, §13.2, §14, §16; NP2: A2, C6-C13, C15).

Each is one sentence of docs/REVIEW.md with the test it asked for.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import Action
from app.channels.telegram.router import Update, handle
from app.channels.telegram.router import _admin_request as router_admin_request
from app.core.enums import Channel, OutboxStatus
from app.core.models import OutboxMessage
from app.core.services.clients import erase_client
from app.core.services.notifications import purge_payload
from app.main import create_app
from app.render.messages import render
from app.worker.transports import build_transports
from tests.channels.test_telegram_flow import (
    CHAT,
    _admin_request,
    _book_and_return_to_menu,
)
from tests.e2e.test_admin_web import _csrf, _sign_in


@pytest.fixture
def web() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


# --- A2, C13: the admin routes -----------------------------------------------


def test_a_blank_translation_and_a_stray_language_are_refused_by_the_admin_routes(
    web: TestClient,
) -> None:
    _sign_in(web)
    blank = web.post(
        "/admin/translations",
        data={"csrf_token": _csrf(web), "lang": "en", "key": "common.skip", "value": "  "},
        follow_redirects=False,
    )
    assert blank.status_code == 303
    assert "empty-value-not-saved" in blank.headers["location"]

    stray = web.post(
        "/admin/translations",
        data={"csrf_token": _csrf(web), "lang": "am", "key": "common.skip", "value": "x"},
        follow_redirects=False,
    )
    assert stray.status_code == 400
    stray_block = web.post(
        "/admin/content/blocks",
        data={
            "csrf_token": _csrf(web),
            "topic_code": "work_terms",
            "lang": "am",
            "position": 1,
            "body_md": "x",
        },
        follow_redirects=False,
    )
    assert stray_block.status_code == 400


# --- C6: admin email links -----------------------------------------------------


async def test_an_admin_email_links_to_the_admin_page(db: AsyncSession) -> None:
    uuid = "11111111-1111-1111-1111-111111111111"
    message = await render(
        db,
        intent_key="request.submitted.admin",
        payload={"uuid": uuid},
        locale="en",
        channel=Channel.email,
        tz="UTC",
        base_url="https://example.test",
    )
    urls = [a.url for a in message.actions if isinstance(a, Action) and a.url]
    assert urls, "the admin email carries no links"
    assert all(f"/admin/requests/{uuid}" in u for u in urls), urls


# --- C7: reminder emails carry a view token -------------------------------------


async def test_a_reminder_email_carries_a_view_token(
    db: AsyncSession, request_id: int, client: Any, email_enabled: None
) -> None:
    from app.core.services.clients import link_identity
    from app.core.services.notifications import Recipient, enqueue_raw

    await link_identity(db, client.id, Channel.email, "rem@example.test", verified=True)
    rows = await enqueue_raw(
        db,
        intent_key="reminder.client",
        recipient=Recipient.client,
        payload={"uuid": "11111111-1111-1111-1111-111111111111"},
        request_id=request_id,
        dedupe_key="np2-reminder-test",
    )
    mail = [r for r in rows if r.channel is Channel.email]
    assert mail, "no email row was queued"
    assert mail[0].payload.get("view_token")


# --- C8 / C9: Telegram ---------------------------------------------------------


async def test_client_text_is_escaped_in_my_appointments(
    db: AsyncSession, future_slot: Any, session_type_id: int
) -> None:
    from app.core.services.translations import get_text

    request = await _book_and_return_to_menu(db, future_slot, session_type_id)
    request.slot_id = None
    request.desired_time_text = "<3 next week"
    await db.flush()

    reply = await handle(
        db, Update(chat_id=CHAT, text=await get_text(db, "ru", "menu.appointments"))
    )
    assert reply is not None
    assert "&lt;3 next week" in reply.text
    assert "<3 next" not in reply.text


async def test_the_admin_request_screen_is_split_under_4096(
    db: AsyncSession, future_slot: Any, session_type_id: int
) -> None:
    request = await _admin_request(db, future_slot, session_type_id)
    request.problem_text = "&" * 4000
    request.contact_note = "<" * 1000
    await db.flush()

    reply = await router_admin_request(db, request)
    parts = [reply.text, *reply.extra]
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts), [len(p) for p in parts]
    joined = "".join(parts)
    assert joined.count("&amp;") == 4000
    assert joined.count("&lt;") == 1000


# --- C10: tz and modality from the browser --------------------------------------


def test_a_bad_tz_or_modality_is_a_400_not_a_500(web: TestClient) -> None:
    web.get("/book")
    assert (
        web.get("/book/slots", params={"tz": "Nowhere/Land", "session_type_id": 1}).status_code
        == 200
    )
    assert web.get("/book/slots", params={"modality": "bogus"}).status_code == 400
    for data in ({"tz": "Nowhere/Land"}, {"modality": "bogus"}):
        response = web.post(
            "/book/hold",
            data={"csrf_token": _csrf(web), "slot_id": 1, "session_type_id": 1, **data},
        )
        assert response.status_code == 400, data


# --- C11: purge and erasure reach the outbox ------------------------------------


def test_purge_removes_the_words_from_a_payload() -> None:
    cleaned = purge_payload({"uuid": "u", "problem": "secret", "note": "n", "message": "m"})
    assert cleaned == {"uuid": "u"}


async def test_erasure_clears_outbox_payloads_and_cancels_what_is_waiting(
    db: AsyncSession, client: Any, request_id: int
) -> None:
    from app.core.models import Practice

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    rows = []
    for status in (OutboxStatus.sent, OutboxStatus.pending):
        row = OutboxMessage(
            practice_id=practice.id,
            channel=Channel.telegram,
            address="12345",
            client_id=client.id,
            request_id=request_id,
            intent_key="request.submitted.admin",
            payload={"uuid": "u", "problem": "my private words"},
            locale="en",
            status=status,
        )
        db.add(row)
        rows.append(row)
    await db.flush()

    await erase_client(db, client.id)

    for row in rows:
        assert "problem" not in row.payload
        assert row.address == "erased"
    assert rows[1].status is OutboxStatus.dead


# --- C12: callbacks are answered when handle() raises ---------------------------


def test_a_callback_is_answered_even_when_handle_raises(
    web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channels.telegram import webhook
    from app.config import get_settings

    answered: list[object] = []

    async def boom(session: Any, update: Any) -> Any:
        raise ValueError("crafted callback")

    async def record(update: Any, reply: Any) -> None:
        answered.append(update.callback_id)

    monkeypatch.setattr(webhook, "handle", boom)
    monkeypatch.setattr(webhook, "_send", record)
    settings = get_settings()
    response = web.post(
        settings.telegram_webhook_path,
        headers={webhook.SECRET_HEADER: settings.telegram_webhook_secret},
        json={
            "update_id": 9,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": 7, "first_name": "A"},
                "data": "tz:<bad>",
                "message": {"message_id": 3, "chat": {"id": 7, "type": "private"}},
            },
        },
    )
    assert response.status_code == 200
    assert answered == ["cb-1"]


# --- C15: one Bot for the process -------------------------------------------------


def test_the_telegram_transport_is_reused_across_passes() -> None:
    from app.core.enums import Channel as C

    assert build_transports()[C.telegram] is build_transports()[C.telegram]


# --- Review round 1: erasure reaches names, reasons and delivery alerts ------------------


async def test_erasure_clears_name_reason_and_alerts_that_name_the_address(
    db: AsyncSession, client: Any, request_id: int
) -> None:
    from app.core.models import Practice

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    from app.core.services.clients import link_identity

    await link_identity(db, client.id, Channel.email, "gone@example.test", verified=True)

    def row(**kw: Any) -> OutboxMessage:
        r = OutboxMessage(
            practice_id=practice.id,
            channel=Channel.telegram,
            address="1",
            locale="en",
            status=OutboxStatus.sent,
            **kw,
        )
        db.add(r)
        return r

    named = row(
        request_id=request_id,
        intent_key="request.submitted.admin",
        payload={"uuid": "u", "name": "Real Name", "reason": "private reason"},
    )
    alert = row(
        intent_key="system.delivery_failed.admin",
        payload={"intent": "x", "address": "gone@example.test", "error": "550 gone@example.test"},
    )
    other = row(
        intent_key="system.delivery_failed.admin",
        payload={"intent": "x", "address": "someone@else.test", "error": "e"},
    )
    await db.flush()

    await erase_client(db, client.id)

    assert "name" not in named.payload and "reason" not in named.payload
    assert alert.payload["address"] == "erased"
    assert "gone@example.test" not in str(alert.payload)
    assert other.payload["address"] == "someone@else.test"


async def test_retention_purges_a_waitlist_entrys_words(db: AsyncSession, client: Any) -> None:
    from datetime import UTC, datetime, timedelta

    from app.core.enums import WaitlistStatus
    from app.core.models import Practice, WaitlistEntry
    from app.worker.jobs.sweeps import purge_waitlist_in_session

    practice = (await db.execute(select(Practice).limit(1))).scalar_one()
    old = WaitlistEntry(
        practice_id=practice.id,
        client_id=client.id,
        problem_text="old words",
        contact_note="old contact",
        status=WaitlistStatus.contacted,
    )
    db.add(old)
    await db.flush()
    old.created_at = datetime.now(UTC) - timedelta(days=30 * (practice.retention_months + 1))
    await db.flush()

    assert await purge_waitlist_in_session(db) >= 1
    assert old.problem_text is None and old.contact_note is None
