"""Client sessions and CSRF for the web channel (IMPLEMENTATION.md §17).

Clients get a signed cookie rather than a database session. `admin_session`
exists for the therapist (§6.3) because her session must be revocable; a client
session carries one fact -- which client this is -- and a short expiry, so a
signed value costs nothing and adds no table §6 does not define.

Signed with SECRET_KEY over HMAC-SHA256. No new dependency: `itsdangerous` and
Starlette's SessionMiddleware would do the same thing, and the stack in §2 is
deliberately short.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
import weakref
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.models import AdminSession, AdminUser

#: §5.1 keeps login tokens to 30 minutes. The session they produce lasts longer
#: -- a client who came back to check on a request should not have to ask for a
#: new link every half hour.
SESSION_TTL_SECONDS = 14 * 24 * 3600

CLIENT_COOKIE = "pb_client"
#: `__Host-` makes the browser refuse the cookie unless it is Secure, Path=/ and
#: has no Domain, so a sibling subdomain cannot plant one (F15). It cannot be
#: used over plain http, which only development does; there the bare name stands.
CSRF_COOKIE = "__Host-pb_csrf"
CSRF_COOKIE_PLAIN = "pb_csrf"
CSRF_FIELD = "csrf_token"

#: A token is good for this long. A form left open overnight is refused and the
#: page reload mints a new one; a stolen token stops working on its own (F15).
CSRF_TTL_SECONDS = 12 * 3600

#: A token with less than this left is replaced when a page is rendered, so a
#: page loaded late in a token's life does not post into a 403 minutes later.
CSRF_REFRESH_BELOW_SECONDS = 2 * 3600


def _sign(payload: bytes) -> str:
    settings = get_settings()
    digest = hmac.new(settings.secret_key.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _encode(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{body}.{_sign(raw)}"


def _decode(value: str) -> dict[str, Any] | None:
    body, _, signature = value.partition(".")
    if not signature:
        return None
    padding = "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(body + padding)
    except (ValueError, TypeError):
        return None
    # compare_digest so a forged cookie cannot be found a byte at a time.
    if not hmac.compare_digest(signature, _sign(raw)):
        return None
    try:
        decoded: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return decoded


def _cookie_kwargs() -> dict[str, Any]:
    """§17: HttpOnly, Secure, SameSite=Lax.

    `Secure` is dropped only when BASE_URL is plain http, which is development;
    a Secure cookie over http is simply never sent, and the resulting "login
    does nothing" is a miserable thing to debug.
    """
    settings = get_settings()
    return {
        "httponly": True,
        "secure": settings.base_url.startswith("https://"),
        "samesite": "lax",
        "path": "/",
    }


def client_session_value(client_id: UUID) -> str:
    """The signed cookie value for a client session, so a page rendered in the
    same response that starts the session can bind its CSRF token to it."""
    return _encode({"cid": str(client_id), "exp": int(time.time()) + SESSION_TTL_SECONDS})


def issue_client_session(response: Response, client_id: UUID, value: str | None = None) -> None:
    """Start a session. Called after a login token is consumed."""
    value = value or client_session_value(client_id)
    response.set_cookie(CLIENT_COOKIE, value, max_age=SESSION_TTL_SECONDS, **_cookie_kwargs())


def current_client_id(request: Request) -> UUID | None:
    """The signed-in client, or None."""
    raw = request.cookies.get(CLIENT_COOKIE)
    if not raw:
        return None
    data = _decode(raw)
    if not data or data.get("exp", 0) < time.time():
        return None
    try:
        return UUID(str(data["cid"]))
    except (KeyError, ValueError):
        return None


def end_client_session(response: Response) -> None:
    response.delete_cookie(CLIENT_COOKIE, path="/")


# --- CSRF (§17: a token on every mutating form) -----------------------------
#
# Double-submit, bound to the session (F15): the token is `nonce.expiry.mac`,
# the mac an HMAC over the nonce, the expiry and the caller's session cookies.
# A token minted for one session is therefore refused on another, even though it
# is validly signed and the cookie and the field agree -- which is exactly what
# an attacker who can plant a cookie but not read the victim's session has.


def csrf_cookie_name() -> str:
    return CSRF_COOKIE if get_settings().base_url.startswith("https://") else CSRF_COOKIE_PLAIN


def _binding(
    request: Request, client_cookie: str | None = None, admin_cookie: str | None = None
) -> str:
    admin = admin_cookie if admin_cookie is not None else request.cookies.get(ADMIN_COOKIE, "")
    client = client_cookie if client_cookie is not None else request.cookies.get(CLIENT_COOKIE, "")
    return f"{admin}|{client}"


def _csrf_mac(nonce: str, expiry: str, binding: str) -> str:
    return _sign(f"csrf|{nonce}|{expiry}|{binding}".encode())


def _mint_csrf(binding: str) -> str:
    nonce = secrets.token_urlsafe(16)
    expiry = str(int(time.time()) + CSRF_TTL_SECONDS)
    return f"{nonce}.{expiry}.{_csrf_mac(nonce, expiry, binding)}"


def _csrf_valid(token: str, binding: str, *, min_left: float = 0) -> bool:
    parts = token.split(".")
    if len(parts) != 3:
        return False
    nonce, expiry, mac = parts
    if not expiry.isdigit() or int(expiry) - time.time() < min_left:
        return False
    return hmac.compare_digest(mac, _csrf_mac(nonce, expiry, binding))


def issue_csrf(response: Response, token: str | None = None) -> str:
    """Set the CSRF cookie, to the value the form is carrying.

    `token` MUST be the value rendered into the page -- minting a fresh one here
    would set a cookie that disagrees with every form on the page just served,
    and every submission would be rejected. Without one the cookie is bound to
    no session at all and the next page replaces it.
    """
    token = token or _mint_csrf("|")
    response.set_cookie(
        csrf_cookie_name(),
        token,
        max_age=CSRF_TTL_SECONDS,
        httponly=False,
        secure=get_settings().base_url.startswith("https://"),
        samesite="lax",
        path="/",
    )
    return token


def csrf_token_for(
    request: Request,
    *,
    new_client_cookie: str | None = None,
    new_admin_cookie: str | None = None,
) -> str:
    """The token to embed in a form, reusing the cookie's while it is valid for
    this session. The `new_*` cookies are a session this very response starts."""
    binding = _binding(request, new_client_cookie, new_admin_cookie)
    existing = request.cookies.get(csrf_cookie_name())
    # Only a full page replaces a token that is running out. An HTMX partial
    # lands inside a page whose other forms still carry the old one, so it keeps
    # any token that is still valid.
    partial = request.headers.get("hx-request") == "true"
    min_left = 0 if partial else CSRF_REFRESH_BELOW_SECONDS
    if existing and _csrf_valid(existing, binding, min_left=min_left):
        return existing
    return _mint_csrf(binding)


def csrf_ok(request: Request, submitted: str | None) -> bool:
    """The cookie is present and valid for this session, and so is the submitted token.

    Not "the two are equal": a full page load in tab B replaces the cookie, and Save in tab A
    still carries the token it was rendered with. Any token still valid and bound to the same
    session (the HMAC covers the session cookies) is as good as the cookie's own -- a token for
    another session fails the binding, and a cross-site POST carries no (SameSite=Lax) cookie.
    """
    cookie = request.cookies.get(csrf_cookie_name())
    if not cookie or not submitted:
        return False
    binding = _binding(request)
    return _csrf_valid(cookie, binding) and _csrf_valid(submitted, binding)


# --- Admin sessions (§6.3, §17) ---------------------------------------------

ADMIN_COOKIE = "pb_admin"

#: Not specified. A week is long enough that the therapist is not signing in
#: constantly, short enough that a forgotten browser stops being a way in.
ADMIN_SESSION_TTL = timedelta(days=7)


def _hash_token(raw: str) -> str:
    """Only the digest is stored, exactly as for client tokens (§6.2)."""
    return hashlib.sha256(raw.encode()).hexdigest()


#: Each Argon2 verify holds 64 MiB (argon2-cffi's default). Run in threads with
#: no bound, a few parallel logins from a few addresses would put the 512 MB web
#: container over its limit. Two at a time; the rest queue and are never refused.
MAX_PARALLEL_VERIFIES = 2

#: A semaphore or lock belongs to the loop it first waited on, so one set per
#: loop. In production there is one loop and so one set.
_gates: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[asyncio.Semaphore, asyncio.Lock]
] = weakref.WeakKeyDictionary()


def _loop_gates() -> tuple[asyncio.Semaphore, asyncio.Lock]:
    loop = asyncio.get_running_loop()
    found = _gates.get(loop)
    if found is None:
        found = _gates[loop] = (asyncio.Semaphore(MAX_PARALLEL_VERIFIES), asyncio.Lock())
    return found


def _verify_gate() -> asyncio.Semaphore:
    return _loop_gates()[0]


async def _verify_off_loop(hasher: Any, stored: str, password: str) -> None:
    """`hasher.verify` in a thread, at most `MAX_PARALLEL_VERIFIES` at once."""
    async with _verify_gate():
        await asyncio.to_thread(hasher.verify, stored, password)


async def authenticate_admin(
    session: AsyncSession, username: str, password: str
) -> AdminUser | None:
    """Verify a username and password. Argon2id (§17).

    Returns None for both an unknown user and a wrong password: telling them
    apart tells an attacker which usernames exist. The hash is verified even
    when the user does not exist, so the two paths take the same time.
    """
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    hasher = PasswordHasher()
    admin = (
        await session.execute(select(AdminUser).where(AdminUser.username == username))
    ).scalar_one_or_none()

    if admin is None:
        # Argon2 over a throwaway hash, so a missing user costs the same as a
        # wrong password.
        try:
            await _verify_off_loop(hasher, await _dummy(), password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass
        return None

    # Argon2 is deliberately slow and holds the GIL-free C routine for tens of
    # milliseconds; run on the loop it stalls the site and the Telegram webhook
    # for every login attempt (F10). A thread keeps the loop turning.
    try:
        await _verify_off_loop(hasher, admin.password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return None

    admin.last_login_at = _now()
    await session.flush()
    return admin


_dummy_hash: str | None = None


async def _dummy() -> str:
    """A real Argon2id hash of a value nobody knows.

    It has to be genuine: a fabricated string fails parsing immediately, which
    would make the unknown-user path measurably faster than a wrong password
    and give away which usernames exist. Computed once, lazily, so importing
    this module does not cost a KDF round -- and inside the same gate as every
    verify, behind a lock with a second look, so a burst of unknown-user logins
    right after a restart computes it once and never holds more than the
    allowed 64 MiB hashes at a time.
    """
    global _dummy_hash
    if _dummy_hash is None:
        _, lock = _loop_gates()
        async with lock:
            if _dummy_hash is None:
                from argon2 import PasswordHasher

                async with _verify_gate():
                    _dummy_hash = await asyncio.to_thread(
                        PasswordHasher().hash, secrets.token_urlsafe(32)
                    )
    return _dummy_hash


def _now() -> datetime:
    return datetime.now(UTC)


async def start_admin_session(session: AsyncSession, response: Response, admin: AdminUser) -> str:
    """§17: rotated on login. Every previous session for this admin is revoked,
    so signing in from a new device ends the old one."""
    await session.execute(
        update(AdminSession)
        .where(
            AdminSession.admin_user_id == admin.id,
            AdminSession.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )

    raw = secrets.token_urlsafe(32)
    session.add(
        AdminSession(
            admin_user_id=admin.id,
            token_hash=_hash_token(raw),
            expires_at=_now() + ADMIN_SESSION_TTL,
        )
    )
    await session.flush()

    response.set_cookie(
        ADMIN_COOKIE,
        raw,
        max_age=int(ADMIN_SESSION_TTL.total_seconds()),
        **_cookie_kwargs(),
    )
    return raw


async def current_admin(session: AsyncSession, request: Request) -> AdminUser | None:
    """The signed-in therapist, or None."""
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw:
        return None

    row = (
        await session.execute(
            select(AdminSession).where(AdminSession.token_hash == _hash_token(raw))
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at <= _now():
        return None

    return (
        await session.execute(select(AdminUser).where(AdminUser.id == row.admin_user_id))
    ).scalar_one_or_none()


async def end_admin_session(session: AsyncSession, request: Request, response: Response) -> None:
    raw = request.cookies.get(ADMIN_COOKIE)
    if raw:
        await session.execute(
            update(AdminSession)
            .where(AdminSession.token_hash == _hash_token(raw))
            .values(revoked_at=_now())
        )
        await session.flush()
    response.delete_cookie(ADMIN_COOKIE, path="/")
