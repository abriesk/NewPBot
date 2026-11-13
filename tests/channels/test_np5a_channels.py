"""NP5a: the onsite {url} is escaped on both paths; one short request reference in text."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Practice, Translation
from app.core.ref import short_ref
from app.core.services.translations import invalidate_cache

URL = "https://clinic.example/where?a=1&b=<2>"
ESCAPED = "https://clinic.example/where?a=1&amp;b=&lt;2&gt;"


async def _onsite_reply(db: AsyncSession, practice: Practice, text: str):  # type: ignore[no-untyped-def]
    from app.channels.telegram import keyboards as kb
    from app.channels.telegram.router import Update, handle
    from tests.channels.test_telegram_flow import CHAT

    practice.clinic_onsite_url = URL
    await db.execute(
        sa.update(Translation)
        .where(
            Translation.practice_id == practice.id,
            Translation.lang == "ru",
            Translation.key == "booking.onsite_info",
        )
        .values(value=text)
    )
    invalidate_cache()
    try:
        await handle(db, Update(chat_id=CHAT, text="/start"))
        await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.LANG}:ru"))
        reply = await handle(db, Update(chat_id=CHAT, callback_data=f"{kb.MODE}:onsite"))
    finally:
        invalidate_cache()
    assert reply is not None
    return reply


async def test_the_onsite_url_is_escaped_when_the_text_has_the_placeholder(
    db: AsyncSession, practice: Practice, session_type_id: int
) -> None:
    reply = await _onsite_reply(db, practice, "Кабинет здесь: {url}")
    assert ESCAPED in reply.extra[0]
    assert "<2>" not in reply.extra[0]


async def test_the_onsite_url_is_escaped_when_the_text_lacks_the_placeholder(
    db: AsyncSession, practice: Practice, session_type_id: int
) -> None:
    reply = await _onsite_reply(db, practice, "Я принимаю в кабинете.")
    assert ESCAPED in reply.extra[0]
    assert "<2>" not in reply.extra[0]


def test_the_short_reference_is_the_first_block_in_upper_case() -> None:
    assert short_ref("5a969404-1111-2222-3333-444455556666") == "5A969404"


def test_a_message_names_the_request_by_its_short_reference() -> None:
    from app.render.messages import _format_args

    args = _format_args({"uuid": "5a969404-1111-2222-3333-444455556666"}, "UTC")
    assert args["uuid"] == "5A969404"
    assert _format_args({}, "UTC")["uuid"] == ""
