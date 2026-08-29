"""Client-facing names for the things the therapist creates (§15).

`app/render/dates.py` is the same idea for a day: a label a client reads,
assembled from the catalogue, in one place so that two channels cannot word it
differently. This module holds the ones that are named by a *row* rather than by
a key the catalogue ships with.

The admin surface deliberately does not use these. It shows the `code`, which is
what she typed and what she searches for, and DESIGN.md §11 keeps that surface
in English anyway.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.services.translations import get_text


async def session_type_name(session: AsyncSession, lang: str, code: str) -> str:
    """What to call a session type in `lang`.

    Session types are rows so that adding "supervision" is an insert rather than
    a migration (§6.4), which means their names cannot live in the locale files:
    the catalogue ships `booking.type.individual` and `booking.type.couple`
    because those are seeded, and a type the therapist adds afterwards has a key
    nobody has written yet. §15 makes `get_text` return the key itself in that
    case, so a type she called "superme" was offered to clients, on both
    channels, as `booking.type.superme`.

    The admin form writes the name in all three languages, so the ordinary case
    is a real one. This is the answer for the gap before she fills it in, and
    for a type created before that form existed: her own code is not a
    translation, but it is a word rather than an identifier, and it is the word
    she chose.
    """
    key = f"booking.type.{code}"
    name = await get_text(session, lang, key)
    return code if name == key else name


__all__ = ["session_type_name"]
