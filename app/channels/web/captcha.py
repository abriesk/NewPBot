"""Proof of work in front of the two unauthenticated write paths (§17, §12.1).

`POST /book` and `POST /waitlist` are limited five an hour **per client**
(§17), and a client costs one email address, so a script with a fresh address
each time is not limited at all. Each submission it makes writes a client row,
a request row, a Telegram notification to the therapist, and — the part that
does lasting damage — one sign-in email from her domain to an address the
script chose. This is what stands in front of that.

**Why a puzzle we set ourselves rather than Turnstile.** A hosted captcha needs
a Cloudflare account and per-install keys, loads a third-party script into the
one page a client fills in about their mental health, and calls out to
`siteverify` while somebody is waiting on a form. §12.2 already states that a
practice server may have no outbound network at all, and the client surface
loads no external asset by design. A deployment behind the Cloudflare tunnel
could afford all that; a deployment on a bare VPS with the `plain` profile is
exactly the one this feature exists for, and is the one that could not.

**Why proof of work and not pictures.** The client this service is for is
frequently anxious and sometimes in a hurry, and a puzzle in front of the
booking button is a cost paid by every one of them. Work is paid by the
browser instead: a quarter of a second of hashing, started when the form
renders and finished long before anybody has described their problem. The
therapist turns it on when the forms are being abused and off again afterwards
(`practice.captcha_on`, default off).

**What it does not do.** It does not stop a determined attacker with real
hardware, and it is not a defence against volumetric flooding — that is
Cloudflare's job where Cloudflare is present, and nothing's where it is not.
It makes mass submission expensive, which is the threat in front of a single
practice.

The hash is SHA-256 and the browser computes it in plain JavaScript rather than
through `crypto.subtle`, which is unavailable on a non-secure origin — the
`plain` profile serves over http, and a gate that only works on https would be
missing on the deployment that needs it most.

**What the difficulties cost.** Measured in a desktop browser against the
shipped implementation in `templates/partials/pow.html`: 16 bits took 0.4s and
20 bits took 7.2s, which is the doubling-per-bit the arithmetic predicts. 24,
the ceiling, is therefore around two minutes there and several times that on an
old phone — a number for a practice actually under attack, and one that will
cost real clients bookings while it is set. The default is 16.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time

from app.config import get_settings
from app.core.services.settings import CAPTCHA_DIFFICULTY_MAX, CAPTCHA_DIFFICULTY_MIN

logger = logging.getLogger(__name__)

#: Long enough that a challenge outlives the form it was issued with -- §12.1's
#: details step asks for a description of somebody's problem, which is not a
#: thing to be timed -- and short enough that a harvested one is worthless.
CHALLENGE_TTL_SECONDS = 30 * 60

#: The two hidden fields the forms post back.
CHALLENGE_FIELD = "pow_challenge"
SOLUTION_FIELD = "pow_solution"

#: Solved challenges, until they expire. Single use is not decoration: without
#: it a script solves one puzzle and replays the answer ten thousand times, and
#: the gate is a formality.
#:
#: In memory for the same reason §17's per-IP windows are (`ratelimit.py`): one
#: ASGI process serves this deployment, so the process is the whole population,
#: and a restart only makes an unspent challenge unusable a little early. A
#: table would have to be swept, and §14's sweeps are for things worth keeping.
_spent: dict[str, float] = {}


def _sign(payload: str) -> str:
    secret = get_settings().secret_key.encode()
    digest = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue(difficulty: int) -> str:
    """A challenge for one form: `nonce.expires.difficulty.signature`.

    The difficulty travels **inside** the signed payload rather than being read
    from settings at verification time, so a form somebody already has open
    goes on being solvable at the price it was issued at when the therapist
    turns the dial up mid-flood.
    """
    difficulty = max(CAPTCHA_DIFFICULTY_MIN, min(CAPTCHA_DIFFICULTY_MAX, int(difficulty)))
    nonce = secrets.token_hex(16)
    expires = int(time.time()) + CHALLENGE_TTL_SECONDS
    payload = f"{nonce}.{expires}.{difficulty}"
    return f"{payload}.{_sign(payload)}"


def leading_zero_bits(digest: bytes) -> int:
    """How many zero bits the digest opens with, counted over the first four
    bytes -- `CAPTCHA_DIFFICULTY_MAX` is 24, so nothing beyond them matters and
    the browser can compare a single 32-bit word."""
    word = int.from_bytes(digest[:4], "big")
    if word == 0:
        return 32
    return 32 - word.bit_length()


def solve(challenge: str, *, limit: int = 1 << 26) -> str | None:
    """Find a solution the way a browser does.

    Here rather than in the tests because the suite is not the only caller that
    needs it: this is the reference the JavaScript is checked against, and two
    implementations of one rule drift unless one of them is the definition.
    """
    parts = challenge.split(".")
    if len(parts) != 4:
        return None
    nonce, _, difficulty_raw, _ = parts
    difficulty = int(difficulty_raw)

    for counter in range(limit):
        digest = hashlib.sha256(f"{nonce}.{counter}".encode()).digest()
        if leading_zero_bits(digest) >= difficulty:
            return str(counter)
    return None


def verify(challenge: str, solution: str) -> bool:
    """True only for a challenge this process signed, still fresh, correctly
    solved, and **not seen before**.

    Every rejection is silent to the caller beyond the boolean: which of the
    four failed is not a client's business, and saying would help exactly the
    caller this exists to slow down.
    """
    _prune()

    parts = challenge.split(".")
    if len(parts) != 4:
        return False
    nonce, expires_raw, difficulty_raw, signature = parts

    if not hmac.compare_digest(_sign(f"{nonce}.{expires_raw}.{difficulty_raw}"), signature):
        return False

    try:
        expires = int(expires_raw)
        difficulty = int(difficulty_raw)
    except ValueError:
        return False

    now = time.time()
    if expires < now:
        return False
    if not CAPTCHA_DIFFICULTY_MIN <= difficulty <= CAPTCHA_DIFFICULTY_MAX:
        return False

    if not solution or len(solution) > 32 or not solution.isdigit():
        return False

    digest = hashlib.sha256(f"{nonce}.{solution}".encode()).digest()
    if leading_zero_bits(digest) < difficulty:
        return False

    if nonce in _spent:
        return False
    _spent[nonce] = expires
    return True


def _prune() -> None:
    if len(_spent) < 1024:
        return
    now = time.time()
    for nonce, expires in list(_spent.items()):
        if expires < now:
            del _spent[nonce]


def reset() -> None:
    """Forget every spent challenge. For tests; nothing in the app calls it."""
    _spent.clear()


__all__ = [
    "CHALLENGE_FIELD",
    "CHALLENGE_TTL_SECONDS",
    "SOLUTION_FIELD",
    "issue",
    "leading_zero_bits",
    "reset",
    "solve",
    "verify",
]
