"""The one request reference every channel shows (NP5a).

A uuid is 36 characters nobody reads aloud. The web has always shown a client its first block in
upper case (`5A969404`); mails and Telegram now do the same, and the admin's request search finds
a request by it. The full uuid stays in every URL and callback: this is a label, never a key.
"""

from __future__ import annotations

from uuid import UUID


def short_ref(value: UUID | str) -> str:
    """`5a969404-...` -> `5A969404`. A string that is not a uuid is shortened the same way."""
    return str(value).split("-")[0].upper()
