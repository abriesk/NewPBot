"""Content blocks, and the Markdown subset they may use (§6.7, §10, §11.1).

Practice information lives in the database as ordered Markdown **blocks**, not
as files. Block granularity is the real answer to Telegram's 4096-character
limit: splitting at boundaries the author chose produces a conversation, where
splitting a long document automatically produces awkward breaks (DESIGN.md
§10.1).

Validation lives here rather than in app/render because it is a domain rule --
what the therapist is *allowed to save* -- and because it must run at save time,
in front of her, rather than at send time in front of a client. The emitters in
app/render/markdown.py import the parser from here, so the subset is defined
once.

markdown-it-py is not on the forbidden-import list (§3); nh3 is, and is used
only by the web emitter.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markdown_it.token import Token
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ContentBlockKind
from app.core.errors import DomainError, NotFound
from app.core.models import ContentBlock, ContentBlockRevision, ContentTopic
from app.core.policies import now_utc
from app.core.services.settings import get_practice

#: §6.7. Keeping a bounded history means a paste that breaks a page at 23:00 can
#: be rolled back, without the table growing without limit.
MAX_REVISIONS = 20

#: Footnote syntax is not parsed (the plugin is not enabled), so it would reach
#: a client as literal `[^1]` text. Rejecting it at save time says so plainly.
FOOTNOTE = re.compile(r"\[\^[^\]]+\]")


class MarkdownNotAllowed(DomainError):
    """A construct outside the §11.1 subset.

    Carries a `detail` for the admin UI to interpolate into the localised
    `admin.content.invalid_markdown` string. Core does not do the localising --
    that is the adapter's job.
    """

    translation_key = "admin.content.invalid_markdown"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


#: Built once. Nothing mutates the parser after construction -- every caller
#: only parses or renders with it -- so rebuilding the rule set on each of the
#: four call sites bought nothing but allocations on the render path.
_PARSER = MarkdownIt("commonmark").enable("table")


def make_parser() -> MarkdownIt:
    """The one parser configuration.

    `table` is enabled deliberately even though tables are rejected: without the
    rule, a pipe table parses as ordinary paragraphs and the validator would
    happily accept something that renders as gibberish.
    """
    return _PARSER


def validate_markdown(body_md: str) -> None:
    """Raise `MarkdownNotAllowed` for anything outside §11.1.

    Accepted: paragraphs, bold, italic, inline code, fenced code, links,
    unordered and ordered lists, headings h1-h3, blockquotes, horizontal rules.

    Rejected: tables, images, raw HTML, nested lists deeper than one level,
    footnotes, headings below h3.
    """
    if FOOTNOTE.search(body_md):
        raise MarkdownNotAllowed("footnotes are not supported")

    tokens = make_parser().parse(body_md)
    list_depth = 0

    for token in tokens:
        if token.type in ("table_open", "thead_open", "tbody_open"):
            raise MarkdownNotAllowed("tables are not supported")
        if token.type in ("html_block", "html_inline"):
            raise MarkdownNotAllowed("raw HTML is not supported")
        if token.type == "heading_open" and token.tag in ("h4", "h5", "h6"):
            raise MarkdownNotAllowed(f"headings below h3 are not supported ({token.tag})")

        if token.type in ("bullet_list_open", "ordered_list_open"):
            list_depth += 1
            if list_depth > 1:
                raise MarkdownNotAllowed("nested lists are not supported")
        elif token.type in ("bullet_list_close", "ordered_list_close"):
            list_depth -= 1

        _validate_inline(token)


def _validate_inline(token: Token) -> None:
    for child in token.children or ():
        if child.type == "image":
            raise MarkdownNotAllowed("images are not supported")
        if child.type == "html_inline":
            raise MarkdownNotAllowed("raw HTML is not supported")


# --- Topics -----------------------------------------------------------------


async def list_menu_topics(session: AsyncSession) -> list[ContentTopic]:
    """Topics the client menu offers, in order.

    `references` is seeded with `show_in_menu = false`: it is sent with waitlist
    confirmations rather than browsed (§20).
    """
    return list(
        (
            await session.execute(
                select(ContentTopic)
                .where(ContentTopic.is_active.is_(True), ContentTopic.show_in_menu.is_(True))
                .order_by(ContentTopic.sort_order, ContentTopic.id)
            )
        )
        .scalars()
        .all()
    )


async def get_topic(session: AsyncSession, topic_code: str) -> ContentTopic:
    topic = (
        await session.execute(select(ContentTopic).where(ContentTopic.code == topic_code))
    ).scalar_one_or_none()
    if topic is None:
        raise NotFound(f"content topic {topic_code!r}")
    return topic


async def get_topic_blocks(
    session: AsyncSession, topic_code: str, lang: str, *, published_only: bool = True
) -> list[ContentBlock]:
    """A topic's blocks, in order.

    Falls back to the practice default language when the requested one has no
    blocks at all -- a client should see the page in the wrong language rather
    than an empty page (DESIGN.md §11).
    """
    topic = await get_topic(session, topic_code)

    blocks = await _blocks_for(session, topic.id, lang, published_only)
    if blocks:
        return blocks

    practice = await get_practice(session)
    if lang != practice.default_language:
        return await _blocks_for(session, topic.id, practice.default_language, published_only)
    return []


async def _blocks_for(
    session: AsyncSession, topic_id: int, lang: str, published_only: bool
) -> list[ContentBlock]:
    stmt = select(ContentBlock).where(ContentBlock.topic_id == topic_id, ContentBlock.lang == lang)
    if published_only:
        stmt = stmt.where(ContentBlock.is_published.is_(True))
    return list((await session.execute(stmt.order_by(ContentBlock.position))).scalars().all())


# --- Blocks -----------------------------------------------------------------


async def upsert_block(
    session: AsyncSession,
    *,
    topic_id: int,
    lang: str,
    position: int,
    body_md: str,
    kind: ContentBlockKind = ContentBlockKind.text,
    link_url: str | None = None,
    is_published: bool = True,
) -> ContentBlock:
    """Create or update the block at `(topic, lang, position)`.

    Validation runs first, so an unsupported construct never reaches the
    database. On update the *previous* body is written to
    `content_block_revision` and `version` is incremented, in this transaction
    (§6.7).
    """
    validate_markdown(body_md)

    practice = await get_practice(session)
    existing = (
        await session.execute(
            select(ContentBlock).where(
                ContentBlock.topic_id == topic_id,
                ContentBlock.lang == lang,
                ContentBlock.position == position,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        block = ContentBlock(
            practice_id=practice.id,
            topic_id=topic_id,
            lang=lang,
            position=position,
            kind=kind,
            body_md=body_md,
            link_url=link_url,
            is_published=is_published,
            version=1,
        )
        session.add(block)
        await session.flush()
        return block

    session.add(
        ContentBlockRevision(
            block_id=existing.id, version=existing.version, body_md=existing.body_md
        )
    )
    existing.body_md = body_md
    existing.kind = kind
    existing.link_url = link_url
    existing.is_published = is_published
    existing.version += 1
    existing.updated_at = now_utc()
    await session.flush()

    await _prune_revisions(session, existing.id)
    return existing


async def _prune_revisions(session: AsyncSession, block_id: int) -> None:
    """Keep the most recent MAX_REVISIONS per block (§6.7)."""
    stale = (
        (
            await session.execute(
                select(ContentBlockRevision)
                .where(ContentBlockRevision.block_id == block_id)
                .order_by(ContentBlockRevision.version.desc())
                .offset(MAX_REVISIONS)
            )
        )
        .scalars()
        .all()
    )
    for revision in stale:
        await session.delete(revision)
    if stale:
        await session.flush()


async def list_revisions(session: AsyncSession, block_id: int) -> list[ContentBlockRevision]:
    return list(
        (
            await session.execute(
                select(ContentBlockRevision)
                .where(ContentBlockRevision.block_id == block_id)
                .order_by(ContentBlockRevision.version.desc())
            )
        )
        .scalars()
        .all()
    )


async def restore_revision(session: AsyncSession, block_id: int, version: int) -> ContentBlock:
    """Roll a block back to an earlier body.

    The restore is itself a write, so the body being replaced is kept as a
    revision too -- rolling back a rollback has to be possible.
    """
    block = (
        await session.execute(select(ContentBlock).where(ContentBlock.id == block_id))
    ).scalar_one_or_none()
    if block is None:
        raise NotFound(f"content block {block_id}")

    revision = (
        await session.execute(
            select(ContentBlockRevision).where(
                ContentBlockRevision.block_id == block_id,
                ContentBlockRevision.version == version,
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        raise NotFound(f"revision {version} of block {block_id}")

    return await upsert_block(
        session,
        topic_id=block.topic_id,
        lang=block.lang,
        position=block.position,
        body_md=revision.body_md,
        kind=block.kind,
        link_url=block.link_url,
        is_published=block.is_published,
    )


async def delete_block(session: AsyncSession, block_id: int) -> None:
    block = (
        await session.execute(select(ContentBlock).where(ContentBlock.id == block_id))
    ).scalar_one_or_none()
    if block is None:
        raise NotFound(f"content block {block_id}")
    await session.delete(block)
    await session.flush()
