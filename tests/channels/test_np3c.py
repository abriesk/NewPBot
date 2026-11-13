"""NP3c: follow-ups from the NP1-NP4a reviews and the NP2 deploy (one test module per slice, the
item number in each docstring)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.telegram import keyboards as kb
from app.channels.telegram.router import _counter_with_slot, _propose_screen
from app.channels.web.client import _SLOT_GONE, _countered_instant
from app.core.enums import Channel, Modality, NegotiationKind, SlotStatus
from app.core.models import BookingRequest, Client, NegotiationMessage, Practice, Slot
from app.core.services import booking
from app.core.services import slots as slot_service
from app.core.services.translations import get_text
from app.render.messages import render
from tests.channels.test_telegram_flow import CHAT, _walk_to_contact

_n = 0


async def _stale_setup(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    other_request_id: int,
) -> tuple[BookingRequest, Slot]:
    """A negotiating request, a booked slot, and an `available` slot half an hour into it: the
    stale screen offered the second before the first was booked."""
    global _n
    _n += 1
    future_slot.modality = Modality.online
    overlapping = Slot(
        practice_id=practice.id,
        starts_at=future_slot.starts_at + timedelta(minutes=30),
        duration_min=60,
        modality=Modality.onsite,
        status=SlotStatus.available,
    )
    spare = Slot(
        practice_id=practice.id,
        starts_at=datetime.now(UTC) + timedelta(days=50, microseconds=_n + 900),
        duration_min=60,
        status=SlotStatus.available,
    )
    db.add_all([overlapping, spare])
    await db.flush()
    request = await booking.submit_slot_request(
        db,
        client_id=client.id,
        slot_id=spare.id,
        session_type_id=session_type_id,
        modality=Modality.online,
        source_channel=Channel.web,
        problem_text="x",
    )
    await booking.admin_propose(db, request.id, proposed_start=spare.starts_at)
    await slot_service.book_slot(db, future_slot.id, other_request_id)
    return request, overlapping


async def _counters(db: AsyncSession, request: BookingRequest) -> int:
    rows = (
        (
            await db.execute(
                select(NegotiationMessage).where(
                    NegotiationMessage.request_id == request.id,
                    NegotiationMessage.kind == NegotiationKind.counter,
                )
            )
        )
        .scalars()
        .all()
    )
    return len(rows)


async def test_telegram_counter_over_a_booked_slot_is_refused(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    other_request_id: int,
) -> None:
    """NP3c/2: the stale keyboard offered an hour a booking has since overlapped."""
    request, overlapping = await _stale_setup(
        db, practice, client, session_type_id, future_slot, other_request_id
    )
    reply = await _counter_with_slot(db, client, request, overlapping.id)
    assert reply.text == await get_text(db, client.language, "booking.slot.taken")
    assert await _counters(db, request) == 0


async def test_the_therapists_proposal_over_a_booked_slot_is_refused(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    other_request_id: int,
) -> None:
    request, overlapping = await _stale_setup(
        db, practice, client, session_type_id, future_slot, other_request_id
    )
    before = (
        await db.execute(
            select(NegotiationMessage.id).where(NegotiationMessage.request_id == request.id)
        )
    ).all()
    await _propose_screen(db, 1, request, kb.PROPOSE_SLOT, str(overlapping.id))
    after = (
        await db.execute(
            select(NegotiationMessage.id).where(NegotiationMessage.request_id == request.id)
        )
    ).all()
    assert len(after) == len(before)


async def test_web_counter_over_a_booked_slot_is_slot_gone(
    db: AsyncSession,
    practice: Practice,
    client: Client,
    session_type_id: int,
    future_slot: Slot,
    other_request_id: int,
) -> None:
    _, overlapping = await _stale_setup(
        db, practice, client, session_type_id, future_slot, other_request_id
    )
    assert await _countered_instant(db, str(overlapping.id), "", "UTC") is _SLOT_GONE


@pytest.mark.parametrize("locale", ["en", "ru", "hy"])
async def test_the_books_mail_says_once_how_long_the_link_lasts(
    db: AsyncSession, locale: str
) -> None:
    """NP3c/6: "works once, expires in 30 minutes" was in the body and again in the .books line."""
    message = await render(
        db,
        intent_key="auth.login_link.client",
        payload={
            "url": "https://example.test/auth/callback?token=t",
            "minutes": 30,
            "when": "tomorrow at noon",
            "session_type": "first meeting",
        },
        locale=locale,
        channel=Channel.email,
        tz="UTC",
        base_url="https://example.test",
    )
    assert "tomorrow at noon" in message.text
    assert message.text.count("30") == 1


async def test_the_email_sent_line_escapes_the_address(
    db: AsyncSession, future_slot: Slot, session_type_id: int, email_enabled: None
) -> None:
    """NP3c/6: `booking.contact.email_sent` is HTML for Telegram; an address with `&` in it is
    escaped, not passed through."""
    from app.channels.telegram.router import Update, handle

    await _walk_to_contact(db, future_slot, session_type_id)
    await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.CONTACT}:{kb.CONTACT_EMAIL}"))
    reply = await handle(db, Update(chat_id=CHAT, text="R&D@Example.Test"))
    assert reply is not None
    assert "r&amp;d@example.test" in reply.extra[0]
    assert "r&d@" not in reply.extra[0]


def _request_with(cookies: dict[str, str]):  # type: ignore[no-untyped-def]
    from starlette.requests import Request

    header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [(b"cookie", header.encode())]}
    )


def test_csrf_accepts_any_valid_token_of_the_same_session() -> None:
    """NP3c/7: a page load in tab B replaces the cookie; Save in tab A carries the old token."""
    from app.channels.web.security import (
        ADMIN_COOKIE,
        _binding,
        _mint_csrf,
        csrf_cookie_name,
        csrf_ok,
    )

    session = {ADMIN_COOKIE: "one-session"}
    binding = _binding(_request_with(session))
    tab_a, tab_b = _mint_csrf(binding), _mint_csrf(binding)
    assert tab_a != tab_b
    # the cookie holds tab B's token (the later page load); tab A submits its own
    request = _request_with({**session, csrf_cookie_name(): tab_b})
    assert csrf_ok(request, tab_a) is True
    assert csrf_ok(request, tab_b) is True

    other = _mint_csrf(_binding(_request_with({ADMIN_COOKIE: "another-session"})))
    assert csrf_ok(request, other) is False
    assert csrf_ok(_request_with({**session, csrf_cookie_name(): other}), tab_a) is False
    assert csrf_ok(_request_with(session), tab_a) is False  # no cookie at all
    assert csrf_ok(request, "") is False


@pytest.mark.parametrize("channel", [Channel.email, Channel.telegram])
async def test_outgoing_times_use_the_web_wording(db: AsyncSession, channel: Channel) -> None:
    """NP3c/10: Telegram and email name a moment as the web does -- not `2026-09-15 18:30`."""
    when = datetime(2026, 9, 15, 14, 30, tzinfo=UTC)
    message = await render(
        db,
        intent_key="request.confirmed.client",
        payload={"uuid": "abc", "time": when.isoformat(), "modality": "online"},
        locale="en",
        channel=channel,
        tz="Asia/Yerevan",
        base_url="https://example.test",
    )
    text = "\n".join(message.parts)
    assert "18:30 (Yerevan, GMT+4)" in text
    assert "2026-09-15 18:30" not in text


async def test_free_text_in_the_time_field_comes_back_as_written(db: AsyncSession) -> None:
    message = await render(
        db,
        intent_key="request.confirmed.client",
        payload={"uuid": "abc", "time": "some evening next week", "modality": "online"},
        locale="en",
        channel=Channel.email,
        tz="UTC",
        base_url="https://example.test",
    )
    assert "some evening next week" in "\n".join(message.parts)
