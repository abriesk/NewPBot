"""Her photo (IMPLEMENTATION.md §6.1, §12.1, §12.2; NP4b).

Uploaded in the admin settings, checked by what the bytes are (magic numbers, never the file name
or the declared type), decoded and re-encoded as a bounded JPEG, and stored in the database. The
re-encode is the point: it drops EXIF -- a phone photo carries the place it was taken -- and it
bounds what the site ever serves, so a 12 MB original becomes a few tens of kilobytes.

Nothing here touches the file system, so there is no path to traverse: the upload is read from the
request body and the result goes to one row.
"""

from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import PracticePhoto
from app.core.policies import now_utc
from app.core.services.settings import get_practice

#: What the admin may upload. The route reads one byte past this, so a larger body is refused
#: without being buffered whole (the web layer's body cap is a little above it).
MAX_UPLOAD_BYTES = 6 * 1024 * 1024

#: The longest side kept. The front page shows the photo at most ~280 CSS px wide; twice that is
#: enough for a high-density phone, and nothing larger is ever sent.
MAX_SIDE = 640

#: A guard against a small file that decodes to a huge canvas (a decompression bomb).
MAX_PIXELS = 36_000_000

JPEG_QUALITY = 82
SERVED_TYPE = "image/jpeg"


class PhotoRejected(ValueError):
    """The upload was refused; `str()` is a sentence the admin page shows."""


def sniff(raw: bytes) -> str | None:
    """`jpeg`, `png` or `webp` by magic bytes, else `None`."""
    if raw[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def process(raw: bytes) -> bytes:
    """The bytes to store: a JPEG no larger than `MAX_SIDE`, without metadata."""
    if not raw:
        raise PhotoRejected("No file was chosen.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise PhotoRejected("The file is larger than 6 MB.")
    kind = sniff(raw)
    if kind is None:
        raise PhotoRejected("Only JPEG, PNG or WebP photos can be used.")

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            if (opened.format or "").lower() != kind:
                raise PhotoRejected("The file is not the kind of image it says it is.")
            width, height = opened.size
            if width < 1 or height < 1 or width * height > MAX_PIXELS:
                raise PhotoRejected("The picture is too large to use.")
            image = ImageOps.exif_transpose(opened)
            image.load()
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                image = flat
            else:
                image = image.convert("RGB")
            image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    except PhotoRejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PhotoRejected("The file could not be read as a picture.") from exc
    return out.getvalue()


async def get_photo(session: AsyncSession) -> PracticePhoto | None:
    practice = await get_practice(session)
    return (
        await session.execute(select(PracticePhoto).where(PracticePhoto.practice_id == practice.id))
    ).scalar_one_or_none()


async def photo_version(session: AsyncSession) -> str | None:
    """A short token naming the stored bytes (for the cache-busting `?v=`), or `None`."""
    practice = await get_practice(session)
    etag = (
        await session.execute(
            select(PracticePhoto.etag).where(PracticePhoto.practice_id == practice.id)
        )
    ).scalar_one_or_none()
    return etag[:12] if etag else None


async def set_photo(session: AsyncSession, raw: bytes) -> PracticePhoto:
    """Validate, resize and store; replaces the one photo she has."""
    data = process(raw)
    practice = await get_practice(session)
    etag = hashlib.sha256(data).hexdigest()
    row = await get_photo(session)
    if row is None:
        row = PracticePhoto(practice_id=practice.id, content_type=SERVED_TYPE, data=data, etag=etag)
        session.add(row)
    else:
        row.content_type = SERVED_TYPE
        row.data = data
        row.etag = etag
        row.updated_at = now_utc()
    await session.flush()
    return row


async def delete_photo(session: AsyncSession) -> bool:
    row = await get_photo(session)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
