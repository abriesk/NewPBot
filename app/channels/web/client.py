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
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.web import ratelimit
from app.channels.web.security import (
    CSRF_FIELD,
    csrf_ok,
    csrf_token_for,
    current_client_id,
    issue_client_session,
    issue_csrf,
)
from app.config import get_settings
from app.core.enums import Channel, Modality, SenderType, TokenPurpose
from app.core.errors import DomainError, NotFound, SlotUnavailable, TokenInvalid
from app.core.models import Client, NegotiationMessage, SessionType, TimezoneOption
from app.core.policies import (
    CLIENT_TEXT_MAX_CHARS,
    BookingPath,
    hold_expiry,
    now_utc,
    resolve_booking_mode,
)
from app.core.services import booking, content, flow, notifications, waitlist
from app.core.services.clients import (
    consume_token,
    issue_token,
    link_identity,
    magic_link_allowance_left,
    resolve_client,
    set_client_language,
)
from app.core.services.flow import Step
from app.core.services.notifications import Envelope, Recipient
from app.core.services.settings import get_practice
from app.core.services.slots import list_available_slots
from app.core.services.translations import get_text
from app.db import unit_of_work
from app.render.dates import day_label
from app.render.markdown import to_web_html

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory="app/channels/web/templates")

#: How far ahead the slot picker looks.
SLOT_WINDOW = timedelta(days=30)

#: Set client-side from Intl.DateTimeFormat (DESIGN.md §8).
TZ_COOKIE = "pb_tz"

#: The last language this browser asked for. The switcher is a setting, not a
#: query parameter, and none of the links on a page carry one.
LANG_COOKIE = "pb_lang"
LANG_COOKIE_MAX_AGE = 365 * 24 * 3600

LANGUAGES = ("ru", "hy", "en")


# --- Page context -----------------------------------------------------------


async def _queue_login_link(session: AsyncSession, *, client_id: UUID, email: str) -> None:
    """Queue `auth.login_link.client` to an address that has not proved itself.

    §13.3 makes this the one intent allowed to reach an unverified address,
    because following the link is what verifies it. It carries the Telegram
    deep link too: the merge has to be behind the same proof, or attaching a
    Telegram account to somebody else's client record costs nothing but knowing
    their email (DESIGN.md §5.1).
    """
    settings = get_settings()
    raw = await issue_token(
        session, TokenPurpose.login, client_id=client_id, payload={"email": email}
    )
    link_raw = await issue_token(session, TokenPurpose.link_channel, client_id=client_id)
    await notifications.enqueue(
        session,
        Envelope(
            "auth.login_link.client",
            Recipient.client,
            {
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
        "home": "common.home",
        "back": "common.back",
        "submit": "common.continue",
        "skip": "common.skip",
        "content_empty": "content.empty",
        "choose_type": "booking.choose_type",
        "choose_modality": "booking.choose_modality",
        "modality_online": "booking.modality.online",
        "modality_onsite": "booking.modality.onsite",
        "onsite_info": "booking.onsite_info",
        "onsite_no_address": "common.error.not_found",
        "choose_timezone": "booking.choose_timezone",
        "timezone_detected": "booking.timezone.detected",
        "choose_slot": "booking.choose_slot",
        "no_slots": "booking.slot.none_available",
        "slot_held": "booking.slot.held",
        "slot_taken": "booking.slot.taken",
        "ask_problem": "booking.ask_problem",
        "ask_name": "booking.ask_name",
        "ask_contact": "booking.ask_contact",
        "submitted": "booking.submitted",
        "unavailable": "booking.unavailable",
        "waitlist_intro": "waitlist.intro",
        "waitlist_problem": "waitlist.ask_problem",
        "waitlist_contact": "waitlist.ask_contact",
        "waitlist_submitted": "waitlist.submitted",
        "email_label": "auth.email_label",
        "sign_in": "auth.sign_in",
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
        "connect_telegram": "intent.auth.login_link.client.action.telegram",
        "telegram_hint": "intent.auth.login_link.client.telegram_hint",
        "error": "common.error.generic",
        "expired_link": "common.error.expired_link",
        "not_found": "common.error.not_found",
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
        # §17's free-text cap, so the form can stop somebody typing three pages
        # before the server tells them no. The refusal is still the core's.
        "text_max": CLIENT_TEXT_MAX_CHARS,
    }


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
            return _render("home.html", {**context, "intro": None})

    @router.get("/t/{topic_code}", response_class=HTMLResponse, include_in_schema=False)
    async def topic_page(request: Request, topic_code: str) -> Response:
        async with unit_of_work() as session:
            context = await _context(session, request)
            try:
                await content.get_topic(session, topic_code)
            except NotFound:
                return _render(
                    "error.html",
                    {
                        **context,
                        "heading": context["t"]["not_found"],
                        "message": context["t"]["not_found"],
                    },
                    status_code=404,
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
                return _render("waitlist.html", context)

            session_types = []
            for st in await _active_session_types(session):
                name = await get_text(session, context["lang"], f"booking.type.{st.code}")
                session_types.append(
                    {
                        "id": st.id,
                        "label": await get_text(
                            session,
                            context["lang"],
                            "booking.type.without_price",
                            name=name,
                            duration=st.duration_min,
                        ),
                    }
                )

            timezones = (
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

            # Both of these carry a {timezone} placeholder, which the generic
            # label pass has no zone to fill (§15). Re-resolved here with the
            # same IANA name the Telegram flow passes.
            labels = {
                **context["t"],
                "timezone_detected": await get_text(
                    session, context["lang"], "booking.timezone.detected", timezone=tz
                ),
                "choose_slot": await get_text(
                    session, context["lang"], "booking.choose_slot", timezone=tz
                ),
            }

            return _render(
                "book.html",
                {
                    **context,
                    "t": labels,
                    "session_types": session_types,
                    "timezones": timezones,
                    "tz": tz,
                    "negotiation": resolved.path is BookingPath.negotiation,
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
        async with unit_of_work() as session:
            practice = await get_practice(session)
            lang = _language(request, practice.default_language, None)
            zone = tz or _timezone(request, practice.timezone)

            slots = await list_available_slots(
                session,
                window_from=now_utc(),
                window_to=now_utc() + SLOT_WINDOW,
                session_type_id=session_type_id,
                modality=Modality(modality) if modality else None,
                tz=zone,
            )

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

            days = [
                (await day_label(session, lang, starts[day], zone), times)
                for day, times in grouped.items()
            ]

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
            return Response(status_code=403)

        async with unit_of_work() as session:
            practice = await get_practice(session)
            client = await _session_client(session, request)
            zone = tz or _timezone(request, practice.timezone)

            reservation = {
                "slot_id": slot_id,
                "session_type_id": session_type_id,
                "modality": modality,
                "tz": zone,
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

            # `booking.slot.held` carries {minutes}: the hold window the client
            # is racing, so it must be a number and not the placeholder (§15).
            labels = {
                **context["t"],
                "slot_held": await get_text(
                    session,
                    context["lang"],
                    "booking.slot.held",
                    minutes=context["practice"].slot_hold_minutes,
                ),
            }

            return _render(
                "details.html",
                {
                    **context,
                    "t": labels,
                    "signed_in": client is not None,
                    "display_name": client.display_name if client else None,
                },
            )

    @router.post("/book", include_in_schema=False)
    async def submit(
        request: Request,
        problem: str = Form(""),
        name: str = Form(""),
        contact: str = Form(""),
        email: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """§12.1: submit; returns the confirmation with the request UUID."""
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            context = await _context(session, request)
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
                )
            except SlotUnavailable:
                await flow.clear(session, client.id, Channel.web)
                return _render(
                    "error.html",
                    {
                        **context,
                        "heading": context["t"]["slot_taken"],
                        "message": context["t"]["slot_taken"],
                    },
                    status_code=409,
                )
            except DomainError:
                return _render(
                    "error.html",
                    {
                        **context,
                        "heading": context["t"]["error"],
                        "message": context["t"]["unavailable"],
                    },
                    status_code=409,
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
                if settings.email_enabled and await magic_link_allowance_left(session, email) > 0:
                    await _queue_login_link(session, client_id=client.id, email=email)
                verify_notice = await get_text(session, context["lang"], "booking.check_email")

            await notifications.publish(session)

            response = _render(
                "done.html",
                {
                    **context,
                    "heading": context["t"]["consultation"],
                    "message": await get_text(
                        session,
                        context["lang"],
                        "booking.submitted",
                        uuid=str(booking_request.uuid),
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

    @router.post("/waitlist", include_in_schema=False)
    async def join_waitlist(
        request: Request,
        problem: str = Form(""),
        contact: str = Form(""),
        email: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            context = await _context(session, request)
            client = await _session_client(session, request)
            if client is None:
                if not email:
                    return RedirectResponse("/book", status_code=303)
                client = await resolve_client(
                    session, Channel.email, email, language=context["lang"]
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
                return _render(
                    "error.html",
                    {
                        **context,
                        "heading": context["t"]["error"],
                        "message": context["t"]["unavailable"],
                    },
                    status_code=409,
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
                try:
                    result = await consume_token(session, token, TokenPurpose.view_request)
                except TokenInvalid:
                    result = None
                if result and result.client_id:
                    client = await _client_by_id(session, result.client_id)
                    arrived_by_token = client is not None

            if client is None:
                return RedirectResponse("/auth/email", status_code=303)

            try:
                booking_request = await booking.get_by_uuid(session, UUID(uuid))
            except (NotFound, ValueError):
                return _render(
                    "error.html",
                    {
                        **context,
                        "heading": context["t"]["not_found"],
                        "message": context["t"]["not_found"],
                    },
                    status_code=404,
                )

            if booking_request.client_id != client.id:
                # Someone else's request. 404 rather than 403: whether a UUID
                # exists is itself information.
                return _render(
                    "error.html",
                    {
                        **context,
                        "heading": context["t"]["not_found"],
                        "message": context["t"]["not_found"],
                    },
                    status_code=404,
                )

            thread = await _thread(session, booking_request.id, context["lang"])
            turn = await booking.whose_turn(session, booking_request.id)

            # §7.1: `scheduled_start` only exists once the therapist approved,
            # so before that the time to show is the slot being held -- and in
            # the zone the client picked, which the request remembers.
            when = await booking.requested_start(session, booking_request)
            zone = booking_request.client_timezone or client.timezone

            response = _render(
                "request.html",
                {
                    **context,
                    "req": booking_request,
                    # The enum value is a database word, not a client-facing one
                    # (§15): the page used to read "pending" in every language.
                    "status_label": await get_text(
                        session,
                        context["lang"],
                        f"request.status.{booking_request.status.value}",
                    ),
                    "when": _format(when, context, zone) or booking_request.desired_time_text,
                    "join_url": await notifications.join_info(session, booking_request)
                    if booking_request.status.value == "confirmed"
                    else None,
                    "thread": thread,
                    "can_respond": turn is SenderType.client,
                    "can_note": booking_request.status in booking.NOTE_STATUSES,
                },
            )
            if arrived_by_token:
                # The token is spent (§6.2). A session is what makes the same
                # link work when the client opens it again an hour later, and
                # what lets the form on this page post back at all.
                issue_client_session(response, client.id)
            return response

    @router.post("/r/{uuid}/{action}", include_in_schema=False)
    async def request_action(
        request: Request,
        uuid: str,
        action: str,
        body: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """§12.1: accept | counter | decline | note."""
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)
        if action not in ("accept", "counter", "decline", "note"):
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
                    await booking.client_counter(
                        session, booking_request.id, body_text=body or None
                    )
                elif action == "note":
                    # §7.1: information, not a transition.
                    await booking.client_note(session, booking_request.id, body_text=body)
                else:
                    await booking.client_decline(session, booking_request.id)
            except DomainError:
                logger.info("refused web action %r on request %s", action, uuid)
                return RedirectResponse(f"/r/{uuid}", status_code=303)

            await notifications.publish(session)
            return RedirectResponse(f"/r/{uuid}", status_code=303)

    # --- Magic-link auth (§12.1, DESIGN.md §5.1) ---------------------------

    @router.get("/auth/email", response_class=HTMLResponse, include_in_schema=False)
    async def auth_email_form(request: Request) -> Response:
        async with unit_of_work() as session:
            context = await _context(session, request)
            return _render("auth_email.html", {**context, "sent": False})

    @router.post("/auth/email", include_in_schema=False)
    async def auth_email(
        request: Request,
        email: str = Form(...),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        settings = get_settings()

        # §17: 10 per hour per IP. The per-address limit is checked below,
        # inside the transaction, because it counts auth_token rows.
        ip_ok = ratelimit.check(ratelimit.MAGIC_LINK_IP, ratelimit.client_ip(request))

        async with unit_of_work() as session:
            context = await _context(session, request)

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
    async def auth_callback(request: Request, token: str = "") -> Response:
        async with unit_of_work() as session:
            context = await _context(session, request)
            try:
                result = await consume_token(session, token, TokenPurpose.login)
            except TokenInvalid:
                return _render(
                    "error.html",
                    {
                        **context,
                        "heading": context["t"]["expired_link"],
                        "message": context["t"]["expired_link"],
                    },
                    status_code=400,
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
                    return _render(
                        "error.html",
                        {
                            **context,
                            "heading": context["t"]["expired_link"],
                            "message": context["t"]["expired_link"],
                        },
                        status_code=400,
                    )

            response = RedirectResponse("/", status_code=303)
            issue_client_session(response, result.client_id)
            issue_csrf(response, csrf_token_for(request))
            return response

    return router


# --- Helpers ----------------------------------------------------------------


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


async def _thread(session: AsyncSession, request_id: int, lang: str) -> list[dict[str, Any]]:
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
    return [
        {
            "sender": message.sender.value,
            "who": message.sender.value,
            "when": message.proposed_start.strftime("%Y-%m-%d %H:%M")
            if message.proposed_start
            else None,
            "body": message.body_text,
        }
        for message in messages
    ]


def _format(value: datetime | None, context: dict[str, Any], tz: str | None = None) -> str | None:
    """A stored instant in the reader's zone. Storage stays UTC (DESIGN.md §8).

    `tz` is the zone the client chose when booking, kept on the request. The
    practice zone is only the fallback: showing a Moscow client a Yerevan time
    with no label is how someone misses a session by an hour.
    """
    if value is None:
        return None
    zone = ZoneInfo(tz or context["practice"].timezone)
    return f"{value.astimezone(zone).strftime('%Y-%m-%d %H:%M')} ({zone.key})"
