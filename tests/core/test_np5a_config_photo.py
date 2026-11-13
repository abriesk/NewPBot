"""NP5a: her photo joins the config export and import; migration 0011 for the privacy text.

Enforces IMPLEMENTATION.md §16.7 (the config file) and §12.2 (the admin settings).
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import io
from pathlib import Path

import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Practice, PracticePhoto, Translation
from app.core.services import photo as photo_service
from app.core.services.config_io import ConfigInvalid, export_config, import_config

MIGRATION = Path("alembic/versions/0011_privacy_contact_copy.py")


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 30), (180, 140, 100)).save(out, "JPEG")
    return out.getvalue()


async def _no_photo(db: AsyncSession, practice: Practice) -> None:
    await db.execute(sa.delete(PracticePhoto).where(PracticePhoto.practice_id == practice.id))
    await db.flush()


async def test_the_photo_is_in_the_export_and_round_trips(
    db: AsyncSession, practice: Practice
) -> None:
    await _no_photo(db, practice)
    assert "practice_photo" not in await export_config(db)

    await photo_service.set_photo(db, _jpeg())
    payload = await export_config(db)
    stored = (await photo_service.get_photo(db)).data  # type: ignore[union-attr]
    assert base64.b64decode(payload["practice_photo"]["data_b64"]) == stored

    # Same bytes in: nothing to do. Gone locally, file has it: created.
    assert (await import_config(db, payload)).section("practice_photo").unchanged == 1
    await _no_photo(db, practice)
    assert (await import_config(db, payload)).section("practice_photo").created == 1
    assert (await photo_service.get_photo(db)).data == stored  # type: ignore[union-attr]


async def test_a_photo_that_is_not_a_picture_aborts_the_import(
    db: AsyncSession, practice: Practice
) -> None:
    await _no_photo(db, practice)
    payload = await export_config(db)
    payload["practice_photo"] = {"data_b64": base64.b64encode(b"not a picture").decode()}
    with pytest.raises(ConfigInvalid):
        await import_config(db, payload)
    payload["practice_photo"] = {"data_b64": "%%%"}
    with pytest.raises(ConfigInvalid):
        await import_config(db, payload)
    assert await photo_service.get_photo(db) is None


def _migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("mig0011", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0011_is_frozen_and_chains_after_0010() -> None:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    imported = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + [
        n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    ]
    assert [m for m in imported if m == "app" or m.startswith("app.")] == []
    mod = _migration()
    assert mod.down_revision == "0010_her_content"


def test_the_new_privacy_default_matches_the_seed_and_names_the_contact_field() -> None:
    from app.seed import LANGUAGES, load_locale_catalogue

    catalogue = load_locale_catalogue()
    changes = {c[0]: c for c in _migration().CHANGES}
    assert set(changes) == set(LANGUAGES)
    for lang, (_, key, old, new) in changes.items():
        assert key == "privacy.default.2"
        assert catalogue[lang][key] == new
        assert new != old[0]
    assert "reach you" in changes["en"][3]


async def test_migration_0011_rewrites_the_old_text_and_spares_an_edit(
    db: AsyncSession, practice: Practice
) -> None:
    mod = _migration()
    en = next(c for c in mod.CHANGES if c[0] == "en")
    for lang, value in (("en", en[2][0]), ("ru", "her own wording")):
        await db.execute(
            sa.update(Translation)
            .where(
                Translation.practice_id == practice.id,
                Translation.lang == lang,
                Translation.key == "privacy.default.2",
            )
            .values(value=value)
        )

    def go(sync_session: sa.orm.Session) -> tuple[int, int]:
        return mod.apply_default_changes(  # type: ignore[no-any-return]
            sync_session.connection(), mod.CHANGES, mod.ADDITIONS
        )

    assert (await db.run_sync(go))[0] == 1
    value = (
        await db.execute(
            sa.select(Translation.value).where(
                Translation.practice_id == practice.id,
                Translation.lang == "ru",
                Translation.key == "privacy.default.2",
            )
        )
    ).scalar_one()
    assert value == "her own wording"
    assert await db.run_sync(go) == (0, 0)
