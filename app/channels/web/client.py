"""Client web routes (IMPLEMENTATION.md §12.1).

A thin adapter, exactly like the Telegram router: every scheduling decision is a
call into app/core/services/. The two channels differ in what they can express,
never in what the rules are (DESIGN.md §3.2).

Multi-step state lives in `flow_state` keyed on the `web` channel, so a client
mid-booking here and mid-booking in Telegram are two independent flows.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.web import captcha, ratelimit
from app.channels.web.security import (
    CSRF_FIELD,
    client_session_value,
    csrf_ok,
    csrf_token_for,
    current_client_id,
    issue_client_session,
    issue_csrf,
)
from app.config import get_settings
from app.core.enums import Channel, Modality, SenderType, SlotStatus, TokenPurpose
from app.core.errors import DomainError, NotFound, SlotUnavailable, TokenInvalid
from app.core.models import (
    BookingRequest,
    Client,
    NegotiationMessage,
    Practice,
    SessionType,
    Slot,
    TimezoneOption,
)
from app.core.policies import (
    CLIENT_TEXT_MAX_CHARS,
    BookingPath,
    hold_expiry,
    now_utc,
    parse_client_time,
    resolve_booking_mode,
)
from app.core.services import booking, content, flow, notifications, waitlist
from app.core.services.clients import (
    consume_token,
    issue_token,
    link_identity,
    magic_link_allowance_left,
    normalise_email,
    resolve_client,
    set_client_language,
    token_target,
    verified_email_client,
)
from app.core.services.flow import Step
from app.core.services.notifications import Envelope, Recipient
from app.core.services.settings import get_practice
from app.core.services.slots import list_available_slots
from app.core.services.translations import get_text
from app.db import unit_of_work
from app.render.dates import day_label, when_label
from app.render.labels import price_text, session_type_name
from app.render.markdown import to_web_html
from app.render.zones import zone_label

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory="app/channels/web/templates")

#: How far ahead the slot picker looks.
SLOT_WINDOW = timedelta(days=30)

#: §12.1: the front page's own blocks, and what a client reads when they choose
#: to meet online. Both are content topics reached by name; the codes are core's
#: (`app/core/services/content.py`), since Telegram reaches for the same two.
HOME_TOPIC = content.HOME_TOPIC
ONLINE_PLATFORMS_TOPIC = content.ONLINE_PLATFORMS_TOPIC

#: §12.1: set on the redirect out of an email link, and nowhere else. A
#: greeting keyed on the session would follow a returning client around for a
#: fortnight.
WELCOME_PARAM = "welcome"

#: Set client-side from Intl.DateTimeFormat (DESIGN.md §8).
TZ_COOKIE = "pb_tz"

#: The last language this browser asked for. The switcher is a setting, not a
#: query parameter, and none of the links on a page carry one.
LANG_COOKIE = "pb_lang"
LANG_COOKIE_MAX_AGE = 365 * 24 * 3600

LANGUAGES = ("ru", "hy", "en")


# --- Page context -----------------------------------------------------------


async def _book_summary(session: AsyncSession, lang: str, book: dict[str, Any]) -> tuple[str, str]:
    """(when, session type) of a booking a login link would file."""
    practice = await get_practice(session)
    zone = str(book.get("tz") or "")
    if not _valid_zone(zone):
        zone = practice.timezone
    starts = (
        await session.execute(select(Slot.starts_at).where(Slot.id == int(book["slot_id"])))
    ).scalar_one_or_none()
    when = await when_label(session, lang, starts, zone) if starts else "?"
    code = (
        await session.execute(
            select(SessionType.code).where(SessionType.id == int(book["session_type_id"]))
        )
    ).scalar_one_or_none()
    return when, await session_type_name(session, lang, code) if code else "?"


async def _carried_booking_text(
    session: AsyncSession, context: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    book = payload.get("book")
    if not isinstance(book, dict):
        return {}
    try:
        when, kind = await _book_summary(session, context["lang"], book)
    except (KeyError, ValueError):
        return {}
    return {
        "book_text": await get_text(
            session, context["lang"], "auth.confirm_book", when=when, session_type=kind
        )
    }


async def not_found_page(request: Request) -> Response:
    """The client site's 404 for a path that is no route at all (registered in app.main)."""
    async with unit_of_work() as session:
        context = await _context(session, request)
        return await _error_page(
            context, context["t"]["not_found_title"], context["t"]["not_found"], 404, action="home"
        )


async def _stale_page(request: Request, *, kept: str = "") -> Response:
    """A form posted with a CSRF token that ran out (the page sat open too long): 403, and a sentence that says so.

    Not a blank response. The text the visitor typed is shown back (`kept`) where the form cannot be re-rendered.
    """
    async with unit_of_work() as session:
        context = await _context(session, request)
        return await _error_page(
            context,
            context["t"]["error_title"],
            context["t"]["stale"],
            403,
            action="home",
            kept=kept,
        )


async def _details_page(
    session: AsyncSession,
    request: Request,
    context: dict[str, Any],
    client: Client | None,
    reservation: dict[str, Any],
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    status: int = 200,
) -> Response:
    """§12.1 step 3. Also the answer to a refused submission: the same form, what was typed kept, the message beside
    the field it is about (`errors` is keyed by field name; `form` is for a message about the whole form)."""
    t = context["t"]
    lang = context["lang"]
    # `booking.slot.held` carries {minutes}: the hold window the client is racing, so it must be a number and not
    # the placeholder (§15).
    labels = {
        **t,
        "slot_held": await get_text(
            session, lang, "booking.slot.held", minutes=context["practice"].slot_hold_minutes
        ),
    }

    # §12.1: the notice names the instant it is holding, not merely that something is held. A reservation made before
    # this field existed carries no time, and the banner renders exactly as it did.
    held_raw = reservation.get("starts_at")
    held_when = (
        await _format(session, datetime.fromisoformat(held_raw), context, reservation.get("tz"))
        if held_raw
        else None
    )
    # What she is about to send, repeated from the picker with a way back (quick win 12).
    code = (
        await session.execute(
            select(SessionType.code).where(SessionType.id == int(reservation["session_type_id"]))
        )
    ).scalar_one_or_none()
    modality = str(reservation.get("modality", "online"))
    summary = " · ".join(
        part
        for part in (
            await session_type_name(session, lang, code) if code else "",
            t["modality_onsite"] if modality == "onsite" else t["modality_online"],
        )
        if part
    )

    return _render(
        "details.html",
        {
            **context,
            "t": labels,
            "held_when": held_when,
            "summary": summary,
            "signed_in": client is not None,
            # §12.1: both prefills need a session. At this step an unsigned visitor has not identified themselves --
            # the address arrives at submit -- so filling either from a typed one would tell whoever guessed it that
            # the address is known here, and whose it is.
            "display_name": client.display_name if client else None,
            "contact_note": await booking.last_contact_note(session, client.id) if client else None,
            "captcha": _challenge(context["practice"]),
            "values": values or {},
            "errors": errors or {},
        },
        status_code=status,
    )


async def _waitlist_form(
    context: dict[str, Any],
    practice: Practice,
    *,
    by_choice: bool,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    status: int = 200,
) -> Response:
    return _render(
        "waitlist.html",
        {
            **context,
            "by_choice": by_choice,
            "captcha": _challenge(practice),
            "values": values or {},
            "errors": errors or {},
        },
        status_code=status,
    )


async def _queue_login_link(
    session: AsyncSession,
    *,
    client_id: UUID,
    email: str,
    request_uuid: UUID | None = None,
    book: dict[str, str] | None = None,
    summary: tuple[str, str] | None = None,
) -> None:
    """Queue `auth.login_link.client` to an address that has not proved itself.

    §13.3 makes this the one intent allowed to reach an unverified address,
    because following the link is what verifies it. It carries the Telegram
    deep link too: the merge has to be behind the same proof, or attaching a
    Telegram account to somebody else's client record costs nothing but knowing
    their email (DESIGN.md §5.1).

    `request_uuid` is where the link lands once it has signed the client in.
    Sent from the booking flow it is the request they have just made -- the mail
    says "open your booking", and it used to open the front page, leaving the
    client to find their way back to a request whose only address is a UUID they
    were shown once. The same link sent from `/auth/email` has no request behind
    it and still lands on the front page, which is the right answer there.
    """
    settings = get_settings()
    payload: dict[str, str] = {"email": email}
    if request_uuid is not None:
        payload["request"] = str(request_uuid)
    if book is not None:
        # A proved owner on a new browser: the choice rides in the token and is
        # filed when the link is followed (the problem text is not carried).
        payload["book"] = book  # type: ignore[assignment]
    raw = await issue_token(session, TokenPurpose.login, client_id=client_id, payload=payload)
    link_raw = await issue_token(session, TokenPurpose.link_channel, client_id=client_id)
    # A link that will file a booking says so in the mail (and on the page).
    carried = {"when": summary[0], "session_type": summary[1]} if book and summary else {}
    await notifications.enqueue(
        session,
        Envelope(
            "auth.login_link.client",
            Recipient.client,
            {
                **carried,
                "url": f"{settings.base_url}/auth/callback?token={raw}",
                "minutes": 30,
                "telegram_url": (
                    f"https://t.me/{settings.telegram_bot_username}?start=link_{link_raw}"
                ),
            },
            # §13.3: this intent addresses itself. Left to the general policy the
            # row is never written at all, since the address it is about is
            # unverified by definition.
            client_id=client_id,
            to=(Channel.email, email),
        ),
    )


async def _labels(session: AsyncSession, lang: str) -> dict[str, str]:
    """The strings every page needs.

    Fetched by key rather than hardcoded so the therapist's edits win (§15).
    """
    keys = {
        "consultation": "menu.consultation",
        "welcome": "common.welcome",
        "home": "common.home",
        "back": "common.back",
        "submit": "common.send_request",
        "skip": "common.skip",
        "content_empty": "content.empty",
        "choose_type": "booking.choose_type",
        "choose_modality": "booking.choose_modality",
        "modality_online": "booking.modality.online",
        "modality_onsite": "booking.modality.onsite",
        "onsite_info": "booking.onsite_info",
        "onsite_no_address": "booking.onsite_no_address",
        "choose_timezone": "booking.choose_timezone",
        "timezone_shown": "booking.timezone.shown",
        "email_hint": "booking.email_hint",
        "choose_slot": "booking.choose_slot_web",
        "no_slots": "booking.slot.none_available",
        "slot_held": "booking.slot.held",
        "slot_taken": "booking.slot.taken",
        "ask_problem": "booking.ask_problem",
        "ask_name": "booking.ask_name",
        "ask_contact": "booking.ask_contact",
        "submitted": "booking.submitted",
        "unavailable": "booking.unavailable",
        "waitlist_intro": "waitlist.intro",
        # The same form reached by choice rather than because there is nothing
        # free (§12.1), so it opens on a different sentence and offers itself
        # from under the picker.
        "waitlist_intro_by_choice": "waitlist.intro_by_choice",
        "waitlist_from_picker": "waitlist.from_picker",
        "waitlist_problem": "waitlist.ask_problem",
        "waitlist_contact": "waitlist.ask_contact",
        "waitlist_submitted": "waitlist.submitted",
        "email_label": "auth.email_label",
        "sign_in": "auth.sign_in",
        "continue_btn": "common.continue",
        "confirm_book_yes": "auth.confirm_book_yes",
        "confirm_book_no": "auth.confirm_book_no",
        "send_link": "auth.send_link",
        "link_sent": "auth.link_sent",
        "your_request": "request.title",
        "thread": "request.thread",
        "add_note": "request.add_note",
        "note_ask": "request.note_ask",
        "when": "request.when",
        "accept": "intent.request.proposal.client.action.accept",
        "counter": "intent.request.proposal.client.action.counter",
        "decline": "intent.request.proposal.client.action.decline",
        "counter_ask": "intent.request.counter.client.ask",
        "counter_other_time": "request.counter.other_time",
        "counter_time_label": "request.counter.time_label",
        "counter_note_label": "request.counter.note_label",
        "counter_join_waitlist": "request.counter.join_waitlist",
        "counter_time_hint": "request.counter.time_hint",
        "connect_telegram": "intent.auth.login_link.client.action.telegram",
        "telegram_hint": "intent.auth.login_link.client.telegram_hint",
        "error": "common.error.generic",
        "error_title": "common.error.title",
        "not_found_title": "common.error.not_found_title",
        "expired_link_title": "common.error.expired_link_title",
        "stale": "common.error.stale",
        "slot_taken_title": "booking.slot.taken_title",
        "choose_another": "booking.choose_another",
        "change": "common.change",
        "optional": "common.optional",
        "step": "common.step",
        "noscript": "common.noscript",
        "skip_to_content": "common.skip_to_content",
        "waitlist_join": "waitlist.join",
        "you": "request.you",
        "reference": "request.reference",
        "decline_ask": "request.decline_ask",
        "decline_yes": "request.decline_yes",
        "email_invalid": "booking.contact.email_invalid",
        "expired_link": "common.error.expired_link",
        "not_found": "common.error.not_found",
        "captcha_failed": "common.error.captcha",
        "captcha_wait": "booking.captcha_wait",
    }
    return {name: await get_text(session, lang, key) for name, key in keys.items()}


async def _context(
    session: AsyncSession, request: Request, lang: str | None = None
) -> dict[str, Any]:
    practice = await get_practice(session)

    # A signed-in client carries their own language, the same one Telegram set
    # (§13.1 step 2), so the two channels do not disagree about a person.
    client = await _session_client(session, request)
    default = client.language if client is not None else practice.default_language
    resolved = _language(request, default, lang)

    if client is not None and _chosen_language(request) is not None:
        if client.language != resolved:
            await set_client_language(session, client.id, resolved)

    topics = []
    for topic in await content.list_menu_topics(session):
        topics.append(
            {
                "code": topic.code,
                "title": await get_text(session, resolved, f"content.topic.{topic.code}.title"),
            }
        )

    return {
        "request": request,
        "practice": practice,
        "lang": resolved,
        "topics": topics,
        "t": await _labels(session, resolved),
        "csrf_token": csrf_token_for(request),
        "signed_in": client is not None,
        "email_enabled": get_settings().email_enabled,
        "errors": {},
        "values": {},
        # §17's free-text cap, so the form can stop somebody typing three pages
        # before the server tells them no. The refusal is still the core's.
        "text_max": CLIENT_TEXT_MAX_CHARS,
    }


def _challenge(practice: Practice) -> str | None:
    """§17: a proof-of-work challenge for a form that needs one, `None` while
    the therapist has the gate switched off — which is its default."""
    if not practice.captcha_on:
        return None
    return captcha.issue(practice.captcha_difficulty)


def _challenge_ok(practice: Practice, challenge: str, solution: str) -> bool:
    """Whether a submission may proceed. Trivially true with the gate off, so
    the two write paths read the same either way."""
    if not practice.captcha_on:
        return True
    return captcha.verify(challenge, solution)


async def _greeting(
    session: AsyncSession,
    request: Request,
    lang: str,
    *,
    client: Client | None = None,
    arrived_by_token: bool = False,
) -> str | None:
    """§12.1: the line a client reads on the page their email link opened.

    `None` on every other visit. The two ways in are the same arrival from the
    same inbox: the `welcome` marker the login callback redirects with, and a
    `view_request` token spent on the page it was sent about.

    A client who never gave a name gets a whole sentence of its own rather than
    a noun substituted into this one -- "Hello, {name}" filled with a word for
    "client" reads in Russian and Armenian like a form somebody abandoned.
    """
    if not arrived_by_token and request.query_params.get(WELCOME_PARAM) != "1":
        return None

    if client is None:
        client = await _session_client(session, request)
    if client is None:
        return None

    name = (client.display_name or "").strip()
    if not name:
        return await get_text(session, lang, "auth.greeting_unnamed")
    return await get_text(session, lang, "auth.greeting", name=name)


def _chosen_language(request: Request) -> str | None:
    """The language this request explicitly asks for, if it asks for one."""
    query = request.query_params.get("lang")
    return query if query in LANGUAGES else None


def _language(request: Request, default: str, override: str | None) -> str:
    """DESIGN.md §11's two client languages, in order of how explicit the
    choice is: this request, then the last one this browser made, then whatever
    the caller considers the default.

    Without the cookie the switcher only lasted as long as the query string,
    and the first nav link -- none of which carry `?lang=` -- put the page back
    into the practice language.
    """
    if override in LANGUAGES:
        return override

    chosen = _chosen_language(request)
    if chosen is not None:
        return chosen

    remembered = request.cookies.get(LANG_COOKIE)
    if remembered in LANGUAGES:
        return remembered

    return default


def _valid_zone(candidate: str | None) -> bool:
    if not candidate:
        return False
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _valid_modality(candidate: str | None) -> bool:
    return candidate in {m.value for m in Modality}


def _timezone(request: Request, practice_tz: str) -> str:
    """The client's zone, in DESIGN.md §8's order: explicit choice, then the
    browser's detection, then the practice default."""
    for candidate in (request.query_params.get("tz"), request.cookies.get(TZ_COOKIE)):
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return candidate
    return practice_tz


async def _error_page(
    context: dict[str, Any],
    heading: str,
    message: str,
    status: int,
    *,
    action: str = "home",
    kept: str = "",
) -> Response:
    """An error page that says what happened and offers the next step that fits it, not only "Home".

    `kept` is text the visitor typed into a form that could not be processed: it is shown back so it can be copied.
    """
    t = context["t"]
    href, label = {
        "home": ("/", t["home"]),
        "book": ("/book", t["choose_another"]),
        "link": ("/auth/email", t["send_link"]),
    }[action]
    return _render(
        "error.html",
        {
            **context,
            "heading": heading,
            "message": message,
            "action_href": href,
            "action_label": label,
            "kept": kept,
        },
        status_code=status,
    )


def _short_ref(uuid: UUID) -> str:
    """The reference a client can read out on the phone: the first block of the uuid, upper case."""
    return str(uuid).split("-")[0].upper()


def _render(name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    # Starlette takes the request first; the older (name, context) form is gone.
    response = TEMPLATES.TemplateResponse(
        context["request"], name, context, status_code=status_code
    )
    # The cookie must carry exactly what the page embedded, or every form on
    # it would be rejected.
    issue_csrf(response, context.get("csrf_token"))
    # The HTMX partials build their own context and carry no language.
    if context.get("lang"):
        _remember_language(response, context["request"], context["lang"])
    return response


def _remember_language(response: Response, request: Request, lang: str) -> None:
    """Persist an explicit switch, and nothing else.

    Only a request that asked for a language writes the cookie: pinning the
    practice default on every visitor would freeze a later change to it for
    everyone who had ever loaded a page.
    """
    if _chosen_language(request) is None:
        return
    response.set_cookie(
        LANG_COOKIE,
        lang,
        max_age=LANG_COOKIE_MAX_AGE,
        httponly=True,
        secure=get_settings().base_url.startswith("https://"),
        samesite="lax",
        path="/",
    )


def build_router() -> APIRouter:
    router = APIRouter()

    # --- Content ------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home(request: Request) -> Response:
        async with unit_of_work() as session:
            context = await _context(session, request)
            # §12.1: the practice's own words, written at /admin/content like
            # any other topic. The topic list stays as the fallback while she
            # has not written them -- an empty front page would be worse than
            # the duplicated navigation it replaces.
            blocks = await content.get_topic_blocks(session, HOME_TOPIC, context["lang"])
            return _render(
                "home.html",
                {
                    **context,
                    "intro": [to_web_html(block.body_md) for block in blocks],
                    "greeting": await _greeting(session, request, context["lang"]),
                },
            )

    @router.get("/t/{topic_code}", response_class=HTMLResponse, include_in_schema=False)
    async def topic_page(request: Request, topic_code: str) -> Response:
        async with unit_of_work() as session:
            context = await _context(session, request)
            try:
                await content.get_topic(session, topic_code)
            except NotFound:
                return await _error_page(
                    context,
                    context["t"]["not_found_title"],
                    context["t"]["not_found"],
                    404,
                    action="home",
                )

            blocks = await content.get_topic_blocks(session, topic_code, context["lang"])
            title = await get_text(session, context["lang"], f"content.topic.{topic_code}.title")
            # §11.3: sanitised by the emitter before it reaches a template.
            return _render(
                "topic.html",
                {
                    **context,
                    "title": title,
                    "blocks": [to_web_html(block.body_md) for block in blocks],
                },
            )

    # --- Booking (§12.1 steps 1-3) -----------------------------------------

    @router.get("/book", response_class=HTMLResponse, include_in_schema=False)
    async def book(request: Request) -> Response:
        async with unit_of_work() as session:
            context = await _context(session, request)
            practice = context["practice"]
            tz = _timezone(request, practice.timezone)

            slots = await list_available_slots(
                session,
                window_from=now_utc(),
                window_to=now_utc() + SLOT_WINDOW,
                tz=tz,
            )
            resolved = resolve_booking_mode(practice, slots_exist=bool(slots))

            if resolved.path is BookingPath.waitlist:
                # Not by choice: there is nothing to choose from, which is what
                # `waitlist.intro` says.
                return _render(
                    "waitlist.html",
                    {**context, "by_choice": False, "captcha": _challenge(practice)},
                )

            session_types = []
            for st in await _active_session_types(session):
                name = await session_type_name(session, context["lang"], st.code)
                price = price_text(st)
                session_types.append(
                    {
                        "id": st.id,
                        # A price is shown only when she stored one (quick win 13); never invented.
                        "label": await get_text(
                            session,
                            context["lang"],
                            "booking.type.with_price",
                            name=name,
                            duration=st.duration_min,
                            price=price,
                        )
                        if price
                        else await get_text(
                            session,
                            context["lang"],
                            "booking.type.without_price",
                            name=name,
                            duration=st.duration_min,
                        ),
                    }
                )

            options = (
                (
                    await session.execute(
                        select(TimezoneOption)
                        .where(TimezoneOption.is_active.is_(True))
                        .order_by(TimezoneOption.sort_order, TimezoneOption.id)
                    )
                )
                .scalars()
                .all()
            )
            # Localised city and GMT offset, IANA ids stay the values (item 2). A zone the browser reported that is
            # not on her list is added, so a visitor from Almaty is not silently put in Yerevan (quick win 1).
            now = now_utc()
            ids = [option.iana_name for option in options]
            if tz not in ids:
                ids.append(tz)
            timezones = [
                {"iana_name": iana, "label": zone_label(iana, context["lang"], now)} for iana in ids
            ]

            labels = context["t"]

            # §12.1: the twin of the on-site rule below it. Someone choosing to
            # meet online is told which platforms that means, where the choice
            # is made rather than in the confirmation afterwards.
            platforms = await content.get_topic_blocks(
                session, ONLINE_PLATFORMS_TOPIC, context["lang"]
            )

            return _render(
                "book.html",
                {
                    **context,
                    "t": labels,
                    "session_types": session_types,
                    "timezones": timezones,
                    "tz": tz,
                    "negotiation": resolved.path is BookingPath.negotiation,
                    "online_info": [to_web_html(block.body_md) for block in platforms],
                },
            )

    @router.get("/book/slots", response_class=HTMLResponse, include_in_schema=False)
    async def book_slots(
        request: Request,
        session_type_id: int | None = None,
        modality: str | None = None,
        tz: str | None = None,
    ) -> Response:
        """§12.1 step 2, an HTMX partial. Times in the client's timezone."""
        if modality and not _valid_modality(modality):
            return Response(status_code=400)
        async with unit_of_work() as session:
            practice = await get_practice(session)
            lang = _language(request, practice.default_language, None)
            zone = tz if _valid_zone(tz) else _timezone(request, practice.timezone)

            slots = await list_available_slots(
                session,
                window_from=now_utc(),
                window_to=now_utc() + SLOT_WINDOW,
                session_type_id=session_type_id,
                modality=Modality(modality) if modality else None,
                tz=zone,
            )

            # §12.1: nothing this way, something the other. Offered as a
            # labelled switch rather than by quietly listing the other kind --
            # a client looking at in-person times after asking for online has
            # to work out what happened, where a button saying so tells them.
            chosen = Modality(modality) if modality else None
            switch_to: Modality | None = None
            # Nothing to switch to when only one modality is on offer (§6.1).
            if not slots and chosen is not None and not practice.online_only:
                other = Modality.online if chosen is Modality.onsite else Modality.onsite
                if await list_available_slots(
                    session,
                    window_from=now_utc(),
                    window_to=now_utc() + SLOT_WINDOW,
                    session_type_id=session_type_id,
                    modality=other,
                    tz=zone,
                ):
                    switch_to = other

            # Grouped by the calendar date, not by the heading it will be given:
            # keying a structure on rendered text is how two days quietly become
            # one when the wording changes.
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            starts: dict[str, datetime] = {}
            for slot in slots:
                local = slot.starts_at_utc.astimezone(ZoneInfo(zone))
                day = local.date().isoformat()
                starts.setdefault(day, slot.starts_at_utc)
                grouped[day].append({"id": slot.id, "label": local.strftime("%H:%M")})

            days = []
            for day, times in grouped.items():
                heading = await day_label(session, lang, starts[day], zone)
                # A button named only "18:00" says nothing out of context (WCAG 2.4.6): the day goes in its name.
                for time in times:
                    time["aria"] = f"{heading}, {time['label']}"
                days.append((heading, times))

            return _render(
                "partials/slots.html",
                {
                    "request": request,
                    "days": days,
                    "t": await _labels(session, lang),
                    "csrf_token": csrf_token_for(request),
                    "session_type_id": session_type_id or "",
                    "modality": modality or "online",
                    "tz": zone,
                    # Beside the times, so it changes with them when the select does (quick win 2).
                    "zone_line": await get_text(
                        session,
                        lang,
                        "booking.timezone.shown",
                        zone=zone_label(zone, lang, now_utc()),
                    ),
                    "switch_to": switch_to.value if switch_to else None,
                    "switch_label": (
                        await get_text(session, lang, f"booking.switch.{switch_to.value}")
                        if switch_to
                        else ""
                    ),
                    "none_this_way": (
                        await get_text(session, lang, f"booking.slot.none_{chosen.value}")
                        if switch_to and chosen
                        else ""
                    ),
                },
            )

    @router.post("/book/hold", include_in_schema=False)
    async def book_hold(
        request: Request,
        slot_id: int = Form(...),
        session_type_id: int = Form(...),
        modality: str = Form("online"),
        tz: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """§12.1: hold a slot; returns the hold expiry.

        DEVIATION, reported in M5 and unchanged here: the reservation is
        recorded against the flow and the *database* hold is taken at submit.
        §6.4's CHECK makes `held_by_request` non-null whenever a slot is held,
        so a real hold needs a request row, and creating one here would notify
        the therapist about a form nobody has filled in yet. The expiry below is
        the window that will apply, `booking.slot.taken` exists in the catalogue
        for the race this leaves, and the M2 concurrency test guarantees exactly
        one winner. Resolving it properly wants a nullable hold owner or a
        two-phase submit.
        """
        if not csrf_ok(request, csrf_token):
            return await _stale_page(request)
        if not _valid_modality(modality):
            return Response(status_code=400)
        if tz and not _valid_zone(tz):
            return Response(status_code=400)

        async with unit_of_work() as session:
            practice = await get_practice(session)
            client = await _session_client(session, request)
            zone = tz or _timezone(request, practice.timezone)

            # §12.1: step 3 names the instant it is holding, so the instant is
            # kept beside the choice rather than looked up again on every
            # render of that page. A slot is created, blocked or deleted and
            # never moved, so `starts_at` for a given id cannot drift away from
            # this copy. The `hold_expires_at` beside it already sets the
            # precedent for a derived time living in this dict.
            starts_at = (
                await session.execute(select(Slot.starts_at).where(Slot.id == slot_id))
            ).scalar_one_or_none()

            reservation = {
                "slot_id": slot_id,
                "session_type_id": session_type_id,
                "modality": modality,
                "tz": zone,
                "starts_at": starts_at.isoformat() if starts_at else None,
                "hold_expires_at": hold_expiry(practice).isoformat(),
            }

            if client is not None:
                await flow.set_step(
                    session, client.id, Channel.web, Step.entering_problem, replace=reservation
                )
                response: Response = RedirectResponse("/book/details", status_code=303)
            else:
                # Not signed in yet: carry the reservation in a signed cookie
                # until the email on the details form identifies them.
                response = RedirectResponse("/book/details", status_code=303)
                _stash(response, reservation)

            issue_csrf(response, csrf_token_for(request))
            return response

    @router.get("/book/details", response_class=HTMLResponse, include_in_schema=False)
    async def book_details(request: Request) -> Response:
        """§12.1 step 3: problem, name, contact note."""
        async with unit_of_work() as session:
            context = await _context(session, request)
            client = await _session_client(session, request)
            reservation = await _reservation(session, request, client)

            if reservation is None:
                return RedirectResponse("/book", status_code=303)
            return await _details_page(session, request, context, client, reservation)

    @router.post("/book", include_in_schema=False)
    async def submit(
        request: Request,
        problem: str = Form(""),
        name: str = Form(""),
        contact: str = Form(""),
        email: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
        pow_challenge: str = Form(""),
        pow_solution: str = Form(""),
    ) -> Response:
        """§12.1: submit; returns the confirmation with the request UUID."""
        typed = {"problem": problem, "name": name, "contact": contact, "email": email}
        if not csrf_ok(request, csrf_token):
            # The page sat open too long: show the same form again, with what was typed, and say so.
            async with unit_of_work() as session:
                context = await _context(session, request)
                client = await _session_client(session, request)
                reservation = await _reservation(session, request, client)
                if reservation is None:
                    return await _stale_page(request)
                return await _details_page(
                    session,
                    request,
                    context,
                    client,
                    reservation,
                    values=typed,
                    errors={"form": context["t"]["stale"]},
                    status=403,
                )
        settings_now = get_settings()
        can_link = settings_now.email_enabled

        async with unit_of_work() as session:
            context = await _context(session, request)
            # Per IP, from the helper every limiter uses. Before the gate and
            # before anything is created: a flood that finds fresh addresses
            # still runs into this.
            practice = context["practice"]
            if not ratelimit.check(
                ratelimit.Limit("book_ip", practice.book_per_ip_per_hour, ratelimit.BOOKING_WINDOW),
                ratelimit.client_ip(request),
            ):
                return await _error_page(
                    context,
                    context["t"]["error_title"],
                    await get_text(session, context["lang"], "error.rate_limited"),
                    429,
                    action="home",
                )
            # §17: before anything is created. A refused submission must cost
            # the practice a row, a notification and an email less than an
            # accepted one, which is the entire point of the gate.
            if not _challenge_ok(context["practice"], pow_challenge, pow_solution):
                return await _error_page(
                    context,
                    context["t"]["error_title"],
                    context["t"]["captcha_failed"],
                    400,
                    action="home",
                )
            client = await _session_client(session, request)
            # Whether this browser had already proved who it belongs to. A
            # session now comes only from consuming a token, so it is proof;
            # a typed address is not, however well-formed.
            proven = client is not None
            reservation = await _reservation(session, request, client)

            if reservation is None:
                return RedirectResponse("/book", status_code=303)

            if client is None:
                if not email:
                    return RedirectResponse("/book/details", status_code=303)
                address = normalise_email(email)
                if address is None:
                    return await _details_page(
                        session,
                        request,
                        context,
                        None,
                        reservation,
                        values=typed,
                        errors={"email": context["t"]["email_invalid"]},
                        status=400,
                    )
                email = address
                # One answer for "no link left", before anything says whether the
                # address is known: a proved owner gets the refusal a new
                # address gets, and neither loses a booking silently.
                if can_link and await magic_link_allowance_left(session, address) <= 0:
                    return await _error_page(
                        context,
                        context["t"]["error_title"],
                        await get_text(session, context["lang"], "error.rate_limited"),
                        429,
                        action="home",
                    )
                owner = await verified_email_client(session, address)
                if owner is not None:
                    if not can_link:
                        return await _error_page(
                            context,
                            context["t"]["error_title"],
                            context["t"]["unavailable"],
                            409,
                            action="home",
                        )
                    # A proved address is somebody's real record. A stranger
                    # typing it files nothing there: the owner is sent a link
                    # carrying the choice, which is filed when it is followed,
                    # and the page says what it says for a new address.
                    book = {
                        "slot_id": str(reservation["slot_id"]),
                        "session_type_id": str(reservation["session_type_id"]),
                        "modality": str(reservation.get("modality", "online")),
                        "tz": str(reservation.get("tz") or ""),
                        "name": (name or "")[:200],
                        "contact": (contact or "")[:500],
                    }
                    await _queue_login_link(
                        session,
                        client_id=owner,
                        email=address,
                        book=book,
                        summary=await _book_summary(session, context["lang"], book),
                    )
                    await notifications.publish(session)
                    return await _check_email_page(session, context)

                # §12.1 (IMPLEMENTATION.md): verification is never a
                # precondition for booking -- the request is submitted either
                # way. What it *is* a precondition for is anything that signs
                # this browser in as the client, because `resolve_client`
                # returns the existing client when the address is already
                # known: typing a stranger's address must not hand over their
                # identity (DESIGN.md §5.1).
                client = await resolve_client(
                    session, Channel.email, email, language=context["lang"]
                )

            if not _valid_modality(reservation.get("modality", "online")) or (
                reservation.get("tz") and not _valid_zone(reservation.get("tz"))
            ):
                return Response(status_code=400)
            try:
                booking_request = await booking.submit_slot_request(
                    session,
                    client_id=client.id,
                    slot_id=int(reservation["slot_id"]),
                    session_type_id=int(reservation["session_type_id"]),
                    modality=Modality(reservation.get("modality", "online")),
                    source_channel=Channel.web,
                    problem_text=problem or None,
                    contact_note=contact or None,
                    display_name=name or None,
                    client_timezone=reservation.get("tz"),
                    # A typed address proves nothing: short hold, and the
                    # therapist hears when the link is followed.
                    # (Without SMTP nothing could release it: filed normally.)
                    awaiting_verification=not proven and can_link,
                )
            except SlotUnavailable:
                await flow.clear(session, client.id, Channel.web)
                return await _error_page(
                    context,
                    context["t"]["slot_taken_title"],
                    context["t"]["slot_taken"],
                    409,
                    action="book",
                )
            except DomainError:
                return await _error_page(
                    context,
                    context["t"]["error_title"],
                    context["t"]["unavailable"],
                    409,
                    action="home",
                )

            await flow.clear(session, client.id, Channel.web)

            settings = get_settings()
            telegram_link = None
            verify_notice = None

            if proven:
                # Already signed in, so the merge link is safe to show: the deep
                # link attaches a Telegram account to this client permanently,
                # and §13.3 then routes every notification to it.
                link_raw = await issue_token(
                    session, TokenPurpose.link_channel, client_id=client.id
                )
                telegram_link = (
                    f"https://t.me/{settings.telegram_bot_username}?start=link_{link_raw}"
                )
            else:
                # The address is unproved, so the way back to this request goes
                # through it rather than around it. Nothing else here reaches an
                # unverified address (§13.3), which is why the booking otherwise
                # leaves the client with no route back at all.
                if can_link:
                    await _queue_login_link(
                        session,
                        client_id=client.id,
                        email=email,
                        request_uuid=booking_request.uuid,
                    )
                if can_link:
                    verify_notice = await get_text(session, context["lang"], "booking.check_email")

            await notifications.publish(session)

            if not proven and can_link:
                # The same page as for an address that already belongs to
                # somebody (above): nothing here says which it was.
                response = await _check_email_page(session, context)
                _clear_stash(response)
                return response

            response = _render(
                "done.html",
                {
                    **context,
                    "heading": context["t"]["consultation"],
                    "message": await get_text(
                        session,
                        context["lang"],
                        "booking.submitted",
                        uuid=_short_ref(booking_request.uuid),
                    ),
                    "request_uuid": str(booking_request.uuid),
                    "telegram_link": telegram_link,
                    "verify_notice": verify_notice,
                },
            )
            if proven:
                issue_client_session(response, client.id)
            _clear_stash(response)
            return response

    # --- Waitlist -----------------------------------------------------------

    @router.get("/waitlist", response_class=HTMLResponse, include_in_schema=False)
    async def waitlist_page(request: Request) -> Response:
        """§12.1: the waitlist beside the picker, not only instead of it.

        `/book` renders this same form when `resolve_booking_mode` sends the
        whole practice to the waitlist -- no times at all, or availability off.
        This is the other way in, for the client who can see four times and
        cannot make any of them. Same form, different first sentence: telling
        somebody there is nothing free while they are looking at a list of free
        times is worse than saying nothing.
        """
        async with unit_of_work() as session:
            context = await _context(session, request)
            return await _waitlist_form(context, await get_practice(session), by_choice=True)

    @router.post("/waitlist", include_in_schema=False)
    async def join_waitlist(
        request: Request,
        problem: str = Form(""),
        contact: str = Form(""),
        email: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
        pow_challenge: str = Form(""),
        pow_solution: str = Form(""),
    ) -> Response:
        typed = {"problem": problem, "contact": contact, "email": email}
        if not csrf_ok(request, csrf_token):
            async with unit_of_work() as session:
                context = await _context(session, request)
                return await _waitlist_form(
                    context,
                    context["practice"],
                    by_choice=True,
                    values=typed,
                    errors={"form": context["t"]["stale"]},
                    status=403,
                )

        async with unit_of_work() as session:
            context = await _context(session, request)
            # §17: the same gate as `POST /book`, for the same reason -- this
            # path also writes a row and notifies her, and a client costs one
            # address.
            if not _challenge_ok(context["practice"], pow_challenge, pow_solution):
                return await _error_page(
                    context,
                    context["t"]["error_title"],
                    context["t"]["captcha_failed"],
                    400,
                    action="home",
                )
            client = await _session_client(session, request)
            if client is None:
                if not email:
                    return RedirectResponse("/book", status_code=303)
                address = normalise_email(email)
                if address is None:
                    return await _waitlist_form(
                        context,
                        context["practice"],
                        by_choice=True,
                        values=typed,
                        errors={"email": context["t"]["email_invalid"]},
                        status=400,
                    )
                client = await resolve_client(
                    session, Channel.email, address, language=context["lang"]
                )

            try:
                await waitlist.join_waitlist(
                    session,
                    client_id=client.id,
                    problem_text=problem or None,
                    contact_note=contact or None,
                )
            except DomainError:
                # §17's limit reaches here. Without the catch it would leave the
                # ASGI handler to turn a rate limit into a 500 and an
                # `error_event` -- a refusal is not a fault.
                return await _error_page(
                    context,
                    context["t"]["error_title"],
                    context["t"]["unavailable"],
                    409,
                    action="home",
                )
            await notifications.publish(session)

            return _render(
                "done.html",
                {
                    **context,
                    "heading": context["t"]["consultation"],
                    "message": context["t"]["waitlist_submitted"],
                    "request_uuid": None,
                },
            )

    # --- Request page (§12.1) ----------------------------------------------

    @router.get("/r/{uuid}", response_class=HTMLResponse, include_in_schema=False)
    async def request_page(request: Request, uuid: str, token: str = "") -> Response:
        async with unit_of_work() as session:
            context = await _context(session, request)
            client = await _session_client(session, request)

            # An emailed link carries a view token; a browser session is the
            # other way in (§12.1: auth required).
            arrived_by_token = False
            if client is None and token:
                # Ownership before spend: the token is single-use, so a link
                # whose UUID is not the token holder's (a mangled or tampered
                # one) must leave it unspent, or the correct link fails next.
                target = await token_target(session, token, TokenPurpose.view_request)
                owner = (
                    await _client_by_id(session, target.client_id)
                    if target is not None and target.client_id
                    else None
                )
                owns = False
                if owner is not None:
                    try:
                        owns = (await booking.get_by_uuid(session, UUID(uuid))).client_id == (
                            owner.id
                        )
                    except (NotFound, ValueError):
                        owns = False
                if owner is not None and owns:
                    # A GET never spends the token: a mail scanner or a link
                    # preview opens it first. The button posts it back.
                    return _render(
                        "confirm.html",
                        {
                            **context,
                            "action": f"/r/{uuid}/enter",
                            "token": token,
                            "heading": context["t"]["your_request"],
                        },
                    )

            if client is None:
                return RedirectResponse("/auth/email", status_code=303)

            try:
                booking_request = await booking.get_by_uuid(session, UUID(uuid))
            except (NotFound, ValueError):
                return await _error_page(
                    context,
                    context["t"]["not_found_title"],
                    context["t"]["not_found"],
                    404,
                    action="home",
                )

            if booking_request.client_id != client.id:
                # Someone else's request. 404 rather than 403: whether a UUID
                # exists is itself information.
                return await _error_page(
                    context,
                    context["t"]["not_found_title"],
                    context["t"]["not_found"],
                    404,
                    action="home",
                )

            turn = await booking.whose_turn(session, booking_request.id)

            # §7.1: `scheduled_start` only exists once the therapist approved,
            # so before that the time to show is the slot being held -- and in
            # the zone the client picked, which the request remembers.
            when = await booking.requested_start(session, booking_request)
            # A client who never told us their zone still has to be shown times
            # in *some* clock, and the practice's is the one they were quoted in.
            zone = (
                booking_request.client_timezone
                or client.timezone
                or (await get_practice(session)).timezone
            )

            thread = await _thread(session, booking_request.id, context, zone)

            # A page that arrives by login token starts the session in the same
            # response, so its forms carry a CSRF token bound to the session
            # about to exist -- the cookie in this request is not it yet.
            new_session = client_session_value(client.id) if arrived_by_token else None

            response = _render(
                "request.html",
                {
                    **context,
                    "csrf_token": csrf_token_for(request, new_client_cookie=new_session),
                    "req": booking_request,
                    "ref": _short_ref(booking_request.uuid),
                    # The enum value is a database word, not a client-facing one
                    # (§15): the page used to read "pending" in every language.
                    "status_label": await get_text(
                        session,
                        context["lang"],
                        f"request.status.{booking_request.status.value}",
                    ),
                    "when": await _format(session, when, context, zone)
                    or booking_request.desired_time_text,
                    "join_url": await notifications.join_info(session, booking_request)
                    if booking_request.status.value == "confirmed"
                    else None,
                    "thread": thread,
                    "greeting": await _greeting(
                        session,
                        request,
                        context["lang"],
                        client=client,
                        arrived_by_token=arrived_by_token,
                    ),
                    "refused": request.query_params.get("refused") == "1",
                    "can_respond": turn is SenderType.client,
                    "can_note": booking_request.status in booking.NOTE_STATUSES,
                    **(
                        await _counter_options(session, booking_request, zone)
                        if turn is SenderType.client
                        else {"slot_days": [], "free_text": False, "offer_waitlist": False}
                    ),
                },
            )
            if arrived_by_token:
                # The token is spent (§6.2). A session is what makes the same
                # link work when the client opens it again an hour later, and
                # what lets the form on this page post back at all.
                issue_client_session(response, client.id, new_session)
            return response

    @router.post("/r/{uuid}/enter", include_in_schema=False)
    async def request_enter(
        request: Request,
        uuid: str,
        token: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """The button on the confirm page: here the view token is spent."""
        if not csrf_ok(request, csrf_token):
            return await _stale_page(request)
        async with unit_of_work() as session:
            # Ownership before spend, as the GET checked it: a mangled link
            # must leave the token for the correct one.
            target = await token_target(session, token, TokenPurpose.view_request)
            owner = (
                await _client_by_id(session, target.client_id)
                if target is not None and target.client_id
                else None
            )
            try:
                owns = owner is not None and (
                    (await booking.get_by_uuid(session, UUID(uuid))).client_id == owner.id
                )
            except (NotFound, ValueError):
                owns = False
            if owner is None or not owns:
                return RedirectResponse("/auth/email", status_code=303)
            try:
                await consume_token(session, token, TokenPurpose.view_request)
            except TokenInvalid:
                return RedirectResponse("/auth/email", status_code=303)
            response = RedirectResponse(f"/r/{UUID(uuid)}?{WELCOME_PARAM}=1", status_code=303)
            started = client_session_value(owner.id)
            issue_client_session(response, owner.id, started)
            issue_csrf(response, csrf_token_for(request, new_client_cookie=started))
            return response

    @router.post("/r/{uuid}/{action}", include_in_schema=False)
    async def request_action(
        request: Request,
        uuid: str,
        action: str,
        body: str = Form(""),
        slot_id: str = Form(""),
        proposed_start: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """§12.1: accept | counter | decline | note | waitlist."""
        if not csrf_ok(request, csrf_token):
            return await _stale_page(request, kept=body)
        if action not in ("accept", "counter", "decline", "note", "waitlist"):
            return Response(status_code=404)

        async with unit_of_work() as session:
            client = await _session_client(session, request)
            if client is None:
                return RedirectResponse("/auth/email", status_code=303)

            try:
                booking_request = await booking.get_by_uuid(session, UUID(uuid))
            except (NotFound, ValueError):
                return Response(status_code=404)
            if booking_request.client_id != client.id:
                return Response(status_code=404)

            try:
                if action == "accept":
                    await booking.client_accept(session, booking_request.id)
                elif action == "counter":
                    # §9: a structured time is preferred and the words are kept
                    # either way. §12.1 now offers the practice's own slots and
                    # a picker, so most counters name an instant instead of
                    # relying on a client guessing an ISO timestamp -- but the
                    # words are still parsed, because Telegram has no picker.
                    practice = await get_practice(session)
                    zone = booking_request.client_timezone or client.timezone or practice.timezone
                    when = await _countered_instant(session, slot_id, proposed_start, zone)
                    if when is _SLOT_GONE:
                        # §12.1: a proposal for a time that is no longer an
                        # opening is worse than no proposal.
                        return RedirectResponse(f"/r/{uuid}?refused=1", status_code=303)
                    await booking.client_counter(
                        session,
                        booking_request.id,
                        proposed_start=when or parse_client_time(body, zone),
                        body_text=body or None,
                    )
                elif action == "note":
                    # §7.1: information, not a transition.
                    await booking.client_note(session, booking_request.id, body_text=body)
                elif action == "waitlist":
                    # §12.1's way out where a counter may not be words. One core
                    # use-case: the transition and the entry succeed together.
                    await booking.client_decline_to_waitlist(session, booking_request.id)
                else:
                    await booking.client_decline(session, booking_request.id)
            except DomainError:
                logger.info("refused web action %r on request %s", action, uuid)
                # §12.2: a redirect with nothing on it is indistinguishable from
                # a click that did nothing, which is exactly how a refused
                # accept looked to the client.
                return RedirectResponse(f"/r/{uuid}?refused=1", status_code=303)

            await notifications.publish(session)
            return RedirectResponse(f"/r/{uuid}", status_code=303)

    # --- Magic-link auth (§12.1, DESIGN.md §5.1) ---------------------------

    @router.get("/auth/email", response_class=HTMLResponse, include_in_schema=False)
    async def auth_email_form(request: Request) -> Response:
        async with unit_of_work() as session:
            context = await _context(session, request)
            return _render("auth_email.html", {**context, "sent": False})

    def _auth_form(context: dict[str, Any], email: str, error: str, status: int) -> Response:
        return _render(
            "auth_email.html",
            {**context, "sent": False, "values": {"email": email}, "errors": {"email": error}},
            status_code=status,
        )

    @router.post("/auth/email", include_in_schema=False)
    async def auth_email(
        request: Request,
        email: str = Form(...),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        if not csrf_ok(request, csrf_token):
            async with unit_of_work() as session:
                context = await _context(session, request)
                return _auth_form(context, email, context["t"]["stale"], 403)

        settings = get_settings()
        address = normalise_email(email)

        # §17: 10 per hour per IP. The per-address limit is checked below,
        # inside the transaction, because it counts auth_token rows.
        ip_ok = ratelimit.check(ratelimit.MAGIC_LINK_IP, ratelimit.client_ip(request))

        async with unit_of_work() as session:
            context = await _context(session, request)
            if address is None:
                return _auth_form(context, email, context["t"]["email_invalid"], 400)
            email = address

            # §4: with SMTP unset there is no way to deliver a link, and the web
            # UI requires Telegram login instead.
            per_email = await magic_link_allowance_left(session, email)
            if settings.email_enabled and ip_ok and per_email > 0:
                client = await resolve_client(
                    session, Channel.email, email, language=context["lang"]
                )
                await _queue_login_link(session, client_id=client.id, email=email)

            # The same page either way: whether an address is known is not
            # something an unauthenticated caller should learn.
            return _render("auth_email.html", {**context, "sent": True})

    @router.get("/auth/callback", include_in_schema=False)
    async def auth_callback_confirm(request: Request, token: str = "") -> Response:
        """A button, not a spend: mail scanners and link previews GET every link
        in a message, and a token they used up would be dead for the person."""
        async with unit_of_work() as session:
            context = await _context(session, request)
            target = await token_target(session, token, TokenPurpose.login)
            if target is None:
                return await _error_page(
                    context,
                    context["t"]["expired_link_title"],
                    context["t"]["expired_link"],
                    400,
                    action="link",
                )
            return _render(
                "confirm.html",
                {
                    **context,
                    "action": "/auth/callback",
                    "token": token,
                    "heading": context["t"]["sign_in"],
                    **await _carried_booking_text(session, context, target.payload),
                },
            )

    @router.post("/auth/callback", include_in_schema=False)
    async def auth_callback(
        request: Request,
        token: str = Form(""),
        mode: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        if not csrf_ok(request, csrf_token):
            return await _stale_page(request)
        async with unit_of_work() as session:
            context = await _context(session, request)
            try:
                result = await consume_token(session, token, TokenPurpose.login)
            except TokenInvalid:
                return await _error_page(
                    context,
                    context["t"]["expired_link_title"],
                    context["t"]["expired_link"],
                    400,
                    action="link",
                )

            if result.client_id is None:
                return RedirectResponse("/auth/email", status_code=303)

            # Following the link proves the address (DESIGN.md §5.1).
            email = str(result.payload.get("email", ""))
            if email:
                try:
                    await link_identity(
                        session, result.client_id, Channel.email, email, verified=True
                    )
                except TokenInvalid:
                    # The address already belongs to someone else. Reachable
                    # since §13.1 step 7 lets a Telegram client name any
                    # address; merging two people is not ours to decide.
                    logger.info("login link refused: address belongs to another client")
                    return await _error_page(
                        context,
                        context["t"]["expired_link_title"],
                        context["t"]["expired_link"],
                        400,
                        action="link",
                    )

            # A proved owner's booking made on another browser: filed now, with
            # the same checks as at the form (the slot may have gone since).
            carried = result.payload.get("book")
            carried_uuid = None
            # Only the explicit choice files it: the default is to sign in.
            if isinstance(carried, dict) and mode == "book":
                try:
                    if not _valid_modality(str(carried["modality"])) or (
                        carried.get("tz") and not _valid_zone(str(carried["tz"]))
                    ):
                        raise ValueError("bad reservation")
                    filed = await booking.submit_slot_request(
                        session,
                        client_id=result.client_id,
                        slot_id=int(carried["slot_id"]),
                        session_type_id=int(carried["session_type_id"]),
                        modality=Modality(str(carried["modality"])),
                        source_channel=Channel.web,
                        contact_note=str(carried.get("contact") or "") or None,
                        display_name=str(carried.get("name") or "") or None,
                        client_timezone=str(carried.get("tz") or "") or None,
                    )
                    carried_uuid = filed.uuid
                    await notifications.publish(session)
                except (DomainError, ValueError, KeyError):
                    gone = _render(
                        "error.html",
                        {
                            **context,
                            "heading": context["t"]["slot_taken"],
                            "message": context["t"]["slot_taken"],
                        },
                        status_code=409,
                    )
                    issue_client_session(gone, result.client_id)
                    return gone

            # Where the link was sent from. A login link minted by the booking
            # flow is about one request and says so, so it lands there rather
            # than on the front page; one from `/auth/email` names no request
            # and lands on the front page as before. Parsed rather than
            # interpolated: this is our own payload, and a redirect built from
            # a string in the database should still have to be a UUID.
            target = f"/r/{carried_uuid}" if carried_uuid else "/"
            requested = str(result.payload.get("request", ""))
            if requested:
                try:
                    target = f"/r/{UUID(requested)}"
                    # The address is proved: a booking made against it stops
                    # being a short hold and the therapist is told (NP2).
                    held = await booking.get_by_uuid(session, UUID(requested))
                    if held.client_id == result.client_id:
                        await booking.release_unverified_hold(session, held.id)
                        await notifications.publish(session)
                except (ValueError, NotFound):
                    logger.info("login token carried an unreadable request reference")

            # §12.1: the arrival is what greets, so the marker rides on this
            # redirect and not in the session it also issues.
            response = RedirectResponse(f"{target}?{WELCOME_PARAM}=1", status_code=303)
            started = client_session_value(result.client_id)
            issue_client_session(response, result.client_id, started)
            issue_csrf(response, csrf_token_for(request, new_client_cookie=started))
            return response

    return router


# --- Helpers ----------------------------------------------------------------


async def _check_email_page(session: AsyncSession, context: dict[str, Any]) -> Response:
    """What an unproved booking sees, whatever became of the address."""
    return _render(
        "done.html",
        {
            **context,
            "heading": context["t"]["consultation"],
            "message": await get_text(session, context["lang"], "booking.check_email"),
            "request_uuid": None,
            "telegram_link": None,
            "verify_notice": None,
        },
    )


async def _active_session_types(session: AsyncSession) -> list[SessionType]:
    return list(
        (
            await session.execute(
                select(SessionType)
                .where(SessionType.is_active.is_(True))
                .order_by(SessionType.sort_order, SessionType.id)
            )
        )
        .scalars()
        .all()
    )


async def _client_by_id(session: AsyncSession, client_id: UUID) -> Client | None:
    return (
        await session.execute(select(Client).where(Client.id == client_id))
    ).scalar_one_or_none()


async def _session_client(session: AsyncSession, request: Request) -> Client | None:
    client_id = current_client_id(request)
    if client_id is None:
        return None
    return await _client_by_id(session, client_id)


#: The pre-sign-in reservation, in a signed cookie. `flow_state` needs a client
#: row, and a visitor choosing a slot before giving an email does not have one.
STASH_COOKIE = "pb_book"


def _stash(response: Response, reservation: dict[str, Any]) -> None:
    from app.channels.web.security import _cookie_kwargs, _encode

    response.set_cookie(STASH_COOKIE, _encode(reservation), max_age=3600, **_cookie_kwargs())


def _clear_stash(response: Response) -> None:
    response.delete_cookie(STASH_COOKIE, path="/")


async def _reservation(
    session: AsyncSession, request: Request, client: Client | None
) -> dict[str, Any] | None:
    """Whichever store holds the slot choice: flow_state for a known client,
    the signed cookie for a visitor."""
    if client is not None:
        data = await flow.data(session, client.id, Channel.web)
        if data.get("slot_id"):
            return data

    from app.channels.web.security import _decode

    raw = request.cookies.get(STASH_COOKIE)
    if raw:
        decoded = _decode(raw)
        if decoded and decoded.get("slot_id"):
            return decoded
    return None


#: Returned by `_countered_instant` when the slot the client picked has been
#: taken since the page was drawn. Distinct from None, which means "they did not
#: pick one" -- a sentinel because the two want opposite answers.
_SLOT_GONE = datetime.min.replace(tzinfo=UTC)


async def _countered_instant(
    session: AsyncSession, slot_id: str, proposed_start: str, tz: str
) -> datetime | None:
    """The instant a counter names, from a slot button or the picker.

    §12.1: the slot is re-read here rather than trusted from the form. The page
    may have been open for an hour, and recording a counter for a time the
    practice no longer offers hands the therapist something she cannot honour.
    Nothing is held either way -- §7.1 keeps the request's *original* slot, and
    one request holding two is not a state worth inventing.
    """
    if slot_id:
        try:
            wanted = int(slot_id)
        except ValueError:
            return _SLOT_GONE
        found = (
            await session.execute(
                select(Slot.starts_at).where(Slot.id == wanted, Slot.status == SlotStatus.available)
            )
        ).scalar_one_or_none()
        return found if found is not None else _SLOT_GONE

    return _parse_local_naive(proposed_start, tz)


def _parse_local_naive(value: str, tz: str) -> datetime | None:
    """`YYYY-MM-DDTHH:MM` from a `datetime-local` field, in the client's zone.

    Their own clock, not the practice's: the picker sits under times already
    rendered in it (DESIGN.md §8).
    """
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return None
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


async def _counter_options(
    session: AsyncSession, booking_request: BookingRequest, tz: str
) -> dict[str, Any]:
    """§12.1: what a client may answer a proposal with.

    The slots first, because one the practice is already holding open needs no
    negotiation at all. Then either a time of their own or the waitlist,
    depending on `fallback_to_negotiation` -- and never neither, or a client who
    simply cannot make the proposed time would have to reject their own request
    to say so.
    """
    practice = await get_practice(session)
    slots = await list_available_slots(
        session,
        window_from=now_utc(),
        window_to=now_utc() + SLOT_WINDOW,
        session_type_id=booking_request.session_type_id,
        modality=booking_request.modality,
        tz=tz,
    )

    zone = ZoneInfo(tz)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    starts: dict[str, datetime] = {}
    for slot in slots:
        local = slot.starts_at_utc.astimezone(zone)
        day = local.date().isoformat()
        starts.setdefault(day, slot.starts_at_utc)
        grouped[day].append({"id": slot.id, "label": local.strftime("%H:%M")})

    lang = (
        await session.execute(select(Client.language).where(Client.id == booking_request.client_id))
    ).scalar_one()
    slot_days = []
    for day, times in grouped.items():
        heading = await day_label(session, lang, starts[day], tz)
        for time in times:
            time["aria"] = f"{heading}, {time['label']}"
        slot_days.append((heading, times))
    return {
        "slot_days": slot_days,
        "free_text": practice.fallback_to_negotiation,
        "offer_waitlist": not practice.fallback_to_negotiation,
    }


async def _thread(
    session: AsyncSession, request_id: int, context: dict[str, Any], zone: str
) -> list[dict[str, Any]]:
    messages = (
        (
            await session.execute(
                select(NegotiationMessage)
                .where(NegotiationMessage.request_id == request_id)
                .order_by(NegotiationMessage.created_at, NegotiationMessage.id)
            )
        )
        .scalars()
        .all()
    )
    # "client" / "therapist" are database words: the reader is "You", the other side has her practice's name.
    return [
        {
            "sender": message.sender.value,
            "who": context["t"]["you"]
            if message.sender is SenderType.client
            else context["practice"].name,
            "when": await _format(session, message.proposed_start, context, zone),
            "body": message.body_text,
        }
        for message in messages
    ]


async def _format(
    session: AsyncSession, value: datetime | None, context: dict[str, Any], tz: str | None = None
) -> str | None:
    """A stored instant in the reader's zone and language (`вт 17 ноя, 18:00 (Ереван, GMT+4)`).

    `tz` is the zone the client chose when booking, kept on the request. The practice zone is only the fallback:
    showing a Moscow client a Yerevan time with no label is how someone misses a session by an hour. Storage stays UTC
    (DESIGN.md §8); the wording is `app.render.dates.when_label`, shared by every client-visible time.
    """
    if value is None:
        return None
    return await when_label(session, context["lang"], value, tz or context["practice"].timezone)
