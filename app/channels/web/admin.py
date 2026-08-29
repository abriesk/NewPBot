"""Admin web UI (IMPLEMENTATION.md §12.2).

Everything under `/admin`, session-authenticated and CSRF-protected (§17). The
primary admin surface: Telegram keeps a reduced one for what matters away from
a desk, and replies with a link to here for anything else (§13.2).

English only, by design -- DESIGN.md §11 puts the admin UI and every operational
error in English. Where the catalogue has an `admin.*` key it is used, so the
therapist can reword it; the rest is plain English in the template, because
inventing a translation key for a surface that is never translated buys nothing.

A thin adapter, like every other channel: the scheduling decisions are calls
into app/core/services/.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.web import ratelimit
from app.channels.web.backups import list_dumps, resolve_dump
from app.channels.web.help import help_guide
from app.channels.web.security import (
    CSRF_FIELD,
    authenticate_admin,
    csrf_ok,
    csrf_token_for,
    current_admin,
    end_admin_session,
    issue_csrf,
    start_admin_session,
)
from app.channels.web.status import read_status
from app.channels.web.translation_groups import Entry, arrange
from app.core.enums import (
    ActorType,
    BookingMode,
    Channel,
    Modality,
    OutboxStatus,
    RequestStatus,
)
from app.core.errors import DomainError, NotFound, SlotReferenced
from app.core.models import (
    AdminUser,
    AuditLog,
    BookingRequest,
    Client,
    ContentBlock,
    ContentTopic,
    Identity,
    NegotiationMessage,
    OutboxMessage,
    SessionType,
    Slot,
    TimezoneOption,
    Translation,
    WaitlistEntry,
)
from app.core.policies import now_utc
from app.core.services import booking, content, notifications, waitlist
from app.core.services import slots as slot_service
from app.core.services.clients import (
    erase_client,
    export_client,
    identities_for,
    identities_for_many,
    list_clients_for_admin,
)
from app.core.services.config_io import (
    ConfigInvalid,
    ImportReport,
    dump_config,
    export_config,
    import_config,
    load_config,
)
from app.core.services.content import MarkdownNotAllowed
from app.core.services.settings import MUTABLE_FIELDS, get_practice, update_settings
from app.core.services.slots import SlotPattern
from app.core.services.translations import get_text, invalidate_cache, missing_keys, set_text
from app.db import unit_of_work
from app.render.markdown import to_email_text, to_telegram, to_web_html

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory="app/channels/web/templates")

#: The admin surface is English. DESIGN.md §11.
LOCALE = "en"

LANGUAGES = ("ru", "hy", "en")

#: §17. The one upload this service accepts, capped before it is parsed.
MAX_CONFIG_UPLOAD = 5 * 1024 * 1024

#: Why an action on the request page is not available, keyed by its §7.1 event.
#:
#: §13.2 has always required the Telegram panel to derive its buttons from the
#: transition table, so it cannot offer what the core would refuse. This page
#: rendered all four forms whatever the status, so Cancel on a `negotiating`
#: request was a control whose only possible outcome was a refusal -- and the
#: refusal said "refused", which explains nothing to somebody who has just been
#: told that the obvious way to call a session off is not available.
#:
#: The control stays on the page, greyed, rather than disappearing: the page
#: keeps one shape across every status, and a therapist looking for cancel finds
#: it and finds out why, instead of concluding it does not exist. Plain English
#: in the channel because the admin surface is never translated (DESIGN.md §11).
UNAVAILABLE_BECAUSE = {
    "admin_approve": "Only a request that is still open can be approved.",
    "admin_propose": "A time can be proposed only while the request is open.",
    "admin_reject": "Only a request that is still open can be rejected.",
    "admin_cancel": (
        "Only a confirmed session can be cancelled. A request that was never "
        "confirmed is rejected instead."
    ),
    "admin_reschedule": (
        "Only a confirmed session can be moved. Before that, propose a time "
        "instead — there is nothing booked to move yet."
    ),
}

#: §12.2: moving a session onto an hour she has already promised. Said rather
#: than refused -- she may be stacking two deliberately, and this is the clash
#: she would otherwise discover on the day.
ALREADY_BOOKED = (
    "Moved. Note that you already had a confirmed session at that time — "
    "check the week schedule."
)

NO_NEW_TIME = "Moving a session needs a new time. Nothing was changed."

#: Why a slot the page still lists cannot be deleted, for the same reason and in
#: the same voice as the entries above. The delete button is withheld from a
#: referenced slot in the first place, so this is what a therapist sees when the
#: reference arrived between the page rendering and her clicking it.
SLOT_REFERENCED = (
    "A request asked for that time, so the slot has to stay. Block it instead "
    "and it stops being offered."
)

BAD_PRICE = (
    "A price is a whole number, like 15000 for 15,000 dram. No decimal point, "
    "and no currency sign. Leave it empty for no price."
)

#: Written as a code point rather than as itself: it is a space on screen, and a
#: literal one here is invisible to the next person reading this line.
NBSP = chr(0xA0)


def _price_amount(value: str) -> int | None:
    """The price as the therapist writes it, which is what the column holds.

    A price here is a **whole unit of the currency named beside it** -- 15000 is
    15,000 dram -- and it is stored exactly as typed. The column is called
    `price_amount_minor` and its comment says 5000 means 50.00, which is the
    ordinary convention and is *not* what this practice means by it: the
    therapist prices in whole dram, and asking her to write 1500000 for a
    15,000 dram session is asking her to do currency arithmetic in a text box.
    Whoever eventually renders a price to a client has to read this rather than
    the column name (DESIGN.md §20.3).

    Passing the field to `int()` is what this replaces: a decimal point in the
    box raised `ValueError` out of the route and reached the therapist as a 500.
    A decimal is now refused with a sentence instead, rather than rounded --
    quietly changing somebody's price is worse than asking again. Spaces go
    because a thousands separator is a normal thing to type, the non-breaking
    one included: that is what a figure pasted from a spreadsheet carries, and
    it is invisible in the field.
    """
    text = value.strip().replace(" ", "").replace(NBSP, "")
    if not text:
        return None
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"{value!r} is not a whole-number price")
    return int(text)


def _price_display(amount: int | None) -> str:
    """The inverse, for the form field. An empty field means no price."""
    return "" if amount is None else str(amount)


async def _stored_text(session: AsyncSession, lang: str, key: str) -> str:
    """The row behind a key, or "" when there is none.

    Deliberately not `get_text`: that falls back through the default language
    and then English (§15), which is right when rendering and wrong when
    prefilling a form. A field showing the Russian name on the Armenian tab
    invites her to press Save, and Save would write the fallback in as though
    it were a translation.
    """
    value = (
        await session.execute(
            select(Translation.value).where(
                Translation.lang == lang, Translation.key == key
            )
        )
    ).scalar_one_or_none()
    return str(value) if value else ""


async def _labels(session: AsyncSession) -> dict[str, str]:
    """Only the labels the catalogue actually covers."""
    keys = {
        "login_title": "admin.login.title",
        "login_failed": "admin.login.failed",
        "nav_requests": "admin.nav.requests",
        "nav_waitlist": "admin.nav.waitlist",
        "nav_slots": "admin.nav.slots",
        "nav_content": "admin.nav.content",
        "nav_translations": "admin.nav.translations",
        "nav_settings": "admin.nav.settings",
        "nav_session_types": "admin.nav.session_types",
        "nav_timezones": "admin.nav.timezones",
        "nav_delivery": "admin.nav.delivery",
        "nav_clients": "admin.nav.clients",
        "nav_privacy": "admin.nav.privacy",
        "nav_maintenance": "admin.nav.maintenance",
        "nav_help": "admin.nav.help",
        "nav_status": "admin.nav.status",
        "approve": "admin.request.approve",
        "propose": "admin.request.propose",
        "reject": "admin.request.reject",
        "cancel": "admin.request.cancel",
        "meeting_url": "admin.request.meeting_url",
        "slots_bulk": "admin.slots.bulk",
        "content_preview": "admin.content.preview",
        "content_revisions": "admin.content.revisions",
        "invalid_markdown": "admin.content.invalid_markdown",
        "settings_saved": "admin.settings.saved",
        "config_export": "admin.maintenance.export",
        "config_import": "admin.maintenance.import",
        "config_preview": "admin.maintenance.preview",
        "backups": "admin.maintenance.backups",
    }
    return {name: await get_text(session, LOCALE, key) for name, key in keys.items()}


async def _context(
    session: AsyncSession, request: Request, admin: AdminUser, **extra: Any
) -> dict[str, Any]:
    return {
        "request": request,
        "admin": admin,
        "practice": await get_practice(session),
        "t": await _labels(session),
        "csrf_token": csrf_token_for(request),
        "flash": request.query_params.get("flash"),
        # The dot lives in base.html, so every admin page carries it (§12.2).
        # One small file read, no queries.
        "health": read_status(),
        **extra,
    }


def _render(name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    response = TEMPLATES.TemplateResponse(
        context["request"], name, context, status_code=status_code
    )
    issue_csrf(response, context.get("csrf_token"))
    return response


def _back(path: str, flash: str = "") -> RedirectResponse:
    target = f"{path}?flash={flash}" if flash else path
    return RedirectResponse(target, status_code=303)


def build_router() -> APIRouter:
    router = APIRouter(prefix="/admin")

    # --- Session ------------------------------------------------------------

    @router.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_form(request: Request) -> Response:
        async with unit_of_work() as session:
            return _render(
                "admin/login.html",
                {
                    "request": request,
                    "t": await _labels(session),
                    "csrf_token": csrf_token_for(request),
                    "failed": False,
                },
            )

    @router.post("/login", include_in_schema=False)
    async def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        # §17: 5 per 15 minutes per IP. Counted before the password is checked,
        # so a wrong guess costs the attempt whether or not it was close.
        if not ratelimit.check(ratelimit.ADMIN_LOGIN, ratelimit.client_ip(request)):
            logger.warning("admin login rate limit reached")
            return Response(status_code=429)

        async with unit_of_work() as session:
            admin = await authenticate_admin(session, username, password)
            if admin is None:
                # Deliberately vague: which half was wrong is not the caller's
                # business.
                logger.warning("failed admin login for %r", username)
                return _render(
                    "admin/login.html",
                    {
                        "request": request,
                        "t": await _labels(session),
                        "csrf_token": csrf_token_for(request),
                        "failed": True,
                    },
                    status_code=401,
                )

            response = _back("/admin/requests")
            await start_admin_session(session, response, admin)
            issue_csrf(response, csrf_token_for(request))
            return response

    @router.post("/logout", include_in_schema=False)
    async def logout(request: Request, csrf_token: str = Form("", alias=CSRF_FIELD)) -> Response:
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)
        async with unit_of_work() as session:
            response = _back("/admin/login")
            await end_admin_session(session, request, response)
            return response

    # --- Requests (§12.2) ---------------------------------------------------

    @router.get("", response_class=HTMLResponse, include_in_schema=False)
    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request) -> Response:
        return _back("/admin/requests")

    @router.get("/requests", response_class=HTMLResponse, include_in_schema=False)
    async def requests_list(
        request: Request, status: str = "", view: str = "", start: str = ""
    ) -> Response:
        """§12.2: two views of one query, chosen by `?view=`.

        Anything other than `grid` is the list, including nonsense: the
        parameter is navigation, not input, so a bad one lands somewhere useful
        rather than on an error page.
        """
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            if view == "grid":
                return _render(
                    "admin/requests.html",
                    await _context(
                        session, request, admin, view="grid", **await _schedule(session, start)
                    ),
                )

            stmt = select(BookingRequest).order_by(BookingRequest.created_at.desc())
            if status:
                try:
                    stmt = stmt.where(BookingRequest.status == RequestStatus(status))
                except ValueError:
                    pass

            rows = (await session.execute(stmt.limit(200))).scalars().all()
            return _render(
                "admin/requests.html",
                await _context(
                    session,
                    request,
                    admin,
                    view="list",
                    rows=await _summaries(session, rows),
                    statuses=[s.value for s in RequestStatus],
                    active=status,
                ),
            )

    @router.get("/requests/{uuid}", response_class=HTMLResponse, include_in_schema=False)
    async def request_detail(request: Request, uuid: str) -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            try:
                booking_request = await booking.get_by_uuid(session, UUID(uuid))
            except (NotFound, ValueError):
                return Response(status_code=404)

            thread = (
                (
                    await session.execute(
                        select(NegotiationMessage)
                        .where(NegotiationMessage.request_id == booking_request.id)
                        .order_by(NegotiationMessage.created_at, NegotiationMessage.id)
                    )
                )
                .scalars()
                .all()
            )
            client = (
                await session.execute(select(Client).where(Client.id == booking_request.client_id))
            ).scalar_one()

            return _render(
                "admin/request_detail.html",
                await _context(
                    session,
                    request,
                    admin,
                    req=booking_request,
                    summary=await _summary(session, booking_request),
                    client=client,
                    # §12.2: how to reach this person. Often the only thing
                    # identifying a web request, which carries no name unless
                    # the client typed one.
                    identities=_identity_contacts(await identities_for(session, client.id)),
                    # §7.1 decides what may be offered, so no admin surface
                    # offers what the core would refuse (§13.2 for the panel).
                    unavailable={
                        event: why
                        for event, why in UNAVAILABLE_BECAUSE.items()
                        if event not in booking.ALLOWED[booking_request.status]
                    },
                    thread=thread,
                    turn=await booking.whose_turn(session, booking_request.id),
                ),
            )

    @router.post("/requests/{uuid}/{action}", include_in_schema=False)
    async def request_action(
        request: Request,
        uuid: str,
        action: str,
        scheduled_start: str = Form(""),
        meeting_url: str = Form(""),
        body: str = Form(""),
        reason: str = Form(""),
        keep_slot: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """§12.2: approve, propose, reject, cancel, reschedule."""
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            try:
                booking_request = await booking.get_by_uuid(session, UUID(uuid))
            except (NotFound, ValueError):
                return Response(status_code=404)

            practice = await get_practice(session)
            when = _parse_local(scheduled_start, practice.timezone)
            if scheduled_start.strip() and when is None:
                # §12.2: a time she typed and this form cannot read is refused,
                # not dropped. Read as "no time given" it turned a mistyped
                # proposal into a timeless one -- she had said when, and the
                # client was told nothing. An empty field still means words
                # only, which §7.1 allows.
                return _back(f"/admin/requests/{uuid}", "bad-time")

            try:
                if action == "approve":
                    await booking.admin_approve(
                        session,
                        booking_request.id,
                        scheduled_start=when,
                        meeting_url=meeting_url or None,
                    )
                elif action == "propose":
                    await booking.admin_propose(
                        session, booking_request.id, proposed_start=when, body_text=body or None
                    )
                elif action == "reject":
                    await booking.admin_reject(session, booking_request.id, reason=reason or None)
                elif action == "cancel":
                    await booking.admin_cancel(
                        session,
                        booking_request.id,
                        reason=reason or None,
                        keep_slot=bool(keep_slot),
                    )
                elif action == "reschedule":
                    if when is None:
                        # The one action with nothing to fall back on: approve
                        # can take the held slot's time and propose may be words,
                        # but a move with no time to move to is not a move.
                        return _back(f"/admin/requests/{uuid}", NO_NEW_TIME)
                    clash = await booking.clashes_at(
                        session, when, ignoring=booking_request.id
                    )
                    await booking.admin_reschedule(
                        session,
                        booking_request.id,
                        new_start=when,
                        note=reason.strip() or None,
                    )
                    if clash is not None:
                        # Said, not refused (§12.2): she may be stacking two on
                        # purpose, and this is the hour she would otherwise find
                        # out about on the day.
                        await notifications.publish(session)
                        return _back(f"/admin/requests/{uuid}", ALREADY_BOOKED)
                else:
                    return Response(status_code=404)
            except DomainError as exc:
                logger.info("admin action %r refused: %s", action, type(exc).__name__)
                return _back(f"/admin/requests/{uuid}", "refused")

            await notifications.publish(session)
            return _back(f"/admin/requests/{uuid}", "done")

    # --- Waitlist -----------------------------------------------------------

    @router.get("/waitlist", response_class=HTMLResponse, include_in_schema=False)
    async def waitlist_list(request: Request) -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            rows = (
                (
                    await session.execute(
                        select(WaitlistEntry).order_by(WaitlistEntry.created_at.desc()).limit(200)
                    )
                )
                .scalars()
                .all()
            )
            return _render(
                "admin/waitlist.html",
                await _context(
                    session,
                    request,
                    admin,
                    rows=rows,
                    actions=("contacted", "converted", "closed"),
                ),
            )

    @router.post("/waitlist/{entry_id}/{action}", include_in_schema=False)
    async def waitlist_action(
        request: Request,
        entry_id: int,
        action: str,
        note: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            try:
                if action == "contacted":
                    await waitlist.mark_contacted(session, entry_id, admin_note=note or None)
                elif action == "converted":
                    await waitlist.mark_converted(session, entry_id)
                elif action == "closed":
                    await waitlist.close_entry(session, entry_id, admin_note=note or None)
                else:
                    return Response(status_code=404)
            except DomainError:
                return _back("/admin/waitlist", "refused")
            return _back("/admin/waitlist", "done")

    # --- Slots --------------------------------------------------------------

    @router.get("/slots", response_class=HTMLResponse, include_in_schema=False)
    async def slots_page(request: Request) -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            practice = await get_practice(session)
            zone = ZoneInfo(practice.timezone)
            rows = (
                (
                    await session.execute(
                        select(Slot)
                        .where(Slot.starts_at >= now_utc() - timedelta(days=1))
                        .order_by(Slot.starts_at)
                        .limit(500)
                    )
                )
                .scalars()
                .all()
            )
            # Which of these a request still points at. An `available` slot can
            # carry such a reference -- every terminal transition releases the
            # slot and leaves the request remembering the time it asked for
            # (§7.1) -- and deleting one is refused, so the button is not
            # offered for it. One query for the whole page rather than one per
            # row.
            referenced = set(
                (
                    await session.execute(
                        select(BookingRequest.slot_id).where(
                            BookingRequest.slot_id.in_([s.id for s in rows])
                        )
                    )
                )
                .scalars()
                .all()
            )

            return _render(
                "admin/slots.html",
                await _context(
                    session,
                    request,
                    admin,
                    rows=[
                        {
                            "id": s.id,
                            "when": s.starts_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
                            "status": s.status.value,
                            "modality": s.modality.value if s.modality else "either",
                            "duration": s.duration_min,
                            "deletable": s.id not in referenced,
                        }
                        for s in rows
                    ],
                    modalities=[m.value for m in Modality],
                    weekdays=list(enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))),
                ),
            )

    @router.post("/slots/bulk", include_in_schema=False)
    async def slots_bulk(
        request: Request,
        date_from: str = Form(...),
        date_to: str = Form(...),
        times: str = Form(...),
        weekdays: list[str] = Form(default=[]),  # noqa: B008 - FastAPI declaration
        duration_min: int = Form(60),
        modality: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """§12.2: create slots in bulk -- a weekly pattern over a date range."""
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            try:
                pattern = SlotPattern(
                    weekdays=frozenset(int(d) for d in weekdays),
                    times=tuple(
                        time.fromisoformat(part.strip())
                        for part in times.split(",")
                        if part.strip()
                    ),
                    date_from=date.fromisoformat(date_from),
                    date_to=date.fromisoformat(date_to),
                    duration_min=duration_min,
                    modality=Modality(modality) if modality else None,
                )
            except ValueError:
                return _back("/admin/slots", "bad-pattern")

            created = await slot_service.create_slots_bulk(session, pattern)
            return _back("/admin/slots", f"created-{len(created)}")

    @router.post("/slots/{slot_id}/{action}", include_in_schema=False)
    async def slot_action(
        request: Request,
        slot_id: int,
        action: str,
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            try:
                if action == "block":
                    await slot_service.block_slot(session, slot_id)
                elif action == "unblock":
                    await slot_service.unblock_slot(session, slot_id)
                elif action == "delete":
                    await slot_service.delete_slot(session, slot_id)
                else:
                    return Response(status_code=404)
            except SlotReferenced:
                return _back("/admin/slots", SLOT_REFERENCED)
            except DomainError:
                # A held or booked slot is blocked, not deleted (DESIGN.md §8).
                return _back("/admin/slots", "refused")
            return _back("/admin/slots", "done")

    # --- Content ------------------------------------------------------------

    @router.get("/content", response_class=HTMLResponse, include_in_schema=False)
    async def content_page(request: Request, topic: str = "", lang: str = "ru") -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            topics = (
                (await session.execute(select(ContentTopic).order_by(ContentTopic.sort_order)))
                .scalars()
                .all()
            )
            selected = topic or (topics[0].code if topics else "")
            blocks: list[ContentBlock] = []
            if selected:
                blocks = await content.get_topic_blocks(
                    session, selected, lang, published_only=False
                )

            return _render(
                "admin/content.html",
                await _context(
                    session,
                    request,
                    admin,
                    topics=topics,
                    selected=selected,
                    lang=lang if lang in LANGUAGES else "ru",
                    languages=LANGUAGES,
                    blocks=blocks,
                ),
            )

    @router.post("/content/blocks", include_in_schema=False)
    async def content_save(
        request: Request,
        topic_code: str = Form(...),
        lang: str = Form(...),
        position: int = Form(...),
        body_md: str = Form(""),
        is_published: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            topic = await content.get_topic(session, topic_code)
            try:
                await content.upsert_block(
                    session,
                    topic_id=topic.id,
                    lang=lang,
                    position=position,
                    body_md=body_md,
                    is_published=bool(is_published),
                )
            except MarkdownNotAllowed as exc:
                # §11.1: caught at save time, in front of her, with a localised
                # explanation -- not at send time in front of a client.
                message = await get_text(session, LOCALE, exc.translation_key, detail=exc.detail)
                return _back(f"/admin/content?topic={topic_code}&lang={lang}", message)

            return _back(f"/admin/content?topic={topic_code}&lang={lang}", "saved")

    @router.post("/content/preview", response_class=HTMLResponse, include_in_schema=False)
    async def content_preview(
        request: Request,
        body_md: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """§12.2: render a block exactly as each channel would, side by side.

        The point is that the therapist sees Telegram's split and tag subset
        before a client does.
        """
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            try:
                content.validate_markdown(body_md)
                error = ""
                telegram_parts = to_telegram(body_md)
                web_html = to_web_html(body_md)
                email_text = to_email_text(body_md)
            except MarkdownNotAllowed as exc:
                error = await get_text(session, LOCALE, exc.translation_key, detail=exc.detail)
                telegram_parts, web_html, email_text = [], "", ""

            return _render(
                "admin/partials/preview.html",
                {
                    "request": request,
                    "csrf_token": csrf_token_for(request),
                    "error": error,
                    "telegram_parts": telegram_parts,
                    "web_html": web_html,
                    "email_text": email_text,
                },
            )

    @router.get(
        "/content/revisions/{block_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def content_revisions(request: Request, block_id: int) -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            block = (
                await session.execute(select(ContentBlock).where(ContentBlock.id == block_id))
            ).scalar_one_or_none()
            if block is None:
                return Response(status_code=404)

            return _render(
                "admin/revisions.html",
                await _context(
                    session,
                    request,
                    admin,
                    block=block,
                    revisions=await content.list_revisions(session, block_id),
                ),
            )

    @router.post("/content/revisions/{block_id}/restore", include_in_schema=False)
    async def content_restore(
        request: Request,
        block_id: int,
        version: int = Form(...),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """A paste that breaks a page at 23:00 has to be undoable (§10.2)."""
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            try:
                await content.restore_revision(session, block_id, version)
            except (NotFound, MarkdownNotAllowed):
                return _back(f"/admin/content/revisions/{block_id}", "refused")
            return _back(f"/admin/content/revisions/{block_id}", "restored")

    # --- Translations -------------------------------------------------------

    @router.get("/translations", response_class=HTMLResponse, include_in_schema=False)
    async def translations_page(request: Request, lang: str = "ru") -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            chosen = lang if lang in LANGUAGES else "ru"

            async def wording(of: str) -> dict[str, str]:
                rows = await session.execute(
                    select(Translation.key, Translation.value).where(Translation.lang == of)
                )
                return {str(key): str(value) for key, value in rows.all()}

            stored = await wording(chosen)
            # §15: a key with no row falls back rather than failing, so it has
            # no row to edit either. Listed anyway, blank and marked, because
            # "the ones you have not written yet" is exactly what she is here
            # for -- the old page could only name them.
            missing = await missing_keys(session, chosen)

            # The English wording, to show beside the field she is filling in.
            english = await wording("en") if chosen != "en" else {}

            entries = [
                Entry(
                    key=key,
                    value=stored.get(key, ""),
                    english=english.get(key, ""),
                    missing=key in set(missing),
                )
                for key in sorted(set(stored) | set(missing))
            ]

            return _render(
                "admin/translations.html",
                await _context(
                    session,
                    request,
                    admin,
                    groups=arrange(entries),
                    lang=chosen,
                    languages=LANGUAGES,
                    missing=missing,
                ),
            )

    @router.post("/translations", include_in_schema=False)
    async def translations_save(
        request: Request,
        lang: str = Form(...),
        key: str = Form(...),
        value: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            await set_text(session, lang, key, value)
            invalidate_cache()
            return _back(f"/admin/translations?lang={lang}", "saved")

    # --- Settings -----------------------------------------------------------

    @router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    async def settings_page(request: Request) -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")
            return _render(
                "admin/settings.html",
                await _context(session, request, admin, fields=sorted(MUTABLE_FIELDS)),
            )

    @router.post("/settings", include_in_schema=False)
    async def settings_save(request: Request) -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            form = await request.form()
            if not csrf_ok(request, str(form.get(CSRF_FIELD, ""))):
                return Response(status_code=403)

            try:
                changes = _settings_changes(form)
                await update_settings(session, **changes)
            except (ValueError, TypeError) as exc:
                # Surfaced, not swallowed: a settings page that redirects
                # cheerfully while changing nothing is the worst kind of bug.
                logger.info("settings rejected: %s", exc)
                return _back("/admin/settings", f"rejected: {exc}")
            labels = await _labels(session)
            return _back("/admin/settings", labels["settings_saved"])

    # --- Session types and timezones ---------------------------------------

    @router.get("/session-types", response_class=HTMLResponse, include_in_schema=False)
    async def session_types_page(request: Request) -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")
            rows = (
                (await session.execute(select(SessionType).order_by(SessionType.sort_order)))
                .scalars()
                .all()
            )
            # What each type is called in each client-facing language. A type is
            # a row, so its name cannot live in the locale files (§6.4) -- these
            # are `translation` rows under `booking.type.<code>`, and without
            # them a client is offered the key (DESIGN.md §20.3).
            names = {
                row.id: {
                    lang: await _stored_text(session, lang, f"booking.type.{row.code}")
                    for lang in LANGUAGES
                }
                for row in rows
            }

            return _render(
                "admin/session_types.html",
                await _context(
                    session,
                    request,
                    admin,
                    rows=rows,
                    languages=LANGUAGES,
                    names=names,
                    # Rendered here rather than in the template, so that the one
                    # place deciding what a price means is `_price_amount` and
                    # its inverse rather than a Jinja expression beside them.
                    prices={row.id: _price_display(row.price_amount_minor) for row in rows},
                ),
            )

    @router.post("/session-types", include_in_schema=False)
    async def session_types_save(
        request: Request,
        code: str = Form(...),
        duration_min: int = Form(60),
        price_amount_minor: str = Form(""),
        price_currency: str = Form(""),
        is_active: str = Form(""),
        name_ru: str = Form(""),
        name_hy: str = Form(""),
        name_en: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """Adding "supervision" is an insert, not a migration (§6.4)."""
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            practice = await get_practice(session)
            row = (
                await session.execute(select(SessionType).where(SessionType.code == code))
            ).scalar_one_or_none()
            if row is None:
                row = SessionType(practice_id=practice.id, code=code)
                session.add(row)

            try:
                amount = _price_amount(price_amount_minor)
            except ValueError:
                # Surfaced rather than swallowed, and rather than raised: this
                # was `int()` on whatever she typed, and a decimal point in a
                # price field is a reasonable thing to type.
                return _back("/admin/session-types", BAD_PRICE)

            row.duration_min = duration_min
            row.price_amount_minor = amount
            # ISO 4217 is upper case, and "amd" beside "AMD" is two currencies
            # as far as anything comparing them is concerned.
            row.price_currency = price_currency.strip().upper() or None
            row.is_active = bool(is_active)
            await session.flush()

            # The name a client is offered. Written through `set_text` like any
            # other translation, so it appears on /admin/translations under
            # `booking.` too and she has two ways to reach it. A blank field
            # writes nothing rather than an empty row: §15's fallback chain is a
            # better answer than a button with no label, and `session_type_name`
            # ends it at the code.
            for lang, value in (("ru", name_ru), ("hy", name_hy), ("en", name_en)):
                if value.strip():
                    await set_text(session, lang, f"booking.type.{code}", value.strip())

            return _back("/admin/session-types", "saved")

    @router.get("/timezones", response_class=HTMLResponse, include_in_schema=False)
    async def timezones_page(request: Request) -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")
            rows = (
                (await session.execute(select(TimezoneOption).order_by(TimezoneOption.sort_order)))
                .scalars()
                .all()
            )
            return _render(
                "admin/timezones.html",
                await _context(session, request, admin, rows=rows),
            )

    @router.post("/timezones", include_in_schema=False)
    async def timezones_save(
        request: Request,
        iana_name: str = Form(...),
        display_name: str = Form(...),
        is_active: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            try:
                ZoneInfo(iana_name)
            except Exception:
                # IANA names only; an offset string breaks at every DST change.
                return _back("/admin/timezones", "not-an-iana-name")

            practice = await get_practice(session)
            row = (
                await session.execute(
                    select(TimezoneOption).where(TimezoneOption.iana_name == iana_name)
                )
            ).scalar_one_or_none()
            if row is None:
                row = TimezoneOption(
                    practice_id=practice.id, iana_name=iana_name, display_name=display_name
                )
                session.add(row)
            row.display_name = display_name
            row.is_active = bool(is_active)
            await session.flush()
            return _back("/admin/timezones", "saved")

    # --- Delivery -----------------------------------------------------------

    @router.get("/delivery", response_class=HTMLResponse, include_in_schema=False)
    async def delivery_page(request: Request) -> Response:
        """§12.2: recent outbox messages and failures, so "did she get my
        message?" is answerable (DESIGN.md §15)."""
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            rows = (
                (
                    await session.execute(
                        select(OutboxMessage).order_by(OutboxMessage.created_at.desc()).limit(100)
                    )
                )
                .scalars()
                .all()
            )
            # .tuples() so this is a sequence of real tuples rather than Rows,
            # which dict() accepts and the type checker understands.
            tallied: dict[OutboxStatus, int] = dict(
                (
                    await session.execute(
                        select(OutboxMessage.status, func.count()).group_by(OutboxMessage.status)
                    )
                )
                .tuples()
                .all()
            )
            return _render(
                "admin/delivery.html",
                await _context(
                    session,
                    request,
                    admin,
                    rows=rows,
                    counts={s.value: tallied.get(s, 0) for s in OutboxStatus},
                ),
            )

    # --- Client export and erasure (§12.2, DESIGN.md §16) -------------------

    @router.get("/clients/{client_id}/export", include_in_schema=False)
    async def export_client_route(request: Request, client_id: str) -> Response:
        """Everything held about one person, as a JSON download.

        DESIGN.md §16: answerable without a database console. This is the one
        direction problem text may travel -- back to the person it is about.
        """
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            try:
                data = await export_client(session, UUID(client_id))
            except (NotFound, ValueError):
                return Response(status_code=404)

            practice = await get_practice(session)
            session.add(
                AuditLog(
                    practice_id=practice.id,
                    actor_type=ActorType.admin,
                    action="client.export",
                    entity_type="client",
                    entity_id=client_id,
                )
            )

            return JSONResponse(
                data,
                headers={
                    "Content-Disposition": (f'attachment; filename="client-{client_id}.json"')
                },
            )

    @router.post("/clients/{client_id}/erase", include_in_schema=False)
    async def erase_client_route(
        request: Request,
        client_id: str,
        confirm: str = Form(""),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """Honour a request to be forgotten.

        Irreversible, so it wants the word typed rather than a single click.
        """
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            if confirm.strip().lower() != "erase":
                return _back("/admin/privacy", "type erase to confirm")

            try:
                await erase_client(session, UUID(client_id))
            except (NotFound, ValueError):
                return Response(status_code=404)

            return _back("/admin/privacy", "erased")

    @router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
    async def privacy_page(request: Request) -> Response:
        """§16's surface, under its own name.

        Every `client` row, deliberately: erased people, and people who asked
        for a magic link and never booked. A data-subject request is answerable
        only against the whole population, so this is the one list that must
        not be filtered -- which is exactly what made it misleading while it
        was also the page called Clients.
        """
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            rows = (
                (
                    await session.execute(
                        select(Client).order_by(Client.created_at.desc()).limit(200)
                    )
                )
                .scalars()
                .all()
            )
            found = await identities_for_many(session, [row.id for row in rows])
            identities = {
                str(row.id): _identity_contacts(found.get(row.id, [])) for row in rows
            }

            return _render(
                "admin/privacy.html",
                await _context(session, request, admin, rows=rows, identities=identities),
            )

    @router.get("/clients", response_class=HTMLResponse, include_in_schema=False)
    async def clients_page(request: Request) -> Response:
        """§12.2's clients list: the practice's people, busiest first.

        A different population from `/admin/privacy` and for a different
        question -- who is the practice seeing, and what has this person booked
        before.
        """
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            practice = await get_practice(session)
            zone = ZoneInfo(practice.timezone)
            rows = [
                {
                    "id": str(summary.client_id),
                    "name": summary.display_name,
                    "contact": _identity_contacts(summary.identities),
                    "requests": summary.requests,
                    "last_session": _in_zone(summary.last_session, zone),
                    "next_session": _in_zone(summary.next_session, zone),
                    "since": summary.created_at.astimezone(zone).strftime("%Y-%m-%d"),
                }
                for summary in await list_clients_for_admin(session)
            ]

            return _render(
                "admin/clients.html",
                await _context(session, request, admin, rows=rows),
            )

    @router.get("/clients/{client_id}", response_class=HTMLResponse, include_in_schema=False)
    async def client_detail(request: Request, client_id: str) -> Response:
        """One person: how to reach them, and everything they have booked.

        Resolves for an erased client too, though the list does not show them:
        a booking elsewhere in the admin UI must never link to a dead page.
        """
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            try:
                client = (
                    await session.execute(select(Client).where(Client.id == UUID(client_id)))
                ).scalar_one_or_none()
            except ValueError:
                return Response(status_code=404)
            if client is None:
                return Response(status_code=404)

            requests = (
                (
                    await session.execute(
                        select(BookingRequest)
                        .where(BookingRequest.client_id == client.id)
                        .order_by(BookingRequest.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            waitlist_entries = (
                (
                    await session.execute(
                        select(WaitlistEntry)
                        .where(WaitlistEntry.client_id == client.id)
                        .order_by(WaitlistEntry.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )

            return _render(
                "admin/client_detail.html",
                await _context(
                    session,
                    request,
                    admin,
                    client=client,
                    identities=_identity_contacts(await identities_for(session, client.id)),
                    # The same summary row the requests list draws, so a
                    # client's history reads exactly like the queue it came from
                    # -- and carries no `problem_text` (hard rule 8).
                    rows=await _summaries(session, requests),
                    waitlist_entries=waitlist_entries,
                ),
            )

    # --- Health (§12.2, §16.8) ---------------------------------------------

    @router.get("/status", response_class=HTMLResponse, include_in_schema=False)
    async def status_page(request: Request) -> Response:
        """The checks in full, with what to do about each.

        Reads the file the worker wrote; it does not check anything itself
        (§12.2). The point of the dot in the header is that nobody has to
        remember to open this page -- but when they do, it has to say whether
        to carry on or to call for help, in those words.
        """
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            return _render(
                "admin/status.html",
                await _context(session, request, admin),
            )

    # --- Help (§12.2) -------------------------------------------------------

    @router.get("/help", response_class=HTMLResponse, include_in_schema=False)
    async def help_page(request: Request, lang: str = "en") -> Response:
        """The admin guide, served from the installation it documents.

        A standalone page rather than one of these templates: it ships its own
        navigation, search, and print stylesheet, and it is the one admin
        surface that is not English-only -- the therapist reads it in Russian
        while the console around it stays in English (DESIGN.md §11).
        """
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

        return HTMLResponse(help_guide(lang))

    # --- Maintenance: configuration and backups (§16.6, §16.7) -------------

    async def _import_error(session: AsyncSession, detail: str) -> str:
        """A refusal, worded from the catalogue like every other admin error.

        `detail` comes from `ConfigInvalid`, which names the section and the
        offending value: the therapist is the person who will fix the file.
        """
        return await get_text(session, LOCALE, "admin.maintenance.invalid", detail=detail)

    async def _maintenance(
        session: AsyncSession,
        request: Request,
        admin: AdminUser,
        *,
        report: ImportReport | None = None,
        error: str = "",
    ) -> Response:
        return _render(
            "admin/maintenance.html",
            await _context(
                session,
                request,
                admin,
                dumps=list_dumps(),
                report=report,
                error=error,
            ),
            status_code=400 if error else 200,
        )

    @router.get("/maintenance", response_class=HTMLResponse, include_in_schema=False)
    async def maintenance_page(request: Request) -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")
            return await _maintenance(session, request, admin)

    @router.get("/maintenance/config/export", include_in_schema=False)
    async def config_export_route(request: Request) -> Response:
        """The admin-editable configuration as a JSON download (§16.7).

        Unlike a dump this carries no client data at all, which is what makes
        it safe to send to whoever is rebuilding the install (DESIGN.md §21.1).
        """
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            payload = await export_config(session)
            practice = await get_practice(session)
            session.add(
                AuditLog(
                    practice_id=practice.id,
                    actor_type=ActorType.admin,
                    actor_id=str(admin.id),
                    action="config.export",
                    entity_type="practice",
                    entity_id=str(practice.id),
                )
            )

            stamp = now_utc().strftime("%Y-%m-%d")
            return Response(
                dump_config(payload),
                media_type="application/json",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="psychobooking-config-{stamp}.json"'
                    )
                },
            )

    @router.post("/maintenance/config/import", response_class=HTMLResponse, include_in_schema=False)
    async def config_import_route(
        request: Request,
        file: UploadFile,
        apply: str = Form("0"),
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """Preview or apply a config file.

        One route with two modes on purpose (§12.2): a preview that runs
        different code from the apply is a preview of nothing. `apply=0` runs
        the identical import inside a savepoint and rolls it back.
        """
        if not csrf_ok(request, csrf_token):
            return Response(status_code=403)

        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            # Read one byte past the cap rather than trusting Content-Length,
            # and never write the upload to disk (§17).
            raw = await file.read(MAX_CONFIG_UPLOAD + 1)
            if len(raw) > MAX_CONFIG_UPLOAD:
                return await _maintenance(
                    session,
                    request,
                    admin,
                    error=await _import_error(session, "the file is larger than 5 MB"),
                )

            applying = apply == "1"
            try:
                payload = load_config(raw)
                report = await import_config(session, payload, apply=applying)
            except ConfigInvalid as exc:
                # The savepoint inside import_config has already rolled back;
                # nothing partial reaches the commit below.
                return await _maintenance(
                    session, request, admin, error=await _import_error(session, str(exc))
                )

            if applying:
                practice = await get_practice(session)
                session.add(
                    AuditLog(
                        practice_id=practice.id,
                        actor_type=ActorType.admin,
                        actor_id=str(admin.id),
                        action="config.import",
                        entity_type="practice",
                        entity_id=str(practice.id),
                        meta=report.as_meta(),
                    )
                )
                # The translations cache is process-wide and the import wrote
                # straight past it.
                invalidate_cache()

            return await _maintenance(session, request, admin, report=report)

    @router.get("/maintenance/backups/{filename}", include_in_schema=False)
    async def backup_download_route(request: Request, filename: str) -> Response:
        """Hand back one dump the sidecar produced (§16.6).

        Read-only by construction: there is no route that writes, overwrites,
        or deletes a dump, and none that restores one (DESIGN.md §21.5).
        """
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

            path = resolve_dump(filename)
            if path is None:
                return Response(status_code=404)

            practice = await get_practice(session)
            session.add(
                AuditLog(
                    practice_id=practice.id,
                    actor_type=ActorType.admin,
                    actor_id=str(admin.id),
                    action="backup.download",
                    entity_type="backup",
                    entity_id=filename,
                )
            )

            # FileResponse streams: a dump is not read into memory (§12.2).
            return FileResponse(
                path, media_type="application/octet-stream", filename=filename
            )
    return router


# --- Helpers ----------------------------------------------------------------


def _week_start(value: str, zone: ZoneInfo) -> date:
    """The Monday of the week `value` names, in the practice's own clock.

    §12.2: a date that will not parse means the current week, and a date that
    is not a Monday is snapped to the one before it -- so a hand-edited URL
    lands on a whole week rather than on a seven-day window starting Thursday.
    """
    try:
        anchor = date.fromisoformat(value)
    except ValueError:
        anchor = datetime.now(UTC).astimezone(zone).date()
    return anchor - timedelta(days=anchor.weekday())


async def _schedule(session: AsyncSession, start: str) -> dict[str, Any]:
    """§12.2's week schedule: seven day columns, plus what has no cell.

    Entries are placed by the **local wall-clock date** of their start, never by
    an offset from the week's first instant. A week containing a DST transition
    is not 168 hours long, and arithmetic on the instant quietly moves a day's
    worth of sessions across the boundary twice a year.
    """
    practice = await get_practice(session)
    zone = ZoneInfo(practice.timezone)
    monday = _week_start(start, zone)

    # Widened a day at each end, then filtered by local date below: a zone whose
    # midnight moves must not be able to clip the first or the last column.
    entries = await booking.scheduled_in_window(
        session,
        window_from=datetime.combine(monday - timedelta(days=1), time.min, tzinfo=zone).astimezone(
            UTC
        ),
        window_to=datetime.combine(monday + timedelta(days=8), time.min, tzinfo=zone).astimezone(
            UTC
        ),
    )

    columns: dict[date, list[dict[str, Any]]] = {
        monday + timedelta(days=offset): [] for offset in range(7)
    }
    for entry in entries:
        if entry.starts_at is None:  # not possible from this query; not worth a crash
            continue
        local = entry.starts_at.astimezone(zone)
        day = columns.get(local.date())
        if day is None:  # the widened edge; belongs to a neighbouring week
            continue
        day.append(
            {
                "uuid": str(entry.uuid),
                "time": local.strftime("%H:%M"),
                "name": entry.display_name or "no name",
                "status": entry.status.value,
            }
        )

    today = datetime.now(UTC).astimezone(zone).date()
    return {
        "days": [
            {
                "label": day.strftime("%a %d %b"),
                "today": day == today,
                "entries": rows,
            }
            for day, rows in columns.items()
        ],
        "unscheduled": [
            {
                "uuid": str(entry.uuid),
                "name": entry.display_name or "no name",
                "status": entry.status.value,
                "wanted": entry.desired_time_text or "",
            }
            for entry in await booking.unscheduled_for_admin(session)
        ],
        "week_from": monday.strftime("%d %b"),
        "week_to": (monday + timedelta(days=6)).strftime("%d %b %Y"),
        "prev": (monday - timedelta(days=7)).isoformat(),
        "next": (monday + timedelta(days=7)).isoformat(),
        "this_week": _week_start("", zone).isoformat(),
        "is_this_week": monday == _week_start("", zone),
    }


async def _session_type_codes(session: AsyncSession) -> dict[int, str]:
    """Every session type's code, by id. There are a handful of them."""
    rows = (await session.execute(select(SessionType.id, SessionType.code))).all()
    return {int(row[0]): str(row[1]) for row in rows}


@dataclass(frozen=True, slots=True)
class _Contact:
    """One way to reach a client: what it says, and what clicking it does."""

    label: str
    #: None where the channel has no way of being opened from a browser.
    href: str | None


def _identity_contact(channel: Channel, external_id: str, verified_at: Any) -> _Contact:
    """One way to reach a client, as one line.

    The email says whether it is verified, because §13.3 delivers nothing to an
    unverified address -- a therapist waiting on a reply that was never sent
    should not have to work that out from the delivery log. Telegram vouches for
    its own ids, so verification is not a question there.

    Both are links, so answering somebody does not begin with selecting an
    address and copying it. `mailto:` always resolves. `tg://user?id=` resolves
    only where the therapist's own Telegram client already knows that person --
    the *bot* has spoken to them, her account may never have -- so it is a
    shortcut when it works and an ordinary-looking id when it does not. An
    interim affordance: a proper "reply from here" belongs to the UI pass, not
    to a column (DESIGN.md §20.2).
    """
    label = f"{channel.value}: {external_id}"
    if channel is Channel.email:
        label += " (verified)" if verified_at else " (unverified)"
        return _Contact(label=label, href=f"mailto:{external_id}")
    if channel is Channel.telegram and external_id.isdigit():
        return _Contact(label=label, href=f"tg://user?id={external_id}")
    return _Contact(label=label, href=None)


def _in_zone(value: datetime | None, zone: ZoneInfo) -> str:
    """An instant in the practice's clock, or nothing at all. DESIGN.md §8."""
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M") if value else ""


def _identity_contacts(identities: Sequence[Any]) -> list[_Contact]:
    """How to reach a client, one line per channel (§12.2)."""
    return [
        _identity_contact(identity.channel, identity.external_id, identity.verified_at)
        for identity in identities
    ]


@dataclass(frozen=True, slots=True)
class _ClientFacts:
    """What a request row needs to know about the person behind it (§12.2)."""

    name: str
    contact: tuple[_Contact, ...]


_NO_FACTS = _ClientFacts(name="", contact=())


async def _client_facts(
    session: AsyncSession, requests: Sequence[BookingRequest]
) -> dict[Any, _ClientFacts]:
    """The name and the contact identities of every client behind these
    requests, by client id, in one query.

    §12.2: a request carries the name it was submitted under, which is often
    nothing, and the client behind it may still have one. It carries no way of
    reaching anybody at all, so the identities come from here too -- joined
    rather than fetched per client, because the list renders two hundred rows
    and its query count must not depend on how many.
    """
    ids = {request.client_id for request in requests}
    if not ids:
        return {}

    rows = (
        await session.execute(
            select(
                Client.id,
                Client.display_name,
                Identity.channel,
                Identity.external_id,
                Identity.verified_at,
            )
            .outerjoin(Identity, Identity.client_id == Client.id)
            .where(Client.id.in_(ids))
        )
    ).all()

    names: dict[Any, str] = {}
    contacts: dict[Any, list[tuple[bool, str, _Contact]]] = {}
    for client_id, display_name, channel, external_id, verified_at in rows:
        names[client_id] = str(display_name) if display_name else ""
        found = contacts.setdefault(client_id, [])
        if channel is None:
            # The outer join's empty half: an erased client keeps their row and
            # their bookings, and loses every identity (§16).
            continue
        # Sorted with Telegram first, because §13.3 tries Telegram first.
        found.append(
            (
                channel is not Channel.telegram,
                str(external_id),
                _identity_contact(channel, str(external_id), verified_at),
            )
        )

    return {
        client_id: _ClientFacts(
            name=name,
            # Keyed on the first two, so `_Contact` never has to be orderable.
            contact=tuple(
                contact for _, _, contact in sorted(contacts[client_id], key=lambda row: row[:2])
            ),
        )
        for client_id, name in names.items()
    }


async def _summaries(
    session: AsyncSession, requests: Sequence[BookingRequest]
) -> list[dict[str, Any]]:
    """The requests list, in two queries rather than two per row.

    `_summary` looks up the practice and the session type itself, which is right
    for one request and wrong for two hundred: the list route was issuing four
    hundred queries to render a table whose lookups are all the same.
    """
    practice = await get_practice(session)
    zone = ZoneInfo(practice.timezone)
    codes = await _session_type_codes(session)
    facts = await _client_facts(session, requests)
    return [_summary_row(request, zone, codes, facts) for request in requests]


async def _summary(session: AsyncSession, request: BookingRequest) -> dict[str, Any]:
    """One row, fetching what it needs. For the pages that render exactly one."""
    practice = await get_practice(session)
    return _summary_row(
        request,
        ZoneInfo(practice.timezone),
        await _session_type_codes(session),
        await _client_facts(session, [request]),
    )


def _summary_row(
    request: BookingRequest,
    zone: ZoneInfo,
    session_type_codes: dict[int, str],
    client_facts: dict[Any, _ClientFacts],
) -> dict[str, Any]:
    """One row of the requests list.

    `uuid` MUST appear (§6.5). `problem_text` is included on the *detail* page
    only, never in a list, a log, or an email (hard rule 8).

    The name falls back to the client's (§12.2): a request carries whatever it
    was submitted under, which for the web is usually nothing at all, and a row
    the therapist cannot attribute to anybody is not much of a row.

    `contact` is there for the rows where even that fails. §12.2 argued the case
    for identities on the request page; the list is where she decides what to
    open, so a row she cannot answer from is one she has to open to find out she
    still cannot. Identities are not `problem_text` and hard rule 8 does not
    reach them.
    """
    session_type = session_type_codes[request.session_type_id]
    facts = client_facts.get(request.client_id, _NO_FACTS)

    return {
        "uuid": str(request.uuid),
        "status": request.status.value,
        "session_type": session_type,
        "modality": request.modality.value,
        "name": request.display_name or facts.name,
        "contact": facts.contact,
        "created": request.created_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
        "scheduled": request.scheduled_start.astimezone(zone).strftime("%Y-%m-%d %H:%M")
        if request.scheduled_start
        else "",
        "desired": request.desired_time_text or "",
    }


def _parse_local(value: str, practice_tz: str) -> datetime | None:
    """`YYYY-MM-DDTHH:MM` in the practice timezone -> an aware UTC instant.

    The therapist enters times in her own clock (DESIGN.md §8); storage is UTC.
    """
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return None
    from datetime import UTC

    return naive.replace(tzinfo=ZoneInfo(practice_tz)).astimezone(UTC)


#: Settings that arrive as checkboxes: absent means false, which a plain
#: "only what was submitted" read would silently treat as "unchanged".
_BOOLEAN_SETTINGS = (
    "availability_on",
    "fallback_to_negotiation",
    "negotiation_enabled",
    "auto_confirm_slots",
)

_INT_SETTINGS = (
    "slot_hold_minutes",
    "pending_expiry_hours",
    "cancel_window_hours",
    "retention_months",
)


def _settings_changes(form: Any) -> dict[str, Any]:
    changes: dict[str, Any] = {}

    for field in _BOOLEAN_SETTINGS:
        changes[field] = bool(form.get(field))

    for field in _INT_SETTINGS:
        raw = form.get(field)
        if raw:
            changes[field] = int(raw)

    for field in ("name", "timezone", "default_language"):
        raw = form.get(field)
        if raw:
            changes[field] = str(raw)

    # A native enum column will not take a bare string.
    mode = form.get("booking_mode")
    if mode:
        changes["booking_mode"] = BookingMode(str(mode))

    for field in ("clinic_onsite_url", "online_meeting_url"):
        if field in form:
            changes[field] = str(form.get(field) or "") or None

    offsets = form.get("reminder_offsets_min")
    if offsets is not None:
        # An empty array disables reminders (§6.1).
        changes["reminder_offsets_min"] = [
            int(part.strip()) for part in str(offsets).split(",") if part.strip()
        ]

    return changes


__all__ = ["build_router"]
