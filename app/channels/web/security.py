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

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from uuid import UUID

from fastapi import Request, Response

from app.config import get_settings

#: §5.1 keeps login tokens to 30 minutes. The session they produce lasts longer
#: -- a client who came back to check on a request should not have to ask for a
#: new link every half hour.
SESSION_TTL_SECONDS = 14 * 24 * 3600

CLIENT_COOKIE = "pb_client"
CSRF_COOKIE = "pb_csrf"
CSRF_FIELD = "csrf_token"


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


def issue_client_session(response: Response, client_id: UUID) -> None:
    """Start a session. Called after a login token is consumed."""
    value = _encode({"cid": str(client_id), "exp": int(time.time()) + SESSION_TTL_SECONDS})
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


def issue_csrf(response: Response, token: str | None = None) -> str:
    """Set the CSRF cookie, to the value the form is carrying.

    Double-submit: the same value goes in an httpOnly-free cookie and in a
    hidden field, and the two must match. `token` MUST be the value rendered
    into the page -- minting a fresh one here would set a cookie that disagrees
    with every form on the page just served, and every submission would be
    rejected.
    """
    token = token or _encode({"t": int(time.time())})
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=False,
        secure=get_settings().base_url.startswith("https://"),
        samesite="lax",
        path="/",
    )
    return token


def csrf_token_for(request: Request) -> str:
    """The token to embed in a form, reusing the cookie's when it is valid."""
    existing = request.cookies.get(CSRF_COOKIE)
    if existing and _decode(existing) is not None:
        return existing
    return _encode({"t": int(time.time())})


def csrf_ok(request: Request, submitted: str | None) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not submitted:
        return False
    if _decode(submitted) is None:
        return False
    return hmac.compare_digest(cookie, submitted)
