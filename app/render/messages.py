"""Intent + locale + channel -> RenderedMessage (IMPLEMENTATION.md §9, §10).

The core emitted a semantic intent; this is where it becomes words. Each channel
gets the same facts expressed the way that channel can carry them: Telegram
inline buttons, email signed links, the web form buttons (DESIGN.md §3.2).

Times are formatted here, at the edge, in the recipient's timezone. Storage is
UTC everywhere behind this line.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import Action, RenderedMessage
from app.core.enums import Channel
from app.core.services.translations import get_text
from app.render.intents import action_label_key, body_key_for, spec_for
from app.render.markdown import escape_telegram

#: Telegram HTML. Never the MarkdownV2 parse mode (hard rule 6).
TELEGRAM_PARSE_MODE = "HTML"


def format_instant(value: str | datetime | None, tz: str) -> str:
    """Render a stored instant in the recipient's zone.

    Falls back to the raw string when the payload carried free text -- a client
    who wrote "some evening next week" gets their own words back, not a parse
    error.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
    else:
        parsed = value
    return parsed.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


async def render(
    session: AsyncSession,
    *,
    intent_key: str,
    payload: dict[str, Any],
    locale: str,
    channel: Channel,
    tz: str,
    base_url: str,
    request_id: int | None = None,
) -> RenderedMessage:
    """Build the message one transport is about to send.

    `request_id` is what a Telegram button carries back (§9). It is a column on
    the outbox row, not a payload field -- §10's payload schemas do not list it
    -- so the caller has to hand it over.
    """
    spec = spec_for(intent_key)
    fmt = _format_args(payload, tz)

    body = await get_text(session, locale, body_key_for(intent_key, payload), **fmt)
    lines = [body]

    for field, key in spec.optional_parts:
        if payload.get(field):
            lines.append(await get_text(session, locale, key, **fmt))

    lines.extend(await _join_lines(session, intent_key, payload, locale, channel, fmt))

    actions = await _actions(session, spec.key, payload, locale, channel, base_url, request_id)
    subject = (
        await get_text(session, locale, spec.email_subject_key)
        if channel == Channel.email and spec.email_subject_key
        else None
    )

    if channel == Channel.telegram:
        parts = [escape_telegram("\n\n".join(lines))]
        return RenderedMessage(
            parts=parts,
            subject=None,
            actions=actions,
            parse_mode=TELEGRAM_PARSE_MODE,
        )

    if channel == Channel.email:
        # §13.4: links, not interactivity. The footer explains why the message
        # arrived at all.
        footer = await get_text(session, locale, "email.footer")
        text = "\n\n".join([*lines, *[f"{a.label}: {a.url}" for a in actions if a.url], footer])
        return RenderedMessage(parts=[text], subject=subject, actions=actions, parse_mode=None)

    return RenderedMessage(
        parts=["\n\n".join(lines)], subject=subject, actions=actions, parse_mode=None
    )


def _format_args(payload: dict[str, Any], tz: str) -> dict[str, Any]:
    """Payload -> `str.format` arguments.

    Every key the catalogue's placeholders might name is present, so a template
    referring to one this intent does not carry degrades to the unformatted
    string rather than raising (§15).
    """
    args = dict(payload)
    args["time"] = format_instant(payload.get("time"), tz)
    args.setdefault("uuid", "")
    args.setdefault("name", "")
    args.setdefault("reason", "")
    args.setdefault("note", "")
    args.setdefault("url", payload.get("join_url") or "")
    args.setdefault("error", "")
    args.setdefault("address", "")
    args.setdefault("intent", "")
    args.setdefault("minutes", "")
    return args


async def _join_lines(
    session: AsyncSession,
    intent_key: str,
    payload: dict[str, Any],
    locale: str,
    channel: Channel,
    fmt: dict[str, Any],
) -> list[str]:
    """§10's join info, which only some intents carry and email never does.

    The notification service already stripped `join_url` from email payloads;
    this is the second half of the same rule, and the reason an email links to
    /r/{uuid} instead.
    """
    if intent_key not in ("request.confirmed.client", "reminder.client"):
        return []
    url = payload.get("join_url")
    if not url or channel == Channel.email:
        return []

    key = (
        "intent.request.confirmed.client.join_onsite"
        if payload.get("modality") == "onsite"
        else "intent.request.confirmed.client.join_online"
    )
    return [await get_text(session, locale, key, **{**fmt, "url": url})]


def _request_url(base_url: str, payload: dict[str, Any]) -> str:
    """§12.1: a link about a request opens the request.

    The `view_request` token the notification service minted rides along, so the
    client is not sent to a sign-in form to reach their own booking. Without one
    the link still works for anyone already signed in.
    """
    url = f"{base_url}/r/{payload.get('uuid', '')}"
    token = payload.get("view_token")
    return f"{url}?token={quote(str(token))}" if token else url


async def _actions(
    session: AsyncSession,
    intent_key: str,
    payload: dict[str, Any],
    locale: str,
    channel: Channel,
    base_url: str,
    request_id: int | None = None,
) -> list[Action]:
    spec = spec_for(intent_key)
    if not spec.actions:
        return []

    actions: list[Action] = []

    for key in spec.actions:
        label = await get_text(session, locale, action_label_key(intent_key, key))

        if channel == Channel.telegram:
            # "Open your booking" is an email affordance (§13.4: email gets
            # links, not interactivity). In Telegram the conversation is the
            # interface, and a client there has no web session to open with.
            if key == "open":
                continue
            # §9: `<action>:<request_id>`, comfortably inside 64 bytes because
            # the id is short and the handler looks the rest up. Without the id
            # the router has nothing to act on and the button does nothing, so
            # an intent with actions and no request is not worth a keyboard.
            if request_id is None:
                continue
            actions.append(
                Action(key=key, label=label, callback_data=f"{key}:{request_id}")
            )
        else:
            url = payload.get("url") if key == "open" else None
            actions.append(
                Action(key=key, label=label, url=url or _request_url(base_url, payload))
            )

    return actions
