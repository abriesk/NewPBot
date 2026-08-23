"""Locale catalogue tests (IMPLEMENTATION.md §15, hard rule 10).

`locales/en.yaml` is the normative key catalogue. These tests are the reason a
missing translation shows up in CI rather than as a bare key in front of a
client.
"""

from __future__ import annotations

import re

import pytest

from app.seed import LANGUAGES, load_locale_catalogue
from app.seed import LOCALES_DIR as LOCALES

#: The admin surface is English by design -- DESIGN.md §11 puts the admin UI and
#: all operational errors in English, and en.yaml marks the block "English only;
#: never translated". Parity therefore applies to the client-facing keys.
ADMIN_PREFIX = "admin."

CATALOGUE = load_locale_catalogue()

#: Telegram HTML, Markdown emphasis, and bullets all come from the renderer.
#: v1.0 embedded Telegram HTML in its strings, which leaked literal tags into
#: email and the web.
MARKUP = re.compile(r"</?[a-z]+>|\*\*|__|^\s*[-*•]\s", re.IGNORECASE | re.MULTILINE)

PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}", re.IGNORECASE)


def _client_keys(lang: str) -> set[str]:
    return {k for k in CATALOGUE[lang] if not k.startswith(ADMIN_PREFIX)}


def test_am_appears_nowhere() -> None:
    """Hard rule 5. `am` is Amharic; Armenian is `hy`."""
    assert "am" not in LANGUAGES
    assert set(LANGUAGES) == {"ru", "hy", "en"}


@pytest.mark.parametrize("lang", ["ru", "hy"])
def test_client_facing_keys_match_the_english_catalogue(lang: str) -> None:
    english = _client_keys("en")
    other = _client_keys(lang)
    assert not english - other, f"missing from {lang}.yaml: {sorted(english - other)}"
    assert not other - english, f"not in the en catalogue: {sorted(other - english)}"


def test_the_admin_namespace_is_english_only() -> None:
    for lang in ("ru", "hy"):
        stray = [k for k in CATALOGUE[lang] if k.startswith(ADMIN_PREFIX)]
        assert not stray, f"{lang}.yaml should not carry admin keys: {stray}"
    assert any(k.startswith(ADMIN_PREFIX) for k in CATALOGUE["en"])


def test_the_yes_key_survives_yaml_boolean_coercion() -> None:
    """Under YAML 1.1 a bare `yes:` parses as True, which would seed the key
    `common.True` and make every `common.yes` lookup miss silently."""
    for lang in LANGUAGES:
        assert "common.yes" in CATALOGUE[lang]
        assert "common.no" in CATALOGUE[lang]
        assert "common.True" not in CATALOGUE[lang]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_values_carry_no_markup(lang: str) -> None:
    offenders = {k: v for k, v in CATALOGUE[lang].items() if MARKUP.search(v)}
    assert not offenders, f"markup belongs in the renderer, not in {lang}.yaml: {offenders}"


@pytest.mark.parametrize("lang", ["ru", "hy"])
def test_no_translation_invents_a_placeholder(lang: str) -> None:
    """A placeholder the payload does not supply raises KeyError at format
    time, in front of a client.

    The check is a subset, not equality, and deliberately so. Dropping a
    placeholder is safe -- `str.format` ignores extra keyword arguments -- and
    is sometimes a legitimate translation choice; hy.yaml currently ships one
    such line marked `# TODO: add {uuid}`. Adding one that does not exist is the
    failure mode worth catching.
    """
    invented: dict[str, set[str]] = {}
    for key, english in CATALOGUE["en"].items():
        if key.startswith(ADMIN_PREFIX):
            continue
        translated = CATALOGUE[lang].get(key, "")
        if not translated.strip():
            continue
        extra = set(PLACEHOLDER.findall(translated)) - set(PLACEHOLDER.findall(english))
        if extra:
            invented[key] = extra
    assert not invented, f"{lang}.yaml uses placeholders the payload lacks: {invented}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_empty_value_is_deliberately_marked_todo(lang: str) -> None:
    """Untranslated copy is legitimate and expected (§15) -- the final wording
    comes from the therapist. An empty value that is *not* marked is an
    accident: a translator clearing a line, or a key added without copy.
    """
    source = (LOCALES / f"{lang}.yaml").read_text(encoding="utf-8")
    marked = {
        line.split(":", 1)[0].strip()
        for line in source.splitlines()
        if "TODO" in line and ":" in line
    }
    unmarked = [
        key
        for key, value in CATALOGUE[lang].items()
        if not value.strip() and not any(key.endswith(suffix) for suffix in marked)
    ]
    assert not unmarked, f"empty but unmarked in {lang}.yaml: {unmarked}"


def test_english_is_complete() -> None:
    """en.yaml is the reference copy and the last fallback before the bare key,
    so it is the one file that may not have gaps."""
    empty = [k for k, v in CATALOGUE["en"].items() if not v.strip()]
    assert not empty, f"en.yaml is the fallback of last resort; empty: {empty}"
