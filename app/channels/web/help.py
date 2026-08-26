"""The admin guide, in the languages it has been written in (§12.2).

The pages live in `app/channels/web/guides/` rather than in `docs/`, because the
image copies `app/` and the point is that the guide describes the version this
installation is actually running -- a guide that ships beside the source but
not beside the deployment documents whatever was true when someone last looked.

They are complete HTML documents with their own navigation, search, and print
stylesheet, so they are served as they are rather than rendered through
`admin/base.html`. Nothing external is fetched: everything is inline.

This is also the one admin surface that is not English-only. DESIGN.md §11 puts
the console in English; nothing in it says the manual has to be, and a Russian
manual is the difference between a therapist reading it and not.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

HELP_DIR = Path(__file__).resolve().parent / "guides"

#: The guides that exist. `en` is the fallback for anything else, including
#: `hy` -- an Armenian guide has not been written, and a missing page is worse
#: than the wrong language.
HELP_LANGUAGES = ("en", "ru")
DEFAULT_HELP_LANGUAGE = "en"


def help_languages() -> tuple[str, ...]:
    return HELP_LANGUAGES


@lru_cache(maxsize=len(HELP_LANGUAGES))
def help_guide(lang: str) -> str:
    """One guide, by language. Cached: the files are baked into the image and
    cannot change while the process runs."""
    chosen = lang if lang in HELP_LANGUAGES else DEFAULT_HELP_LANGUAGE
    return (HELP_DIR / f"admin-guide.{chosen}.html").read_text(encoding="utf-8")


__all__ = ["DEFAULT_HELP_LANGUAGE", "HELP_DIR", "HELP_LANGUAGES", "help_guide", "help_languages"]
