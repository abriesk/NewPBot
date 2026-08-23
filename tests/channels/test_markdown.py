"""Renderer golden tests (IMPLEMENTATION.md §11.5).

§11.5 names five cases explicitly, and each has a test here:

  - Russian text containing `.`, `-`, `!`, `(`, `)`
  - Armenian text
  - a link whose label contains `<`
  - a 6000-character block
  - a code block containing `</b>`

The first two are the reason this module exists at all: v1.0 sent raw Markdown
in the MarkdownV2 parse mode, which fails on the first `.` or `-` in ordinary
Russian prose.

These live under tests/channels rather than tests/core because the renderer
produces channel-specific output and imports nh3, which core may not.
"""

from __future__ import annotations

import re

import pytest

from app.core.services.content import MarkdownNotAllowed, validate_markdown
from app.render.markdown import (
    TELEGRAM_PART_LIMIT,
    TELEGRAM_RULE,
    escape_telegram,
    to_email,
    to_email_text,
    to_telegram,
    to_telegram_blocks,
    to_web_html,
)

RUSSIAN = "Здравствуйте! Консультация длится 50-60 минут (онлайн). Стоимость — 5.000 драм."
ARMENIAN = "Բարև Ձեզ։ Խորհրդատվությունը տևում է 50-60 րոպե։"

TAG = re.compile(r"</?([a-z]+)[^>]*>")


def _tags_used(html: str) -> set[str]:
    return {m.group(1) for m in TAG.finditer(html)}


# --- The five named cases ---------------------------------------------------


def test_russian_punctuation_survives_untouched() -> None:
    """No escaping of `.`, `-`, `!`, `(`, `)` -- that was MarkdownV2's problem,
    and HTML mode does not have it."""
    parts = to_telegram(RUSSIAN)
    assert len(parts) == 1
    assert parts[0] == RUSSIAN
    for char in ".-!()":
        assert f"\\{char}" not in parts[0]


def test_armenian_text_survives_untouched() -> None:
    parts = to_telegram(ARMENIAN)
    assert parts == [ARMENIAN]
    # The Armenian full stop is not ASCII punctuation and must not be mangled.
    assert "։" in parts[0]


def test_a_link_label_containing_a_less_than_is_escaped() -> None:
    parts = to_telegram("[a < b](https://example.test/x)")
    assert len(parts) == 1
    assert "&lt;" in parts[0]
    # The angle bracket must not survive as a tag delimiter.
    assert _tags_used(parts[0]) == {"a"}
    assert 'href="https://example.test/x"' in parts[0]


def test_a_six_thousand_character_block_becomes_several_parts() -> None:
    """M3 acceptance: split at block boundaries, each part within the limit."""
    paragraph = "Это предложение повторяется много раз. " * 160
    assert len(paragraph) > 6000

    parts = to_telegram(paragraph)
    assert len(parts) > 1
    assert all(len(part) <= TELEGRAM_PART_LIMIT for part in parts)


def test_a_code_block_containing_a_closing_b_tag_is_escaped() -> None:
    parts = to_telegram("```\n</b>\n```")
    assert len(parts) == 1
    assert "&lt;/b&gt;" in parts[0]
    # <pre> is the only real tag; the </b> must not close anything.
    assert _tags_used(parts[0]) == {"pre"}


# --- Telegram emitter (§11.2) -----------------------------------------------


def test_escape_touches_only_the_three_characters() -> None:
    assert escape_telegram("&<>") == "&amp;&lt;&gt;"
    # Quotes and everything else are left exactly as the author typed them.
    assert escape_telegram("\"'.-!()*_`") == "\"'.-!()*_`"


def test_ampersand_is_escaped_before_the_brackets() -> None:
    """Otherwise `&lt;` becomes `&amp;lt;` and the client sees the escape."""
    assert escape_telegram("<") == "&lt;"
    assert escape_telegram("&lt;") == "&amp;lt;"


@pytest.mark.parametrize("level", ["#", "##", "###"])
def test_headings_become_a_bold_line(level: str) -> None:
    assert to_telegram_blocks(f"{level} Условия работы") == ["<b>Условия работы</b>"]


def test_lists_use_bullets_and_numbers() -> None:
    assert to_telegram_blocks("- one\n- two") == ["• one\n• two"]
    assert to_telegram_blocks("1. one\n2. two") == ["1. one\n2. two"]


def test_a_horizontal_rule_becomes_a_divider() -> None:
    assert to_telegram_blocks("---") == [TELEGRAM_RULE]


def test_emphasis_maps_onto_the_supported_tags() -> None:
    assert to_telegram_blocks("**bold** and *italic* and `code`") == [
        "<b>bold</b> and <i>italic</i> and <code>code</code>"
    ]


def test_only_supported_tags_are_emitted() -> None:
    """Telegram accepts roughly ten tags; anything else breaks the message."""
    from app.render.markdown import TELEGRAM_TAGS

    source = "# H\n\npara **b** *i* `c` [l](https://e.test)\n\n- x\n\n> quote\n\n---\n\n```\nz\n```"
    for part in to_telegram(source):
        assert _tags_used(part) <= TELEGRAM_TAGS


def test_parts_never_split_inside_a_link() -> None:
    """A link straddling two messages renders as broken markup in both."""
    link = "[канал консультаций](https://example.test/very/long/path)"
    source = "\n\n".join([f"Параграф {i}. {link}" for i in range(200)])

    for part in to_telegram(source):
        assert part.count("<a ") == part.count("</a>")


def test_an_oversized_code_block_keeps_its_pre_tags_balanced() -> None:
    """A single code block longer than the limit has to go somewhere; what must
    not happen is a part carrying an unbalanced <pre>."""
    source = "```\n" + ("x" * 9000) + "\n```"
    parts = to_telegram(source)

    assert len(parts) > 1
    for part in parts:
        assert part.count("<pre>") == part.count("</pre>") == 1
        assert len(part) <= TELEGRAM_PART_LIMIT


def test_markdownv2_appears_nowhere_in_the_renderer() -> None:
    """Hard rule 6."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "app" / "render" / "markdown.py"
    text = source.read_text(encoding="utf-8")
    assert "parse_mode='MarkdownV2'" not in text
    assert 'parse_mode="MarkdownV2"' not in text


# --- Web emitter (§11.3) ----------------------------------------------------


def test_headings_are_shifted_one_level_down() -> None:
    """The page already has an h1; a block starting at h1 would give it two."""
    assert "<h2>" in to_web_html("# Условия")
    assert "<h3>" in to_web_html("## Подробнее")
    assert "<h4>" in to_web_html("### Ещё")
    assert "<h1>" not in to_web_html("# Условия")


def test_web_output_is_sanitised() -> None:
    # Raw HTML is rejected at save time, but the sanitiser is the second line of
    # defence for anything already stored.
    assert "<script>" not in to_web_html("text <script>alert(1)</script>")


def test_web_output_keeps_russian_and_armenian_intact() -> None:
    assert "Здравствуйте" in to_web_html(RUSSIAN)
    assert "Բարև" in to_web_html(ARMENIAN)


# --- Email emitter (§11.4) --------------------------------------------------


def test_email_renders_links_as_text_and_url() -> None:
    assert to_email_text("[our terms](https://example.test/terms)") == (
        "our terms (https://example.test/terms)"
    )


def test_email_returns_plain_and_html() -> None:
    plain, html = to_email("# Title\n\nBody text.")
    assert "Title" in plain
    assert "<" not in plain
    assert "<h2>" in html


def test_email_plain_text_carries_no_markup() -> None:
    plain = to_email_text("**bold** and *italic* and `code`")
    assert plain == "bold and italic and code"


# --- Validation (§11.1) -----------------------------------------------------


def test_a_table_is_rejected_at_save_time() -> None:
    """M3 acceptance."""
    with pytest.raises(MarkdownNotAllowed, match="table"):
        validate_markdown("| a | b |\n|---|---|\n| 1 | 2 |")


def test_an_image_is_rejected() -> None:
    with pytest.raises(MarkdownNotAllowed, match="image"):
        validate_markdown("![alt](https://example.test/x.png)")


def test_raw_html_is_rejected() -> None:
    with pytest.raises(MarkdownNotAllowed, match="HTML"):
        validate_markdown("<div>hello</div>")


def test_a_nested_list_is_rejected() -> None:
    with pytest.raises(MarkdownNotAllowed, match=r"[Nn]ested"):
        validate_markdown("- one\n    - deeper")


def test_a_footnote_is_rejected() -> None:
    with pytest.raises(MarkdownNotAllowed, match="footnote"):
        validate_markdown("text[^1]\n\n[^1]: note")


@pytest.mark.parametrize("level", ["####", "#####", "######"])
def test_headings_below_h3_are_rejected(level: str) -> None:
    with pytest.raises(MarkdownNotAllowed, match="h3"):
        validate_markdown(f"{level} too deep")


@pytest.mark.parametrize(
    "source",
    [
        "Ordinary paragraph.",
        "**bold** *italic* `code`",
        "# h1\n\n## h2\n\n### h3",
        "- a\n- b",
        "1. a\n2. b",
        "> quoted",
        "---",
        "```\ncode\n```",
        "[link](https://example.test)",
        RUSSIAN,
        ARMENIAN,
    ],
)
def test_the_accepted_subset_validates(source: str) -> None:
    validate_markdown(source)


def test_the_error_carries_a_localised_key_for_the_admin_ui() -> None:
    """§11.1 wants a localised admin error; core supplies the key and detail
    and lets the adapter do the localising."""
    with pytest.raises(MarkdownNotAllowed) as caught:
        validate_markdown("| a |\n|---|\n| 1 |")
    assert caught.value.translation_key == "admin.content.invalid_markdown"
    assert caught.value.detail
