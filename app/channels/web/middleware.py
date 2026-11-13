"""Two pure-ASGI middlewares: a body cap enforced while streaming (C3), and
the security headers every response carries (C15).

Pure ASGI rather than Starlette's `BaseHTTPMiddleware`: the body cap has to sit
between the server and the multipart parser and count bytes as they arrive, and
`BaseHTTPMiddleware` buffers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: Sent on every response (C15). `frame-ancestors` is the modern clickjacking
#: control and `X-Frame-Options` its older twin. `no-referrer` because login and
#: request links carry their token in the query string: a link on such a page
#: must not hand it to the next site in the `Referer`.
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"referrer-policy", b"no-referrer"),
)

#: Anything that is not named below. Forms are a few kilobytes; a megabyte is
#: far more than any of them, and a body that size is refused before it is read.
DEFAULT_BODY_LIMIT = 1024 * 1024


class _TooLarge(Exception):
    """The body outgrew its cap. Raised out of `receive` into the parser."""


async def _refuse(send: Send) -> None:
    body = b"Request body too large"
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodyLimitMiddleware:
    """Refuse a request body over its cap, by what has actually arrived.

    A declared `Content-Length` over the cap is refused before a byte is read;
    a body without one (chunked), or one that lies, is cut off the moment the
    bytes received pass the cap. Either way the multipart parser never sees more
    than the cap, so nothing larger is spooled to disk (C3).
    """

    def __init__(
        self, app: ASGIApp, limits: Mapping[str, int], default: int = DEFAULT_BODY_LIMIT
    ) -> None:
        self.app = app
        self.limits = dict(limits)
        self.default = default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        limit = self.limits.get(scope.get("path", ""), self.default)
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > limit:
                    await _refuse(send)
                    return

        seen = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _TooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if started:
                raise
            await _refuse(send)


class SecurityHeadersMiddleware:
    """Add `SECURITY_HEADERS` to every HTTP response that does not set them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                headers = list(message.get("headers", []))
                headers.extend(pair for pair in SECURITY_HEADERS if pair[0] not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, with_headers)


__all__ = [
    "DEFAULT_BODY_LIMIT",
    "SECURITY_HEADERS",
    "BodyLimitMiddleware",
    "SecurityHeadersMiddleware",
]
