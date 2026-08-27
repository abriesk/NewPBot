"""Markdown emitters, one per channel (IMPLEMENTATION.md §11).

Blocks are authored in the §11.1 subset, parsed to a token stream, and emitted
per channel. This module exists because v1.0 passed raw Markdown to Telegram in
the MarkdownV2 parse mode, which fails on the first `.` or `-` in ordinary
Russian text: it requires escaping about eighteen characters and supports
neither headings nor tables.

Telegram output is HTML restricted to the tag subset below. The MarkdownV2
parse mode MUST NOT be used anywhere (hard rule 6); the literal spelling is
kept out of this file so a repository-wide grep for it stays meaningful.

Depends on app.core for the parser configuration, never the other way round.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import nh3
from markdown_it.token import Token

from app.core.services.content import make_parser

#: §11.2. Telegram accepts only these; anything else is shown literally or
#: rejects the message outright.
TELEGRAM_TAGS = frozenset({"b", "i", "u", "s", "a", "code", "pre", "blockquote"})

#: §11.2. Below Telegram's 4096 limit, with room for the caption Telegram adds
#: to nothing and for multi-byte characters counting as one each.
TELEGRAM_PART_LIMIT = 3500

#: §11.2. A horizontal rule, since Telegram has no <hr>.
TELEGRAM_RULE = "──────────"

#: §11.3. The web allowlist. Deliberately close to the accepted subset -- there
#: is no reason for the sanitiser to permit tags the author cannot write.
WEB_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "code",
    "pre",
    "a",
    "ul",
    "ol",
    "li",
    "h2",
    "h3",
    "h4",
    "blockquote",
    "hr",
}
# nh3 manages `rel` itself (it adds noopener/noreferrer), and refuses to run if
# the allowlist also claims it.
WEB_ATTRIBUTES = {"a": {"href", "title"}}


def escape_telegram(text: str) -> str:
    """§11.2: escape `&`, `<`, `>` -- and nothing else.

    Ampersand first, or the escapes escape each other. Note this deliberately
    does *not* escape quotes: Telegram does not need it in text nodes, and doing
    so would put literal `&quot;` in front of clients.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Telegram ---------------------------------------------------------------


def _telegram_inline(tokens: list[Token] | None) -> str:
    """Render one inline token stream to Telegram HTML."""
    out: list[str] = []
    for token in tokens or ():
        if token.type == "text":
            out.append(escape_telegram(token.content))
        elif token.type == "code_inline":
            out.append(f"<code>{escape_telegram(token.content)}</code>")
        elif token.type == "strong_open":
            out.append("<b>")
        elif token.type == "strong_close":
            out.append("</b>")
        elif token.type == "em_open":
            out.append("<i>")
        elif token.type == "em_close":
            out.append("</i>")
        elif token.type == "s_open":
            out.append("<s>")
        elif token.type == "s_close":
            out.append("</s>")
        elif token.type == "link_open":
            # attrGet is typed str | int | float | None; hrefs are always text.
            href = escape_telegram(str(token.attrGet("href") or ""))
            # The href sits inside double quotes, so that one character has to
            # go too -- escape_telegram deliberately leaves it alone elsewhere.
            out.append(f'<a href="{href.replace(chr(34), "&quot;")}">')
        elif token.type == "link_close":
            out.append("</a>")
        elif token.type in ("softbreak", "hardbreak"):
            out.append("\n")
    return "".join(out)


def to_telegram_blocks(body_md: str) -> list[str]:
    """One string per block, before packing into messages.

    Kept separate from `to_telegram` so the splitter can work at block
    boundaries, which is what §11.2 requires.
    """
    tokens = make_parser().parse(body_md)
    blocks: list[str] = []

    index = 0
    list_counter = 0
    in_ordered = False
    pending_items: list[str] = []

    while index < len(tokens):
        token = tokens[index]

        if token.type == "heading_open":
            inline = tokens[index + 1]
            # §11.2: headings become a bold line. Telegram has no <h1>.
            blocks.append(f"<b>{_telegram_inline(inline.children)}</b>")
            index += 3
            continue

        if token.type == "paragraph_open":
            inline = tokens[index + 1]
            rendered = _telegram_inline(inline.children)
            if rendered.strip():
                blocks.append(rendered)
            index += 3
            continue

        if token.type == "fence" or token.type == "code_block":
            blocks.append(f"<pre>{escape_telegram(token.content.rstrip(chr(10)))}</pre>")
            index += 1
            continue

        if token.type == "hr":
            blocks.append(TELEGRAM_RULE)
            index += 1
            continue

        if token.type in ("bullet_list_open", "ordered_list_open"):
            in_ordered = token.type == "ordered_list_open"
            list_counter = 0
            pending_items = []
            index += 1
            continue

        if token.type == "list_item_open":
            list_counter += 1
            # Collect the item's inline content; nesting is rejected at save
            # time, so one level is all that can appear here.
            item_parts: list[str] = []
            index += 1
            while index < len(tokens) and tokens[index].type != "list_item_close":
                if tokens[index].type == "inline":
                    item_parts.append(_telegram_inline(tokens[index].children))
                index += 1
            marker = f"{list_counter}. " if in_ordered else "• "
            pending_items.append(marker + " ".join(p for p in item_parts if p))
            index += 1
            continue

        if token.type in ("bullet_list_close", "ordered_list_close"):
            if pending_items:
                blocks.append("\n".join(pending_items))
            pending_items = []
            index += 1
            continue

        if token.type == "blockquote_open":
            quote_parts: list[str] = []
            depth = 1
            index += 1
            while index < len(tokens) and depth:
                if tokens[index].type == "blockquote_open":
                    depth += 1
                elif tokens[index].type == "blockquote_close":
                    depth -= 1
                elif tokens[index].type == "inline":
                    quote_parts.append(_telegram_inline(tokens[index].children))
                index += 1
            blocks.append(f"<blockquote>{chr(10).join(quote_parts)}</blockquote>")
            continue

        index += 1

    return blocks


def _split_oversized(block: str) -> list[str]:
    """Break one over-long block, per §11.2's cascade.

    Paragraph boundaries first, then the last whitespace before the limit. A
    fenced block is unwrapped, split, and each part re-wrapped, so no part ever
    carries an unbalanced `<pre>` -- "never split inside a code block" read as
    the property that matters, since a single code block longer than the limit
    has to go somewhere.
    """
    is_pre = block.startswith("<pre>") and block.endswith("</pre>")
    body = block[len("<pre>") : -len("</pre>")] if is_pre else block
    limit = TELEGRAM_PART_LIMIT - (len("<pre></pre>") if is_pre else 0)

    chunks: list[str] = []
    remaining = body

    while len(remaining) > limit:
        window = remaining[:limit]

        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = max(window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            # No whitespace at all: a hard cut is the only option left. Back it
            # off an entity it would otherwise land inside -- `&amp;` cut into
            # `&am` and `p;` is not markup Telegram will accept, and text
            # escaped by `escape_telegram` is full of them.
            cut = limit
            unterminated = window.rfind("&")
            if unterminated > 0 and ";" not in window[unterminated:]:
                cut = unterminated

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)

    return [f"<pre>{chunk}</pre>" for chunk in chunks] if is_pre else chunks


def pack_telegram_parts(blocks: Sequence[str]) -> list[str]:
    """Pack already-rendered blocks into messages within Telegram's limit.

    Parts break at block boundaries. A block that is itself too long is split by
    `_split_oversized`; nothing else is ever cut mid-block, so a link never
    straddles two messages. As many blocks as fit share a message, so a short
    notification stays one message and only a long one becomes several.

    Separate from `to_telegram` because §10's intent bodies need the packing and
    must not have the parsing: they are translated sentences with client text
    interpolated into them and escaped, not markdown. Running them through the
    parser would let a client put formatting in the therapist's chat.
    """
    parts: list[str] = []
    current = ""

    for block in blocks:
        pieces = _split_oversized(block) if len(block) > TELEGRAM_PART_LIMIT else [block]

        for piece in pieces:
            if not current:
                current = piece
            elif len(current) + 2 + len(piece) <= TELEGRAM_PART_LIMIT:
                current = f"{current}\n\n{piece}"
            else:
                parts.append(current)
                current = piece

    if current:
        parts.append(current)
    return parts


def to_telegram(body_md: str) -> list[str]:
    """Render markdown to a list of message parts, each within Telegram's limit."""
    return pack_telegram_parts(to_telegram_blocks(body_md))


# --- Web --------------------------------------------------------------------

_HEADING = re.compile(r"<(/?)h([123])>")


def to_web_html(body_md: str) -> str:
    """§11.3: full HTML, headings shifted one level down, then sanitised.

    The shift exists because the page already has an `<h1>`; a block starting
    at `<h1>` would give it two.
    """
    html = make_parser().render(body_md)
    shifted = _HEADING.sub(lambda m: f"<{m.group(1)}h{int(m.group(2)) + 1}>", html)
    return nh3.clean(shifted, tags=WEB_TAGS, attributes=WEB_ATTRIBUTES)


# --- Email ------------------------------------------------------------------


def _email_inline(tokens: list[Token] | None) -> str:
    """Plain text. §11.4 renders links as `text (url)`."""
    out: list[str] = []
    label: list[str] | None = None
    href = ""

    for token in tokens or ():
        if token.type == "link_open":
            label = []
            href = str(token.attrGet("href") or "")
        elif token.type == "link_close" and label is not None:
            text = "".join(label)
            out.append(f"{text} ({href})" if href and href != text else text)
            label = None
        elif token.type in ("text", "code_inline"):
            (label if label is not None else out).append(token.content)
        elif token.type in ("softbreak", "hardbreak"):
            (label if label is not None else out).append("\n")

    return "".join(out)


def to_email_text(body_md: str) -> str:
    """The plain-text part, which is the primary one.

    Email is the least private channel in this system -- shared inboxes,
    lock-screen previews -- so what reaches it is decided by the notification
    policy (§13.4), not here. This only renders what it is given.
    """
    tokens = make_parser().parse(body_md)
    lines: list[str] = []

    index = 0
    counter = 0
    ordered = False

    while index < len(tokens):
        token = tokens[index]
        if token.type in ("heading_open", "paragraph_open"):
            lines.append(_email_inline(tokens[index + 1].children))
            index += 3
            continue
        if token.type in ("fence", "code_block"):
            lines.append(token.content.rstrip("\n"))
            index += 1
            continue
        if token.type == "hr":
            lines.append("---")
            index += 1
            continue
        if token.type in ("bullet_list_open", "ordered_list_open"):
            ordered = token.type == "ordered_list_open"
            counter = 0
            index += 1
            continue
        if token.type == "inline" and tokens[index - 1].type == "paragraph_open":
            index += 1
            continue
        if token.type == "list_item_open":
            counter += 1
            item: list[str] = []
            index += 1
            while index < len(tokens) and tokens[index].type != "list_item_close":
                if tokens[index].type == "inline":
                    item.append(_email_inline(tokens[index].children))
                index += 1
            lines.append(f"{counter}. " if ordered else "- ")
            lines[-1] += " ".join(p for p in item if p)
            index += 1
            continue
        index += 1

    return "\n\n".join(line for line in lines if line.strip())


def to_email_html(body_md: str) -> str:
    """§11.4: a minimal HTML alternative. Same sanitiser as the web."""
    return to_web_html(body_md)


def to_email(body_md: str) -> tuple[str, str]:
    """`(plain, html)` -- plain is primary."""
    return to_email_text(body_md), to_email_html(body_md)


__all__ = [
    "TELEGRAM_PART_LIMIT",
    "TELEGRAM_RULE",
    "TELEGRAM_TAGS",
    "escape_telegram",
    "pack_telegram_parts",
    "to_email",
    "to_email_html",
    "to_email_text",
    "to_telegram",
    "to_telegram_blocks",
    "to_web_html",
]
