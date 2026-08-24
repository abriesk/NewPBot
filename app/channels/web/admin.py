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
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.web import ratelimit
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
from app.core.enums import ActorType, BookingMode, Modality, OutboxStatus, RequestStatus
from app.core.errors import DomainError, NotFound
from app.core.models import (
    AdminUser,
    AuditLog,
    BookingRequest,
    Client,
    ContentBlock,
    ContentTopic,
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
from app.core.services.clients import erase_client, export_client, identities_for
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
    async def requests_list(request: Request, status: str = "") -> Response:
        async with unit_of_work() as session:
            admin = await current_admin(session, request)
            if admin is None:
                return _back("/admin/login")

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
                    rows=[await _summary(session, r) for r in rows],
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
        csrf_token: str = Form("", alias=CSRF_FIELD),
    ) -> Response:
        """§12.2: approve, propose, reject, cancel."""
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
                        session, booking_request.id, reason=reason or "cancelled"
                    )
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
            rows = (
                (
                    await session.execute(
                        select(Translation)
                        .where(Translation.lang == chosen)
                        .order_by(Translation.key)
                    )
                )
                .scalars()
                .all()
            )
            return _render(
                "admin/translations.html",
                await _context(
                    session,
                    request,
                    admin,
                    rows=rows,
                    lang=chosen,
                    languages=LANGUAGES,
                    missing=await missing_keys(session, chosen),
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
            return _render(
                "admin/session_types.html",
                await _context(session, request, admin, rows=rows),
            )

    @router.post("/session-types", include_in_schema=False)
    async def session_types_save(
        request: Request,
        code: str = Form(...),
        duration_min: int = Form(60),
        price_amount_minor: str = Form(""),
        price_currency: str = Form(""),
        is_active: str = Form(""),
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

            row.duration_min = duration_min
            row.price_amount_minor = int(price_amount_minor) if price_amount_minor else None
            row.price_currency = price_currency or None
            row.is_active = bool(is_active)
            await session.flush()
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
                return _back("/admin/clients", "type erase to confirm")

            try:
                await erase_client(session, UUID(client_id))
            except (NotFound, ValueError):
                return Response(status_code=404)

            return _back("/admin/clients", "erased")

    @router.get("/clients", response_class=HTMLResponse, include_in_schema=False)
    async def clients_page(request: Request) -> Response:
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
            identities: dict[str, list[str]] = {}
            for client in rows:
                identities[str(client.id)] = [
                    f"{i.channel.value}: {i.external_id}"
                    for i in await identities_for(session, client.id)
                ]

            return _render(
                "admin/clients.html",
                await _context(session, request, admin, rows=rows, identities=identities),
            )

    return router


# --- Helpers ----------------------------------------------------------------


async def _summary(session: AsyncSession, request: BookingRequest) -> dict[str, Any]:
    """One row of the requests list.

    `uuid` MUST appear (§6.5). `problem_text` is included on the *detail* page
    only, never in a list, a log, or an email (hard rule 8).
    """
    practice = await get_practice(session)
    zone = ZoneInfo(practice.timezone)
    session_type = (
        await session.execute(
            select(SessionType.code).where(SessionType.id == request.session_type_id)
        )
    ).scalar_one()

    return {
        "uuid": str(request.uuid),
        "status": request.status.value,
        "session_type": session_type,
        "modality": request.modality.value,
        "name": request.display_name or "",
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
