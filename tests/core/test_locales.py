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


@pytest.mark.parametrize("lang", LANGUAGES)
def test_a_rejection_carries_the_therapist_words_without_a_label(lang: str) -> None:
    """Her answer to a request she cannot take is as often a referral as a
    refusal, and the client reads it verbatim under a body that has already
    said the request could not be scheduled. "Reason:" in front of "my
    colleague Anna works with exactly this" reads as a refusal being justified.

    A cancellation is the opposite case and keeps its label: a session called
    off genuinely has a reason.
    """
    key = "intent.request.rejected.client.reason"
    value = CATALOGUE[lang][key]
    assert value == "{reason}", f"{lang}.yaml labels the note: {value}"
    assert "{reason}" in CATALOGUE[lang]["intent.request.cancelled.client.reason"]


def test_no_armenian_value_ends_in_a_latin_colon() -> None:
    """NP4a: the Armenian full stop is "։" (U+0589); a Latin ":" at the end of a sentence reads as a
    typo."""
    bad = {k: v for k, v in CATALOGUE["hy"].items() if v.rstrip().endswith(":")}
    assert not bad, f"hy values ending in ':': {sorted(bad)}"


def test_one_voice_in_armenian_and_the_word_for_a_request_in_russian() -> None:
    """NP4a: she speaks in the first person ("Խնդրում եմ", not "Խնդրում ենք"); ru says "заявка",
    never "запрос"."""
    assert not [k for k, v in CATALOGUE["hy"].items() if "Խնդրում ենք" in v]
    assert not [
        k for k, v in CATALOGUE["ru"].items() if "запрос" in v.lower().replace("запросите", "")
    ]


def _literal_get_text_calls() -> list[tuple[str, str, int, frozenset[str]]]:
    """(file, key, line, keyword names) of every `get_text(..., "literal.key", name=...)` call in
    app/."""
    import ast
    from pathlib import Path

    found = []
    for path in sorted((Path(__file__).resolve().parents[2] / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "get_text" or len(node.args) < 3:
                continue
            key = node.args[2]
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if any(k.arg is None for k in node.keywords):  # **fmt: not checkable statically
                continue
            found.append(
                (path.name, key.value, node.lineno, frozenset(k.arg for k in node.keywords))
            )
    return found


#: An argument a call passes that no language's text uses (it was there before NP4a; passing it
# loses nothing).
HARMLESS_EXTRA_ARGUMENTS = {"intent.request.confirmed.client.body": frozenset({"uuid"})}


def test_a_text_and_every_call_that_formats_it_agree_on_their_placeholders() -> None:
    """NP4a: `get_text` returns the text unformatted when a placeholder is missing, and a deploy
    never overwrites a stored
    row. So a call site and every language of the key must name the same placeholders: a call
    without arguments against
    text holding `{timezone}` shows the braces to a client, and an argument the new text dropped
    silently loses its
    information (Telegram's zone line)."""
    problems = []
    for where, key, line, kwargs in _literal_get_text_calls():
        for lang in ("en", "ru", "hy"):
            text = CATALOGUE[lang].get(key)
            if text is None:
                continue
            placeholders = set(PLACEHOLDER.findall(text))
            if placeholders != set(kwargs - HARMLESS_EXTRA_ARGUMENTS.get(key, frozenset())):
                problems.append(
                    f"{where}:{line} {key} [{lang}] text {sorted(placeholders)} "
                    f"vs call {sorted(kwargs)}"
                )
    assert not problems, "\n".join(problems)


# --- NP3c item 13: guards the placeholder test above does not reach ------------------------------


def _app_trees() -> list[tuple[str, object]]:
    import ast
    from pathlib import Path

    return [
        (path.name, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted((Path(__file__).resolve().parents[2] / "app").rglob("*.py"))
    ]


def _text_keys_named_in_app() -> list[tuple[str, int, str, bool]]:
    """(file, line, key, is_prefix) for every key `get_text` is asked for: a literal, or the
    constant start of an f-string (`f"request.status.{x}"` -> prefix `request.status.`), and
    every value of the `_labels` table in the web client (a label name -> its key)."""
    import ast

    found: list[tuple[str, int, str, bool]] = []
    for name, tree in _app_trees():
        for node in ast.walk(tree):  # type: ignore[arg-type]
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if called == "get_text" and len(node.args) >= 3:
                    key = node.args[2]
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        found.append((name, node.lineno, key.value, False))
                    elif isinstance(key, ast.JoinedStr) and key.values:
                        head = key.values[0]
                        if isinstance(head, ast.Constant) and isinstance(head.value, str):
                            found.append((name, node.lineno, head.value, True))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_labels":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Dict):
                        for value in inner.values:
                            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                                found.append((name, value.lineno, value.value, False))
    return found


def test_every_text_key_the_app_asks_for_exists_in_english() -> None:
    """A key nobody wrote is shown to a client as the bare key (`error.rate_limited`, NP4a's Opus).
    Covers literal keys, the start of f-string keys and the web client's `_labels` table."""
    english = CATALOGUE["en"]
    problems = []
    for where, line, key, is_prefix in _text_keys_named_in_app():
        exists = any(k.startswith(key) for k in english) if is_prefix else key in english
        if not exists:
            problems.append(f"{where}:{line} {key!r}{' (prefix)' if is_prefix else ''}")
    assert not problems, "\n".join(problems)


#: `_labels` keys whose text holds a placeholder, and who fills it: a `_labels` text is fetched
#: with no arguments, so every one of these shows its braces unless the page overrides or
#: `.replace`s it. Adding a key with a placeholder to `_labels` means adding it here, deliberately.
LABELS_WITH_A_PLACEHOLDER = {
    "onsite_info": "booking.onsite_info",  # `_context` overrides it with the clinic address
    "timezone_shown": "booking.timezone.shown",  # templates .replace('{zone}', ...)
    "slot_held": "booking.slot.held",  # `_details_page` overrides it with the hold minutes
    "submitted": "booking.submitted",  # templates .replace('{uuid}', ...)
    "step": "common.step",  # templates .replace('{n}'/'{total}', ...)
    "reference": "request.reference",  # templates .replace('{ref}', ...)
    # the admin console's own `_labels`
    "delivery_credentials": "admin.delivery.credentials",  # delivery.html .replace('{count}')
}


def _labels_table() -> dict[str, str]:
    import ast

    table: dict[str, str] = {}
    for _name, tree in _app_trees():
        for node in ast.walk(tree):  # type: ignore[arg-type]
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_labels":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Dict):
                        for k, v in zip(inner.keys, inner.values, strict=False):
                            if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                                table[str(k.value)] = str(v.value)
    return table


def test_a_label_text_with_a_placeholder_is_on_the_list_of_those_filled_by_hand() -> None:
    table = _labels_table()
    assert table, "the `_labels` table was not found"
    holding = {
        name: key
        for name, key in table.items()
        if any(PLACEHOLDER.search(CATALOGUE[lang].get(key, "")) for lang in LANGUAGES)
    }
    assert holding == LABELS_WITH_A_PLACEHOLDER, (
        "a `_labels` text holds a placeholder nothing is known to fill (or the list is stale): "
        f"{sorted(set(holding) ^ set(LABELS_WITH_A_PLACEHOLDER))}"
    )
