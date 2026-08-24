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
from collections import defaultdict, deque
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

#: {(limit name, key): timestamps}. Bounded by pruning on every check, so a
#: burst of distinct keys cannot grow it without bound for longer than a window.
_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def _prune(bucket: deque[float], window: int, now: float) -> None:
    while bucket and bucket[0] <= now - window:
        bucket.popleft()


def check(limit: Limit, key: str) -> bool:
    """Record an attempt. False means the caller has had their allowance.

    Counted whether or not the attempt succeeds: a limiter that only counts
    failures is trivially defeated by succeeding at something cheap.
    """
    now = time.monotonic()
    bucket = _hits[(limit.name, key)]
    _prune(bucket, limit.window_seconds, now)

    if len(bucket) >= limit.allowance:
        return False

    bucket.append(now)
    return True


def remaining(limit: Limit, key: str) -> int:
    """How many attempts are left. For tests and diagnostics."""
    bucket = _hits[(limit.name, key)]
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
