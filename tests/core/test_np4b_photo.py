"""NP4b: her photo and the languages line (IMPLEMENTATION.md §6.1, §12.1, §12.2, §17).

The photo is checked by its bytes, re-encoded bounded and stripped of metadata, and stored whole in
one row; nothing is invented when she has none.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.services import photo
from app.core.services.settings import LANGUAGES_MAX, get_practice, update_settings


def _image(fmt: str, size: tuple[int, int] = (1600, 1200), mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, (200, 120, 90) if mode == "RGB" else (200, 120, 90, 128)).save(out, fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_each_allowed_type_is_accepted_and_resized_to_a_bounded_jpeg(fmt: str) -> None:
    stored = photo.process(_image(fmt))
    assert photo.sniff(stored) == "jpeg"
    with Image.open(io.BytesIO(stored)) as result:
        assert max(result.size) == photo.MAX_SIDE
        assert result.size == (640, 480)  # the aspect ratio is kept


def test_a_small_picture_is_not_enlarged() -> None:
    with Image.open(io.BytesIO(photo.process(_image("PNG", (200, 100))))) as result:
        assert result.size == (200, 100)


def test_a_transparent_png_is_flattened_onto_white() -> None:
    stored = photo.process(_image("PNG", (50, 50), mode="RGBA"))
    with Image.open(io.BytesIO(stored)) as result:
        assert result.mode == "RGB"


def test_the_type_is_read_from_the_bytes_not_from_a_name() -> None:
    with pytest.raises(photo.PhotoRejected, match="JPEG, PNG or WebP"):
        photo.process(b"GIF89a" + b"\x00" * 100)
    with pytest.raises(photo.PhotoRejected, match="JPEG, PNG or WebP"):
        photo.process(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    with pytest.raises(photo.PhotoRejected, match="JPEG, PNG or WebP"):
        photo.process(b"%PDF-1.4 photo.jpg")


def test_a_file_with_the_right_start_and_no_picture_is_refused() -> None:
    with pytest.raises(photo.PhotoRejected, match="could not be read"):
        photo.process(b"\xff\xd8\xff" + b"not really a jpeg" * 20)


def test_an_empty_and_an_oversized_upload_are_refused() -> None:
    with pytest.raises(photo.PhotoRejected, match="No file"):
        photo.process(b"")
    with pytest.raises(photo.PhotoRejected, match="larger than 6 MB"):
        photo.process(b"\xff\xd8\xff" + b"\x00" * photo.MAX_UPLOAD_BYTES)


def test_metadata_does_not_survive() -> None:
    source = io.BytesIO()
    exif = Image.Exif()
    exif[0x010F] = "SomePhoneMaker"
    Image.new("RGB", (64, 64), (1, 2, 3)).save(source, "JPEG", exif=exif)
    assert b"SomePhoneMaker" in source.getvalue()
    assert b"SomePhoneMaker" not in photo.process(source.getvalue())


@pytest.mark.asyncio
async def test_setting_a_photo_replaces_the_one_before_and_removing_it_leaves_none(
    db: AsyncSession,
) -> None:
    assert await photo.get_photo(db) is None
    assert await photo.photo_version(db) is None
    first = await photo.set_photo(db, _image("PNG", (100, 100)))
    second = await photo.set_photo(db, _image("JPEG", (120, 80)))
    assert second.practice_id == first.practice_id
    assert (await photo.photo_version(db)) == second.etag[:12]
    assert await photo.delete_photo(db) is True
    assert await photo.get_photo(db) is None
    assert await photo.delete_photo(db) is False


@pytest.mark.asyncio
async def test_languages_is_a_setting_that_is_tidied_and_can_be_cleared(db: AsyncSession) -> None:
    practice = await update_settings(db, languages="  Русский,   English ")
    assert practice.languages == "Русский, English"
    assert (await update_settings(db, languages="")).languages is None
    with pytest.raises(ValueError, match="languages"):
        await update_settings(db, languages="x" * (LANGUAGES_MAX + 1))
    assert (await get_practice(db)).languages is None
