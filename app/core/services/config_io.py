"""Portable configuration: export and import (IMPLEMENTATION.md §16.7).

Everything the therapist typed into the admin UI -- settings, session types,
timezone options, topics, blocks, translations -- as one JSON file, and no
client data at all. That absence is the point: a `pg_dump` carries problem text
and cannot be handed to whoever is rebuilding the install, where this file can
(DESIGN.md §21.1).

Rows match on **natural keys**, never database ids: a topic by `code`, a
translation by `(lang, key)`, a block by `(topic, lang, position)`. A file
exported before three migrations therefore still imports afterwards, and a
column added in between simply takes its default.

Import merges and never deletes (DESIGN.md §21.3). Rows absent from the file
are left alone, because files get kept and reused: replace-semantics would let
a six-month-old file silently delete every topic added since. Merge fails in
the recoverable direction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import BookingMode, ContentBlockKind
from app.core.errors import DomainError
from app.core.models import (
    ContentBlock,
    ContentTopic,
    SessionType,
    TimezoneOption,
    Translation,
)
from app.core.policies import now_utc
from app.core.services.content import upsert_block
from app.core.services.settings import MUTABLE_FIELDS, get_practice, update_settings
from app.core.services.translations import invalidate_cache, set_text

#: What the envelope must say. A file that does not say it is not ours.
FORMAT = "psychobooking.config"

#: Versions this build can read. A file from a newer build is refused whole
#: rather than half-imported -- §16.7 requires a format change to arrive with
#: an upgrade path, not with a best effort.
VERSION = 1
KNOWN_VERSIONS = frozenset({1})

#: Top-level keys allowed in the file. Anything else is a refusal, not a
#: silent skip: that is what stops a `clients` or `admin_user` section from
#: looking like it was honoured (§16.7).
SECTIONS = ("practice", "session_types", "timezone_options", "content", "translations")

_ENVELOPE = ("format", "version", "exported_at")

#: Columns exported per row, in file order. Ids, `practice_id`, and timestamps
#: are never written -- they are exactly the fields that do not survive the
#: trip to another installation.
_SESSION_TYPE_FIELDS = (
    "code",
    "duration_min",
    "price_amount_minor",
    "price_currency",
    "price_display_override",
    "is_active",
    "sort_order",
)
_TIMEZONE_FIELDS = ("iana_name", "display_name", "sort_order", "is_active")
_TOPIC_FIELDS = ("code", "sort_order", "show_in_menu", "is_active")
_BLOCK_FIELDS = ("lang", "position", "kind", "body_md", "link_url", "is_published")

#: The admin namespace is English by design (DESIGN.md §11), so an `admin.` key
#: offered for ru or hy is skipped rather than written -- nothing would ever
#: read it.
ADMIN_PREFIX = "admin."


class ConfigInvalid(DomainError):
    """The file cannot be imported, and nothing has been written.

    One exception for every rejection in §16.7. The caller shows `str(exc)` --
    the therapist is the person who will fix the file, so the message names the
    section and the offending value.
    """


@dataclass
class SectionReport:
    """What happened to one section. `skipped` means recognised and not
    written -- an unknown translation key -- never "failed"; a failure aborts
    the whole import."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
        }


@dataclass
class ImportReport:
    applied: bool = False
    sections: dict[str, SectionReport] = field(default_factory=dict)
    #: Human-readable notes for the admin page. Never fatal, never content.
    warnings: list[str] = field(default_factory=list)

    def section(self, name: str) -> SectionReport:
        return self.sections.setdefault(name, SectionReport())

    def as_meta(self) -> dict[str, Any]:
        """Audit `meta` (§16.7): counts and section names only.

        No bodies, no translation values -- hard rule 8 is about problem text,
        but the audit log is not a copy of the content either.
        """
        return {
            "applied": self.applied,
            "sections": {name: report.as_dict() for name, report in sorted(self.sections.items())},
            "warnings": len(self.warnings),
        }

    @property
    def changed(self) -> bool:
        return any(r.created or r.updated for r in self.sections.values())


# --- Export -----------------------------------------------------------------


async def export_config(session: AsyncSession) -> dict[str, Any]:
    """The whole admin-editable configuration, ready for `json.dumps`."""
    practice = await get_practice(session)

    session_types = (
        (
            await session.execute(
                select(SessionType).order_by(SessionType.sort_order, SessionType.code)
            )
        )
        .scalars()
        .all()
    )
    timezones = (
        (
            await session.execute(
                select(TimezoneOption).order_by(TimezoneOption.sort_order, TimezoneOption.iana_name)
            )
        )
        .scalars()
        .all()
    )
    topics = (
        (
            await session.execute(
                select(ContentTopic).order_by(ContentTopic.sort_order, ContentTopic.code)
            )
        )
        .scalars()
        .all()
    )
    blocks = (
        (
            await session.execute(
                select(ContentBlock).order_by(ContentBlock.lang, ContentBlock.position)
            )
        )
        .scalars()
        .all()
    )
    translations = (
        await session.execute(
            select(Translation.lang, Translation.key, Translation.value).order_by(
                Translation.lang, Translation.key
            )
        )
    ).all()

    by_topic: dict[int, list[dict[str, Any]]] = {}
    for block in blocks:
        by_topic.setdefault(block.topic_id, []).append(_row(block, _BLOCK_FIELDS))

    catalogue: dict[str, dict[str, str]] = {}
    for lang, key, value in translations:
        catalogue.setdefault(lang, {})[key] = value

    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": now_utc().isoformat(),
        "practice": {
            field_: _scalar(getattr(practice, field_)) for field_ in sorted(MUTABLE_FIELDS)
        },
        "session_types": [_row(row, _SESSION_TYPE_FIELDS) for row in session_types],
        "timezone_options": [_row(row, _TIMEZONE_FIELDS) for row in timezones],
        "content": [
            {**_row(topic, _TOPIC_FIELDS), "blocks": by_topic.get(topic.id, [])} for topic in topics
        ],
        "translations": {lang: catalogue.get(lang, {}) for lang in sorted(catalogue)},
    }


def dump_config(payload: dict[str, Any]) -> str:
    """Serialise for download.

    Sorted keys and two-space indent, so two exports diff usefully in a text
    editor -- the file is meant to be read by a person at least once.
    """
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _row(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: _scalar(getattr(obj, name)) for name in fields}


def _scalar(value: Any) -> Any:
    """Enums become their value; arrays become plain lists. Everything else in
    the exported set is already JSON."""
    if isinstance(value, BookingMode | ContentBlockKind):
        return value.value
    if isinstance(value, list):
        return list(value)
    return value


# --- Parsing ----------------------------------------------------------------


def load_config(raw: bytes | str) -> dict[str, Any]:
    """Parse an uploaded file, refusing duplicate keys.

    `json.loads` keeps the last of two identical keys silently, which would
    make a file with two `"ru"` translation blocks import half of what it
    appears to say. §16.7 requires the duplicate to be a refusal, so it is
    caught here, at the only place that can still see both.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigInvalid("the file is not UTF-8 text") from exc

    try:
        payload = json.loads(raw, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as exc:
        raise ConfigInvalid(f"not valid JSON: {exc.msg} (line {exc.lineno})") from exc

    if not isinstance(payload, dict):
        raise ConfigInvalid("the file must contain a JSON object")
    return payload


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ConfigInvalid(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


# --- Import -----------------------------------------------------------------


async def import_config(
    session: AsyncSession, payload: dict[str, Any], *, apply: bool = True
) -> ImportReport:
    """Merge a config file into this installation.

    With `apply=False` the identical code runs inside a savepoint that is
    rolled back before returning: a preview that runs different code from the
    apply is a preview of nothing (§12.2).

    Every rejection in §16.7 raises `ConfigInvalid` before or during the write,
    and the caller's transaction (or the savepoint) leaves nothing behind.
    """
    _check_envelope(payload)

    # A savepoint either way, so "nothing partial is written" does not depend on
    # what the caller does with the exception: an adapter that catches
    # ConfigInvalid to render it on a page would otherwise commit the half of
    # the file that had already applied.
    savepoint = await session.begin_nested()
    try:
        report = await _apply(session, payload, applied=apply)
    except Exception:
        await savepoint.rollback()
        raise

    if apply:
        await savepoint.commit()
    else:
        # The report is built before this runs: rolling back discards the rows,
        # not the counts.
        await savepoint.rollback()
    return report


async def _apply(session: AsyncSession, payload: dict[str, Any], *, applied: bool) -> ImportReport:
    report = ImportReport(applied=applied)

    await _import_practice(session, payload.get("practice"), report)
    await _import_session_types(session, payload.get("session_types"), report)
    await _import_timezones(session, payload.get("timezone_options"), report)
    await _import_content(session, payload.get("content"), report)
    await _import_translations(session, payload.get("translations"), report)

    await session.flush()
    return report


def _check_envelope(payload: dict[str, Any]) -> None:
    if payload.get("format") != FORMAT:
        raise ConfigInvalid(f"not a {FORMAT} file (found format={payload.get('format')!r})")
    version = payload.get("version")
    if version not in KNOWN_VERSIONS:
        raise ConfigInvalid(
            f"unsupported format version {version!r}; this build reads {sorted(KNOWN_VERSIONS)}"
        )

    unknown = sorted(set(payload) - set(SECTIONS) - set(_ENVELOPE))
    if unknown:
        # Naming them matters: a file with an `admin_user` or `clients` section
        # must not look as though it was honoured (§16.7).
        raise ConfigInvalid(f"unknown section(s): {unknown}")


def _languages() -> tuple[str, ...]:
    # Imported lazily for the same reason app/core/services/translations.py
    # does it: app.seed reads the filesystem, and core should not at import
    # time.
    from app.seed import LANGUAGES

    return LANGUAGES


def _catalogue() -> dict[str, dict[str, str]]:
    from app.seed import load_locale_catalogue

    return load_locale_catalogue()


def _check_lang(lang: Any, where: str) -> str:
    """Hard rule 5 lives here for imports: `am` is Amharic and cannot enter."""
    if not isinstance(lang, str) or lang not in _languages():
        raise ConfigInvalid(
            f"{where}: unknown language {lang!r}; expected one of {list(_languages())}"
        )
    return lang


def _dicts(value: Any, section: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ConfigInvalid(f"{section}: expected a list of objects")
    return value


def _required(item: dict[str, Any], key: str, section: str) -> Any:
    if key not in item or item[key] in (None, ""):
        raise ConfigInvalid(f"{section}: every entry needs {key!r}")
    return item[key]


async def _import_practice(session: AsyncSession, raw: Any, report: ImportReport) -> None:
    entry = report.section("practice")
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ConfigInvalid("practice: expected an object")

    unknown = sorted(set(raw) - MUTABLE_FIELDS)
    if unknown:
        raise ConfigInvalid(f"practice: not settable: {unknown}")

    changes: dict[str, Any] = dict(raw)
    if "booking_mode" in changes:
        changes["booking_mode"] = _enum(
            BookingMode, changes["booking_mode"], "practice.booking_mode"
        )
    if "default_language" in changes:
        _check_lang(changes["default_language"], "practice.default_language")
    if "reminder_offsets_min" in changes:
        offsets = changes["reminder_offsets_min"]
        if not isinstance(offsets, list) or any(not isinstance(n, int) for n in offsets):
            raise ConfigInvalid("practice.reminder_offsets_min: expected a list of integers")

    practice = await get_practice(session)
    if all(getattr(practice, name) == value for name, value in changes.items()):
        entry.unchanged = 1
        return

    try:
        await update_settings(session, **changes)
    except ValueError as exc:
        # update_settings owns what is settable; do not second-guess it here.
        raise ConfigInvalid(f"practice: {exc}") from exc
    entry.updated = 1


def _enum(enum_cls: Any, value: Any, where: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = [member.value for member in enum_cls]
        raise ConfigInvalid(f"{where}: unknown value {value!r}; expected one of {allowed}") from exc


async def _import_session_types(session: AsyncSession, raw: Any, report: ImportReport) -> None:
    entry = report.section("session_types")
    practice = await get_practice(session)

    for item in _dicts(raw, "session_types"):
        code = str(_required(item, "code", "session_types"))
        unknown = sorted(set(item) - set(_SESSION_TYPE_FIELDS))
        if unknown:
            raise ConfigInvalid(f"session_types[{code}]: unknown field(s): {unknown}")

        existing = (
            await session.execute(select(SessionType).where(SessionType.code == code))
        ).scalar_one_or_none()

        values = {name: item[name] for name in _SESSION_TYPE_FIELDS if name in item}
        values.pop("code", None)

        if existing is None:
            session.add(SessionType(practice_id=practice.id, code=code, **values))
            entry.created += 1
        elif _assign(existing, values):
            entry.updated += 1
        else:
            entry.unchanged += 1

    await session.flush()


async def _import_timezones(session: AsyncSession, raw: Any, report: ImportReport) -> None:
    entry = report.section("timezone_options")
    practice = await get_practice(session)

    for item in _dicts(raw, "timezone_options"):
        iana = str(_required(item, "iana_name", "timezone_options"))
        unknown = sorted(set(item) - set(_TIMEZONE_FIELDS))
        if unknown:
            raise ConfigInvalid(f"timezone_options[{iana}]: unknown field(s): {unknown}")

        existing = (
            await session.execute(select(TimezoneOption).where(TimezoneOption.iana_name == iana))
        ).scalar_one_or_none()

        values = {name: item[name] for name in _TIMEZONE_FIELDS if name in item}
        values.pop("iana_name", None)

        if existing is None:
            session.add(TimezoneOption(practice_id=practice.id, iana_name=iana, **values))
            entry.created += 1
        elif _assign(existing, values):
            entry.updated += 1
        else:
            entry.unchanged += 1

    await session.flush()


async def _import_content(session: AsyncSession, raw: Any, report: ImportReport) -> None:
    topics_entry = report.section("content_topics")
    blocks_entry = report.section("content_blocks")
    practice = await get_practice(session)

    for item in _dicts(raw, "content"):
        code = str(_required(item, "code", "content"))
        unknown = sorted(set(item) - set(_TOPIC_FIELDS) - {"blocks"})
        if unknown:
            raise ConfigInvalid(f"content[{code}]: unknown field(s): {unknown}")

        topic = (
            await session.execute(select(ContentTopic).where(ContentTopic.code == code))
        ).scalar_one_or_none()
        values = {name: item[name] for name in _TOPIC_FIELDS if name in item}
        values.pop("code", None)

        if topic is None:
            topic = ContentTopic(practice_id=practice.id, code=code, **values)
            session.add(topic)
            await session.flush()
            topics_entry.created += 1
        elif _assign(topic, values):
            topics_entry.updated += 1
        else:
            topics_entry.unchanged += 1

        await _import_blocks(session, topic, item.get("blocks"), code, blocks_entry)


async def _import_blocks(
    session: AsyncSession,
    topic: ContentTopic,
    raw: Any,
    code: str,
    entry: SectionReport,
) -> None:
    seen: set[tuple[str, int]] = set()

    for item in _dicts(raw, f"content[{code}].blocks"):
        where = f"content[{code}].blocks"
        lang = _check_lang(item.get("lang"), where)
        position = _required(item, "position", where)
        if not isinstance(position, int):
            raise ConfigInvalid(f"{where}: position must be an integer, not {position!r}")

        unknown = sorted(set(item) - set(_BLOCK_FIELDS))
        if unknown:
            raise ConfigInvalid(f"{where}[{lang}:{position}]: unknown field(s): {unknown}")

        if (lang, position) in seen:
            raise ConfigInvalid(f"{where}: two blocks claim {lang!r} position {position}")
        seen.add((lang, position))

        body_md = str(_required(item, "body_md", where))
        kind = _enum(
            ContentBlockKind, item.get("kind", ContentBlockKind.text.value), f"{where}.kind"
        )
        link_url = item.get("link_url")
        is_published = bool(item.get("is_published", True))

        existing = (
            await session.execute(
                select(ContentBlock).where(
                    ContentBlock.topic_id == topic.id,
                    ContentBlock.lang == lang,
                    ContentBlock.position == position,
                )
            )
        ).scalar_one_or_none()

        if existing is not None and existing.body_md == body_md:
            # §16.7: an unchanged body writes no revision and does not bump
            # `version`. Re-importing the same file must be a genuine no-op,
            # or twenty imports would erase the block's real history.
            if _assign(
                existing, {"kind": kind, "link_url": link_url, "is_published": is_published}
            ):
                entry.updated += 1
            else:
                entry.unchanged += 1
            continue

        # New body, or no block yet: the editor's own path, so the previous
        # body lands in content_block_revision and the Markdown subset is
        # enforced exactly as it is at save time.
        try:
            await upsert_block(
                session,
                topic_id=topic.id,
                lang=lang,
                position=position,
                body_md=body_md,
                kind=kind,
                link_url=link_url,
                is_published=is_published,
            )
        except DomainError as exc:
            raise ConfigInvalid(f"{where}[{lang}:{position}]: {exc}") from exc

        if existing is None:
            entry.created += 1
        else:
            entry.updated += 1


async def _import_translations(session: AsyncSession, raw: Any, report: ImportReport) -> None:
    entry = report.section("translations")
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ConfigInvalid("translations: expected an object keyed by language")

    catalogue = _catalogue()
    known = set(catalogue.get("en", {}))

    for lang, values in raw.items():
        _check_lang(lang, "translations")
        if not isinstance(values, dict):
            raise ConfigInvalid(f"translations[{lang}]: expected an object of key to value")

        rows = (
            await session.execute(
                select(Translation.key, Translation.value).where(Translation.lang == lang)
            )
        ).all()
        existing: dict[str, str] = dict(rows)  # type: ignore[arg-type]

        for key, value in values.items():
            if not isinstance(value, str):
                raise ConfigInvalid(f"translations[{lang}][{key}]: value must be a string")

            if key not in known:
                # Usually a file from a newer build. A key no renderer reads is
                # dead weight, so it is reported rather than written (§16.7).
                report.warnings.append(f"{lang}: no such translation key {key!r}")
                entry.skipped += 1
                continue
            if lang != "en" and key.startswith(ADMIN_PREFIX):
                report.warnings.append(f"{lang}: {key!r} is English-only and was skipped")
                entry.skipped += 1
                continue

            if key in existing:
                if existing[key] == value:
                    entry.unchanged += 1
                    continue
                entry.updated += 1
            else:
                entry.created += 1

            await set_text(session, lang, key, value)

    invalidate_cache()


def _assign(row: Any, values: dict[str, Any]) -> bool:
    """Set only what differs. Returns whether anything did."""
    changed = False
    for name, value in values.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    return changed


__all__ = [
    "FORMAT",
    "KNOWN_VERSIONS",
    "VERSION",
    "ConfigInvalid",
    "ImportReport",
    "SectionReport",
    "dump_config",
    "export_config",
    "import_config",
    "load_config",
]
