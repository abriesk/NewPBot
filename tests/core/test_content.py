"""Content blocks and revisions (IMPLEMENTATION.md §6.7, §10).

What clients read and the therapist edits, with no deploy in between. The
reading half is ordering and visibility: topics in sort order, blocks in
position order, unpublished blocks hidden from clients, and a language the
practice holds no copy for falling back to the default rather than rendering
an empty page.

The writing half is the safety net around a live editor. A body that fails
validation is refused before it is written, never half-saved; every write
stores the previous body first, so there is always something to roll back
to; and the history is capped at twenty revisions per block, so a page she
edits often cannot grow without bound.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.models import ContentBlockRevision, ContentTopic, Practice
from app.core.services import content
from app.core.services.content import MAX_REVISIONS, MarkdownNotAllowed


async def _topic_id(db: AsyncSession, code: str = "work_terms") -> int:
    topic = await content.get_topic(db, code)
    return int(topic.id)


async def test_seeded_topics_are_in_the_menu_except_references(db: AsyncSession) -> None:
    """§20: `references` is sent with waitlist confirmations, not browsed."""
    codes = [topic.code for topic in await content.list_menu_topics(db)]
    assert codes == ["work_terms", "qualification", "about_psychotherapy"]
    assert "references" not in codes


async def test_topics_come_back_in_sort_order(db: AsyncSession) -> None:
    topics = await content.list_menu_topics(db)
    assert [t.sort_order for t in topics] == sorted(t.sort_order for t in topics)


async def test_an_unknown_topic_raises(db: AsyncSession) -> None:
    with pytest.raises(NotFound):
        await content.get_topic(db, "no_such_topic")


async def test_blocks_come_back_in_position_order(db: AsyncSession) -> None:
    topic_id = await _topic_id(db)
    for position, text in ((2, "second"), (0, "zeroth"), (1, "first")):
        await content.upsert_block(
            db, topic_id=topic_id, lang="ru", position=position, body_md=text
        )

    blocks = await content.get_topic_blocks(db, "work_terms", "ru")
    assert [b.body_md for b in blocks] == ["zeroth", "first", "second"]


async def test_unpublished_blocks_are_hidden_from_clients(db: AsyncSession) -> None:
    topic_id = await _topic_id(db)
    await content.upsert_block(
        db, topic_id=topic_id, lang="ru", position=0, body_md="draft", is_published=False
    )

    assert await content.get_topic_blocks(db, "work_terms", "ru") == []
    assert len(await content.get_topic_blocks(db, "work_terms", "ru", published_only=False)) == 1


async def test_a_missing_language_falls_back_to_the_practice_default(
    db: AsyncSession, practice: Practice
) -> None:
    """A client should see the page in the wrong language rather than an empty
    page (DESIGN.md §11)."""
    topic_id = await _topic_id(db)
    await content.upsert_block(
        db, topic_id=topic_id, lang=practice.default_language, position=0, body_md="по-русски"
    )

    blocks = await content.get_topic_blocks(db, "work_terms", "hy")
    assert [b.body_md for b in blocks] == ["по-русски"]


# --- Validation at save time ------------------------------------------------


async def test_saving_a_table_returns_a_validation_error(db: AsyncSession) -> None:
    """M3 acceptance. Caught in front of the therapist, not at send time in
    front of a client."""
    topic_id = await _topic_id(db)
    with pytest.raises(MarkdownNotAllowed, match="table"):
        await content.upsert_block(
            db,
            topic_id=topic_id,
            lang="ru",
            position=0,
            body_md="| a | b |\n|---|---|\n| 1 | 2 |",
        )


async def test_a_rejected_block_is_not_written(db: AsyncSession) -> None:
    topic_id = await _topic_id(db)
    with pytest.raises(MarkdownNotAllowed):
        await content.upsert_block(
            db, topic_id=topic_id, lang="ru", position=0, body_md="<div>raw</div>"
        )
    assert await content.get_topic_blocks(db, "work_terms", "ru", published_only=False) == []


# --- Revisions (§6.7) -------------------------------------------------------


async def test_each_write_stores_the_previous_body_and_bumps_the_version(
    db: AsyncSession,
) -> None:
    topic_id = await _topic_id(db)
    block = await content.upsert_block(
        db, topic_id=topic_id, lang="ru", position=0, body_md="first"
    )
    assert block.version == 1

    block = await content.upsert_block(
        db, topic_id=topic_id, lang="ru", position=0, body_md="second"
    )
    assert block.version == 2
    assert block.body_md == "second"

    revisions = await content.list_revisions(db, block.id)
    assert [(r.version, r.body_md) for r in revisions] == [(1, "first")]


async def test_creating_a_block_writes_no_revision(db: AsyncSession) -> None:
    topic_id = await _topic_id(db)
    block = await content.upsert_block(db, topic_id=topic_id, lang="ru", position=0, body_md="only")
    assert await content.list_revisions(db, block.id) == []


async def test_a_block_keeps_only_the_most_recent_twenty_revisions(
    db: AsyncSession,
) -> None:
    topic_id = await _topic_id(db)
    block = await content.upsert_block(db, topic_id=topic_id, lang="ru", position=0, body_md="v1")
    for n in range(2, 30):
        block = await content.upsert_block(
            db, topic_id=topic_id, lang="ru", position=0, body_md=f"v{n}"
        )

    kept = await content.list_revisions(db, block.id)
    assert len(kept) == MAX_REVISIONS
    # The oldest are the ones dropped.
    assert kept[0].version > kept[-1].version
    assert kept[-1].version == block.version - MAX_REVISIONS


async def test_a_block_can_be_rolled_back(db: AsyncSession) -> None:
    """A paste that breaks a page at 23:00 has to be undoable (DESIGN.md §10.2)."""
    topic_id = await _topic_id(db)
    await content.upsert_block(db, topic_id=topic_id, lang="ru", position=0, body_md="good")
    block = await content.upsert_block(
        db, topic_id=topic_id, lang="ru", position=0, body_md="broken paste"
    )

    restored = await content.restore_revision(db, block.id, version=1)
    assert restored.body_md == "good"
    # The rollback is itself a write, so the bad body survives as a revision --
    # rolling back a rollback has to be possible.
    assert any(r.body_md == "broken paste" for r in await content.list_revisions(db, block.id))


async def test_restoring_an_unknown_revision_raises(db: AsyncSession) -> None:
    topic_id = await _topic_id(db)
    block = await content.upsert_block(db, topic_id=topic_id, lang="ru", position=0, body_md="x")
    with pytest.raises(NotFound):
        await content.restore_revision(db, block.id, version=99)


async def test_deleting_a_block_removes_its_revisions(db: AsyncSession) -> None:
    topic_id = await _topic_id(db)
    await content.upsert_block(db, topic_id=topic_id, lang="ru", position=0, body_md="a")
    block = await content.upsert_block(db, topic_id=topic_id, lang="ru", position=0, body_md="b")
    block_id = block.id

    await content.delete_block(db, block_id)

    remaining = (
        await db.execute(
            select(func.count())
            .select_from(ContentBlockRevision)
            .where(ContentBlockRevision.block_id == block_id)
        )
    ).scalar_one()
    assert remaining == 0


async def test_topic_titles_are_not_a_column(db: AsyncSession) -> None:
    """§20: they come from the translation key `content.topic.<code>.title`."""
    columns = {c.name for c in ContentTopic.__table__.columns}
    assert "title" not in columns
    assert "name" not in columns
