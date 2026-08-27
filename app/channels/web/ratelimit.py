"""Rate limits (IMPLEMENTATION.md §17).

    admin login        5 per 15 min per IP
    magic-link         3 per hour per email, 10 per hour per IP
    booking submission 5 per hour per client

Those are the only unauthenticated write paths in the system, which is why §17
singles them out.

Two mechanisms, chosen per limit rather than uniformly:

  - The **per-identity** limits count rows that already exist. Magic links are
    `auth_token` rows; booking submissions are `booking_request` rows. Both
    survive a restart, need no new table, and are exactly the thing being
    limited.

  - The **per-IP** limits are an in-memory sliding window, because an IP address
    is not stored anywhere and §6 defines no table for it. Redis is forbidden
    (§2) and one ASGI process serves this deployment, so a process-local window
    is the whole population. It resets on deploy; an attacker cannot cause a
    deploy, and the per-identity limits still hold across one.

`Retry-After` is not returned deliberately: telling a caller exactly when to
come back is a convenience for the caller, and the caller here is the one being
limited.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

#: §17, verbatim.
ADMIN_LOGIN_PER_IP = 5
ADMIN_LOGIN_WINDOW = 15 * 60

MAGIC_LINK_PER_EMAIL = 3
MAGIC_LINK_PER_IP = 10
MAGIC_LINK_WINDOW = 3600

BOOKING_PER_CLIENT = 5
BOOKING_WINDOW = 3600


@dataclass(frozen=True, slots=True)
class Limit:
    name: str
    allowance: int
    window_seconds: int


ADMIN_LOGIN = Limit("admin_login", ADMIN_LOGIN_PER_IP, ADMIN_LOGIN_WINDOW)
MAGIC_LINK_IP = Limit("magic_link_ip", MAGIC_LINK_PER_IP, MAGIC_LINK_WINDOW)

#: {(limit name, key): timestamps}.
_hits: dict[tuple[str, str], deque[float]] = {}

#: The longest window any limit here uses. A bucket whose newest timestamp is
#: older than this cannot affect any of them, whichever limit it belongs to,
#: which is what lets the sweep below work without knowing.
_MAX_WINDOW = max(ADMIN_LOGIN_WINDOW, MAGIC_LINK_WINDOW)

#: Pruning empties a deque but leaves its key behind, so the map grew by one
#: entry per address that ever called and never shrank -- slowly, but without
#: any bound. Swept once it passes this, which is amortised rather than walking
#: every key on every check.
_SWEEP_AT = 512


def _prune(bucket: deque[float], window: int, now: float) -> None:
    while bucket and bucket[0] <= now - window:
        bucket.popleft()


def _sweep(now: float) -> None:
    """Drop keys that can no longer affect any limit."""
    cutoff = now - _MAX_WINDOW
    for ident in [i for i, bucket in _hits.items() if not bucket or bucket[-1] <= cutoff]:
        del _hits[ident]


def check(limit: Limit, key: str) -> bool:
    """Record an attempt. False means the caller has had their allowance.

    Counted whether or not the attempt succeeds: a limiter that only counts
    failures is trivially defeated by succeeding at something cheap.
    """
    now = time.monotonic()
    if len(_hits) >= _SWEEP_AT:
        _sweep(now)

    bucket = _hits.setdefault((limit.name, key), deque())
    _prune(bucket, limit.window_seconds, now)

    if len(bucket) >= limit.allowance:
        return False

    bucket.append(now)
    return True


def remaining(limit: Limit, key: str) -> int:
    """How many attempts are left. For tests and diagnostics.

    Reads without recording: asking must not be what creates the entry, or
    every diagnostic call would leak one.
    """
    bucket = _hits.get((limit.name, key))
    if bucket is None:
        return limit.allowance
    _prune(bucket, limit.window_seconds, time.monotonic())
    return max(0, limit.allowance - len(bucket))


def reset() -> None:
    """Clear every window. Tests only -- never called by the application."""
    _hits.clear()


def client_ip(request: object) -> str:
    """The caller's address.

    uvicorn runs with --proxy-headers, so `request.client.host` is already the
    real address behind Caddy or a tunnel rather than the proxy's (§16.3).
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host) if host else "unknown"
