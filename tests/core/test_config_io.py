"""Portable configuration (IMPLEMENTATION.md §16.7, §18, M10 acceptance).

Two properties carry most of the weight here. A round trip must be a genuine
no-op -- twenty imports of the same file cannot be allowed to erase a block's
revision history -- and a rejected file must leave the database exactly as it
was, whatever had already been applied when the bad section was reached.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import BookingMode
from app.core.models import ContentBlock, ContentBlockRevision, ContentTopic, SessionType
from app.core.services.config_io import (
    FORMAT,
    VERSION,
    ConfigInvalid,
    dump_config,
    export_config,
    import_config,
    load_config,
)
from app.core.services.content import get_topic, upsert_block
from app.core.services.settings import MUTABLE_FIELDS, get_practice
from app.core.services.translations import set_text

TOPIC = "work_terms"


def _without_timestamp(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "exported_at"}


async def _block(db: AsyncSession, lang: str, position: int) -> ContentBlock | None:
    topic = await get_topic(db, TOPIC)
    return (
        await db.execute(
            select(ContentBlock).where(
                ContentBlock.topic_id == topic.id,
                ContentBlock.lang == lang,
                ContentBlock.position == position,
            )
        )
    ).scalar_one_or_none()


def _find_topic(payload: dict[str, Any], code: str) -> dict[str, Any]:
    return next(topic for topic in payload["content"] if topic["code"] == code)


# --- Export -----------------------------------------------------------------


async def test_the_export_is_exactly_the_admin_editable_configuration(db: AsyncSession) -> None:
    payload = await export_config(db)

    assert payload["format"] == FORMAT
    assert payload["version"] == VERSION
    assert set(payload) == {
        "format",
        "version",
        "exported_at",
        "practice",
        "session_types",
        "timezone_options",
        "content",
        "translations",
    }
    # §16.7: exactly MUTABLE_FIELDS, so adding a settings field does not
    # silently widen what an import can rewrite.
    assert set(payload["practice"]) == set(MUTABLE_FIELDS)


async def test_the_export_carries_no_client_data(db: AsyncSession) -> None:
    """DESIGN.md §21.1 -- the reason this file exists beside pg_dump at all."""
    serialised = dump_config(await export_config(db)).lower()

    for forbidden in (
        "problem_text",
        "booking_request",
        "client_id",
        "waitlist_entry",
        "outbox",
        "audit_log",
        "password_hash",
    ):
        assert forbidden not in serialised, f"{forbidden!r} leaked into the config file"


async def test_the_export_holds_no_database_ids(db: AsyncSession) -> None:
    payload = await export_config(db)

    for row in payload["session_types"] + payload["timezone_options"]:
        assert "id" not in row and "practice_id" not in row
    for topic in payload["content"]:
        assert "id" not in topic and "practice_id" not in topic
        for block in topic["blocks"]:
            assert "id" not in block and "topic_id" not in block


async def test_the_export_is_stable_and_readable(db: AsyncSession) -> None:
    text = dump_config(await export_config(db))

    assert text.endswith("\n")
    assert json.loads(text)["format"] == FORMAT
    # Sorted keys and an indent: two exports have to diff usefully.
    assert '\n  "format"' in text


# --- Round trip -------------------------------------------------------------


async def test_reimporting_an_export_changes_nothing(db: AsyncSession) -> None:
    before = await export_config(db)

    report = await import_config(db, before, apply=True)

    assert report.applied
    assert not report.changed, report.as_meta()
    after = await export_config(db)
    assert _without_timestamp(after) == _without_timestamp(before)


async def test_an_unchanged_block_writes_no_revision(db: AsyncSession) -> None:
    """§16.7. Otherwise twenty imports would push the real history off the end
    of the twenty MAX_REVISIONS a block keeps."""
    topic = await get_topic(db, TOPIC)
    await upsert_block(db, topic_id=topic.id, lang="ru", position=0, body_md="Условия работы.")

    payload = await export_config(db)
    block = await _block(db, "ru", 0)
    assert block is not None
    version_before = block.version

    await import_config(db, payload, apply=True)

    await db.refresh(block)
    assert block.version == version_before
    revisions = (
        await db.execute(
            select(ContentBlockRevision).where(ContentBlockRevision.block_id == block.id)
        )
    ).scalars().all()
    assert [r.version for r in revisions] == []


async def test_a_changed_block_keeps_its_previous_body_as_a_revision(db: AsyncSession) -> None:
    """The undo for a bad import is the revisions page that already exists."""
    topic = await get_topic(db, TOPIC)
    await upsert_block(db, topic_id=topic.id, lang="ru", position=0, body_md="Первая версия.")

    payload = await export_config(db)
    _find_topic(payload, TOPIC)["blocks"] = [
        {"lang": "ru", "position": 0, "kind": "text", "body_md": "Вторая версия.",
         "link_url": None, "is_published": True}
    ]

    report = await import_config(db, payload, apply=True)

    assert report.sections["content_blocks"].updated == 1
    block = await _block(db, "ru", 0)
    assert block is not None and block.body_md == "Вторая версия."
    bodies = (
        (
            await db.execute(
                select(ContentBlockRevision.body_md).where(
                    ContentBlockRevision.block_id == block.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert "Первая версия." in bodies


# --- Merge semantics --------------------------------------------------------


async def test_import_applies_what_the_file_names(db: AsyncSession) -> None:
    payload = await export_config(db)
    payload["practice"]["slot_hold_minutes"] = 42
    payload["practice"]["booking_mode"] = BookingMode.negotiation.value
    payload["session_types"].append(
        {"code": "supervision", "duration_min": 90, "price_amount_minor": None,
         "price_currency": None, "price_display_override": None,
         "is_active": True, "sort_order": 7}
    )

    report = await import_config(db, payload, apply=True)

    assert report.sections["practice"].updated == 1
    assert report.sections["session_types"].created == 1
    practice = await get_practice(db)
    assert practice.slot_hold_minutes == 42
    assert practice.booking_mode is BookingMode.negotiation
    added = (
        await db.execute(select(SessionType).where(SessionType.code == "supervision"))
    ).scalar_one()
    assert added.duration_min == 90


async def test_import_never_deletes_what_the_file_leaves_out(db: AsyncSession) -> None:
    """DESIGN.md §21.3: merge fails in the recoverable direction."""
    payload = await export_config(db)
    codes = {row["code"] for row in payload["session_types"]}
    payload["session_types"] = []
    payload["content"] = []
    payload["translations"] = {}

    await import_config(db, payload, apply=True)

    surviving = set(
        (await db.execute(select(SessionType.code))).scalars().all()
    )
    assert codes <= surviving
    assert (await db.execute(select(ContentTopic.code))).scalars().all()


async def test_a_new_topic_arrives_with_its_blocks(db: AsyncSession) -> None:
    payload = await export_config(db)
    payload["content"].append(
        {
            "code": "supervision_terms",
            "sort_order": 9,
            "show_in_menu": True,
            "is_active": True,
            "blocks": [
                {"lang": "ru", "position": 0, "kind": "text", "body_md": "Текст.",
                 "link_url": None, "is_published": True},
                {"lang": "hy", "position": 0, "kind": "text", "body_md": "Տեքստ։",
                 "link_url": None, "is_published": True},
            ],
        }
    )

    report = await import_config(db, payload, apply=True)

    assert report.sections["content_topics"].created == 1
    assert report.sections["content_blocks"].created == 2
    topic = await get_topic(db, "supervision_terms")
    assert topic.sort_order == 9


async def test_translations_are_matched_by_language_and_key(db: AsyncSession) -> None:
    await set_text(db, "ru", "common.yes", "Да")
    payload = await export_config(db)
    payload["translations"]["ru"]["common.yes"] = "Ага"

    report = await import_config(db, payload, apply=True)

    assert report.sections["translations"].updated == 1
    after = await export_config(db)
    assert after["translations"]["ru"]["common.yes"] == "Ага"


async def test_an_unknown_translation_key_is_skipped_not_written(db: AsyncSession) -> None:
    """A key no renderer reads is dead weight, and usually means the file came
    from a newer build (§16.7)."""
    payload = await export_config(db)
    # Scoped to the key this test injects. The suite shares one database, and
    # any `translation` row whose key has since left locales/*.yaml is skipped
    # for exactly this reason -- a database-wide count grows with every one of
    # them. Import merges and never deletes (DESIGN.md §21.3), so naming one
    # language here leaves the rest of the catalogue alone.
    payload["translations"] = {"ru": {}}
    payload["translations"]["ru"]["feature.from.the.future"] = "Что-то"

    report = await import_config(db, payload, apply=True)

    assert report.sections["translations"].skipped == 1
    assert any("feature.from.the.future" in warning for warning in report.warnings)
    after = await export_config(db)
    assert "feature.from.the.future" not in after["translations"].get("ru", {})


async def test_the_admin_namespace_is_not_imported_into_another_language(
    db: AsyncSession,
) -> None:
    """DESIGN.md §11: the admin surface is English only."""
    payload = await export_config(db)
    # Scoped for the reason above: only the row this test names counts towards
    # `skipped`.
    payload["translations"] = {}
    payload["translations"]["hy"] = {"admin.nav.requests": "Հարցումներ"}

    report = await import_config(db, payload, apply=True)

    assert report.sections["translations"].skipped == 1
    after = await export_config(db)
    assert "admin.nav.requests" not in after["translations"].get("hy", {})


# --- Preview ----------------------------------------------------------------


async def test_a_preview_reports_the_counts_and_writes_nothing(db: AsyncSession) -> None:
    payload = await export_config(db)
    payload["practice"]["slot_hold_minutes"] = 99

    preview = await import_config(db, payload, apply=False)

    assert not preview.applied
    assert preview.sections["practice"].updated == 1
    practice = await get_practice(db)
    assert practice.slot_hold_minutes != 99

    applied = await import_config(db, payload, apply=True)
    assert applied.as_meta()["sections"] == preview.as_meta()["sections"]
    practice = await get_practice(db)
    assert practice.slot_hold_minutes == 99


# --- Refusals ---------------------------------------------------------------


async def test_a_foreign_file_is_refused(db: AsyncSession) -> None:
    with pytest.raises(ConfigInvalid):
        await import_config(db, {"format": "something.else", "version": 1})


async def test_an_unknown_version_is_refused(db: AsyncSession) -> None:
    payload = await export_config(db)
    payload["version"] = VERSION + 1

    with pytest.raises(ConfigInvalid, match="unsupported format version"):
        await import_config(db, payload)


async def test_an_unknown_section_is_refused_rather_than_ignored(db: AsyncSession) -> None:
    """§16.7: a file with an `admin_user` section must not look honoured."""
    payload = await export_config(db)
    payload["admin_user"] = [{"username": "intruder", "password_hash": "x"}]

    with pytest.raises(ConfigInvalid, match="admin_user"):
        await import_config(db, payload)


async def test_amharic_cannot_enter_through_an_import(db: AsyncSession) -> None:
    """Hard rule 5. `am` is Amharic; Armenian is `hy`."""
    payload = await export_config(db)
    payload["translations"]["am"] = {"common.yes": "አዎ"}

    with pytest.raises(ConfigInvalid, match="unknown language"):
        await import_config(db, payload)


async def test_a_block_in_an_unknown_language_is_refused(db: AsyncSession) -> None:
    payload = await export_config(db)
    _find_topic(payload, TOPIC)["blocks"] = [
        {"lang": "am", "position": 0, "kind": "text", "body_md": "x",
         "link_url": None, "is_published": True}
    ]

    with pytest.raises(ConfigInvalid, match="unknown language"):
        await import_config(db, payload)


async def test_a_setting_that_is_not_settable_is_refused(db: AsyncSession) -> None:
    payload = await export_config(db)
    payload["practice"]["retention_months_typo"] = 6

    with pytest.raises(ConfigInvalid, match="not settable"):
        await import_config(db, payload)


async def test_an_unknown_enum_value_is_refused(db: AsyncSession) -> None:
    payload = await export_config(db)
    payload["practice"]["booking_mode"] = "whenever"

    with pytest.raises(ConfigInvalid, match="booking_mode"):
        await import_config(db, payload)


async def test_markdown_outside_the_subset_is_refused(db: AsyncSession) -> None:
    """The same validation the editor applies at save time (§11.1)."""
    payload = await export_config(db)
    _find_topic(payload, TOPIC)["blocks"] = [
        {"lang": "ru", "position": 0, "kind": "text",
         "body_md": "| a | b |\n|---|---|\n| 1 | 2 |",
         "link_url": None, "is_published": True}
    ]

    with pytest.raises(ConfigInvalid, match="table"):
        await import_config(db, payload)


async def test_two_blocks_claiming_one_position_are_refused(db: AsyncSession) -> None:
    payload = await export_config(db)
    _find_topic(payload, TOPIC)["blocks"] = [
        {"lang": "ru", "position": 0, "kind": "text", "body_md": "Один.",
         "link_url": None, "is_published": True},
        {"lang": "ru", "position": 0, "kind": "text", "body_md": "Два.",
         "link_url": None, "is_published": True},
    ]

    with pytest.raises(ConfigInvalid, match="two blocks"):
        await import_config(db, payload)


async def test_a_duplicate_json_key_is_refused_while_both_are_still_visible() -> None:
    """`json.loads` silently keeps the last of two identical keys, which would
    import half of what the file appears to say (§16.7)."""
    raw = '{"format": "psychobooking.config", "version": 1, "version": 2}'

    with pytest.raises(ConfigInvalid, match="duplicate key"):
        load_config(raw)


async def test_a_file_that_is_not_json_is_refused() -> None:
    with pytest.raises(ConfigInvalid, match="not valid JSON"):
        load_config(b"# psychobooking config\nnope\n")


async def test_a_rejected_import_leaves_the_database_untouched(db: AsyncSession) -> None:
    """The bad section is reached *after* the good ones, so this only passes if
    the whole import is one transaction (§16.7)."""
    practice = await get_practice(db)
    hold_before = practice.slot_hold_minutes

    payload = await export_config(db)
    payload["practice"]["slot_hold_minutes"] = hold_before + 5
    payload["translations"] = {"am": {"common.yes": "አዎ"}}

    with pytest.raises(ConfigInvalid):
        await import_config(db, payload, apply=True)

    practice = await get_practice(db)
    assert practice.slot_hold_minutes == hold_before
