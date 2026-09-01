"""The proof-of-work gate (IMPLEMENTATION.md §17, §12.1).

Four things have to hold for the gate to be worth having, and three of them
are the ones that get left out: a forged challenge is refused, a stale one is
refused, and a **replayed** one is refused. Without the last, a script solves
one puzzle and posts the answer ten thousand times.

`solve` is the reference implementation the browser's JavaScript is checked
against; the tests use it because a test that computed the answer its own way
would be a second definition of the rule.
"""

from __future__ import annotations

import hashlib
import time

import pytest

from app.channels.web import captcha


@pytest.fixture(autouse=True)
def _fresh_spent_set() -> None:
    """Spent nonces are process-local (`captcha._spent`), so one test's
    solution must not be another's replay."""
    captcha.reset()


def test_a_solved_challenge_is_accepted() -> None:
    challenge = captcha.issue(8)
    solution = captcha.solve(challenge)

    assert solution is not None
    assert captcha.verify(challenge, solution)


def test_the_same_solution_is_refused_the_second_time() -> None:
    """The difference between a gate and a formality."""
    challenge = captcha.issue(8)
    solution = captcha.solve(challenge)
    assert solution is not None

    assert captcha.verify(challenge, solution)
    assert not captcha.verify(challenge, solution)


def test_a_challenge_this_process_did_not_sign_is_refused() -> None:
    """Otherwise the client sets its own difficulty, which is the same as
    setting it to zero."""
    challenge = captcha.issue(8)
    nonce, expires, _difficulty, signature = challenge.split(".")

    forged = f"{nonce}.{expires}.8.{signature[:-1]}x"
    assert not captcha.verify(forged, "0")

    # The difficulty is inside the signed payload, so lowering it invalidates
    # the signature rather than the puzzle.
    weakened = f"{nonce}.{expires}.1.{signature}"
    assert not captcha.verify(weakened, "0")


def test_a_stale_challenge_is_refused() -> None:
    challenge = captcha.issue(8)
    nonce, _expires, difficulty, _signature = challenge.split(".")
    expired = int(time.time()) - 1
    payload = f"{nonce}.{expired}.{difficulty}"
    # Signed correctly and still refused: the signature says we issued it, the
    # timestamp says it is no longer ours to honour.
    stale = f"{payload}.{captcha._sign(payload)}"

    assert captcha.solve(stale) is not None
    assert not captcha.verify(stale, str(captcha.solve(stale)))


@pytest.mark.parametrize("solution", ["", "0", "not-a-number", "-1", "1" * 40])
def test_an_answer_that_is_not_the_answer_is_refused(solution: str) -> None:
    challenge = captcha.issue(16)
    assert not captcha.verify(challenge, solution)


def test_the_difficulty_is_clamped_to_what_settings_allows() -> None:
    """`issue` is called with a column value; a row edited by hand should not
    be able to switch the gate off or make it impossible."""
    assert captcha.issue(0).split(".")[2] == "8"
    assert captcha.issue(999).split(".")[2] == "24"


def test_leading_zero_bits_counts_what_it_says() -> None:
    assert captcha.leading_zero_bits(bytes([0xFF, 0, 0, 0])) == 0
    assert captcha.leading_zero_bits(bytes([0x7F, 0, 0, 0])) == 1
    assert captcha.leading_zero_bits(bytes([0x00, 0xFF, 0, 0])) == 8
    assert captcha.leading_zero_bits(bytes(4)) == 32


def test_the_solution_is_a_sha256_of_the_nonce_and_the_counter() -> None:
    """The shape the browser has to reproduce, written down once.

    `nonce . counter`, SHA-256, and the first four bytes must open with at
    least `difficulty` zero bits. If this test changes, `partials/pow.html`
    changes with it.
    """
    challenge = captcha.issue(8)
    nonce = challenge.split(".")[0]
    solution = captcha.solve(challenge)
    assert solution is not None

    digest = hashlib.sha256(f"{nonce}.{solution}".encode()).digest()
    assert captcha.leading_zero_bits(digest) >= 8
