"""Demo data for the screenshot runs (np-shot.sh) and the tests that need a populated practice.

Run AFTER `alembic upgrade head` and `python -m app.seed`, inside the app image:  python scripts/seed_demo.py
It REFUSES a database whose name does not end in `_test`. It creates no admin: the base seed's admin (ADMIN_USERNAME /
ADMIN_PASSWORD of the run) is the one that signs in. Idempotent: a second run on the same database does nothing.

What it makes: 3 session types (one with a price), 40+ slots over the next 10 days (days +3 and +7 empty, +2 and +5 full),
a client with a verified email for each of the 7 request statuses, a request in each status (every one with a
scheduled_start), a short thread with a proposal on the negotiating one, one waitlist entry, one content block with
two revisions.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.enums import (
    ActorType,
    Channel,
    ContentBlockKind,
    Modality,
    NegotiationKind,
    RequestStatus,
    SenderType,
    SlotStatus,
    WaitlistStatus,
)
from app.core.models import (
    BookingRequest,
    Client,
    ContentBlock,
    ContentBlockRevision,
    ContentTopic,
    Identity,
    NegotiationMessage,
    Practice,
    SessionType,
    Slot,
    WaitlistEntry,
)
from app.db import dispose_engine, unit_of_work

TZ = ZoneInfo("Asia/Yerevan")
#: day offset -> (hours of the day, status). Days not listed are empty (+3, +7).
FREE_DAYS = {1: (10, 11, 12, 15, 17, 18), 4: (10, 12, 14, 16, 18, 19), 6: (11, 13, 15, 17, 18, 19),
             8: (10, 11, 15, 16, 17, 18), 9: (12, 13, 14, 17, 18, 19), 10: (10, 11, 12, 15, 16, 18)}
FULL_DAYS = {2: (10, 11, 12, 15, 16), 5: (11, 12, 14, 15, 17)}
ONSITE_EXTRA = {1: 14, 4: 11, 6: 10}  # a few in-person-only slots


def _at(day: int, hour: int) -> datetime:
    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return (today + timedelta(days=day)).replace(hour=hour)


def refuse_unless_test_database() -> None:
    name = make_url(get_settings().database_url).database or ""
    if not name.endswith("_test"):
        raise SystemExit(f"seed_demo: refusing database {name!r}: its name must end in _test")


async def seed(s: AsyncSession) -> bool:
    """Fill the practice of `s`; False when it already holds requests."""
    practice = (await s.execute(select(Practice).order_by(Practice.id).limit(1))).scalar_one()
    if (await s.execute(select(BookingRequest.id).limit(1))).first() is not None:
        return False
    pid = practice.id
    practice.book_per_ip_per_hour = 1000  # every language x size of a screenshot run books one

    types = {t.code: t for t in (await s.execute(select(SessionType).where(SessionType.practice_id == pid))).scalars()}
    indiv = types["individual"]
    indiv.price_amount_minor, indiv.price_currency = 15000, "AMD"
    s.add(SessionType(practice_id=pid, code="intro", duration_min=30, sort_order=2))
    await s.flush()
    intro = (await s.execute(select(SessionType).where(SessionType.code == "intro"))).scalar_one()

    # slots
    for day, hours in FREE_DAYS.items():
        for h in hours:
            s.add(Slot(practice_id=pid, starts_at=_at(day, h), duration_min=60))
        if day in ONSITE_EXTRA:
            s.add(Slot(practice_id=pid, starts_at=_at(day, ONSITE_EXTRA[day]), duration_min=60,
                       modality=Modality.onsite))

    # clients and requests, one per status
    now = datetime.now(TZ)
    plan = [  # status, client name, session type, modality, start, extra fields
        (RequestStatus.pending, "Anna Demo", indiv, Modality.online, _at(1, 18), {}),
        (RequestStatus.negotiating, "Boris Demo", indiv, Modality.online, _at(4, 12), {}),
        (RequestStatus.confirmed, "Carine Demo", intro, Modality.online, _at(2, 11),
         {"meeting_url": "https://meet.example.test/demo-room"}),
        (RequestStatus.rejected, "Davit Demo", indiv, Modality.onsite, _at(6, 13),
         {"rejected_reason": "Sorry, that week is full."}),
        (RequestStatus.expired, "Elena Demo", types["couple"], Modality.online, _at(-3, 15), {}),
        (RequestStatus.cancelled, "Fedor Demo", indiv, Modality.online, _at(5, 14),
         {"cancelled_at": now - timedelta(hours=5), "cancelled_by": ActorType.client,
          "cancellation_reason": "I am ill."}),
        (RequestStatus.completed, "Gayane Demo", indiv, Modality.online, _at(-7, 17),
         {"confirmed_at": now - timedelta(days=9)}),
    ]
    reqs = {}
    for i, (st, who, stype, mod, start, extra) in enumerate(plan):
        lang = ("ru", "hy", "en")[i % 3]
        c = Client(practice_id=pid, display_name=who, language=lang, timezone="Asia/Yerevan",
                   created_at=now - timedelta(days=30 - i))
        s.add(c)
        await s.flush()
        s.add(Identity(practice_id=pid, client_id=c.id, channel=Channel.email,
                       external_id=f"demo{i + 1}@example.test", verified_at=now - timedelta(days=20)))
        fields = dict(extra)
        if st in (RequestStatus.confirmed, RequestStatus.completed):
            fields.setdefault("confirmed_at", now - timedelta(days=1))
        if st is RequestStatus.pending:
            fields["expires_at"] = now + timedelta(hours=40)
        r = BookingRequest(
            practice_id=pid, client_id=c.id, session_type_id=stype.id, modality=mod, status=st,
            source_channel=Channel.web, scheduled_start=start, scheduled_duration_min=stype.duration_min,
            client_timezone="Asia/Yerevan", problem_text="I would like to talk about stress at work.",
            display_name=who, awaiting_verification=False, created_at=now - timedelta(days=12 - i), **fields,
        )
        s.add(r)
        await s.flush()
        reqs[st] = r

    # full days: booked slots (they point at the confirmed request; demo data only)
    for day, hours in FULL_DAYS.items():
        for h in hours:
            s.add(Slot(practice_id=pid, starts_at=_at(day, h), duration_min=60, status=SlotStatus.booked,
                       booked_request=reqs[RequestStatus.confirmed].id))

    # a short thread with a proposal
    neg = reqs[RequestStatus.negotiating]
    base = now - timedelta(hours=30)
    for k, (sender, kind, body, prop) in enumerate((
        (SenderType.client, NegotiationKind.note, "Could we meet in the evening? I finish work at six.", None),
        (SenderType.admin, NegotiationKind.proposal, "Yes. How about this one?", _at(4, 19)),
        (SenderType.client, NegotiationKind.note, "Thank you, let me check my calendar.", None),
    )):
        s.add(NegotiationMessage(request_id=neg.id, sender=sender, kind=kind, body_text=body,
                                 proposed_start=prop, created_at=base + timedelta(hours=k * 4)))

    # waitlist
    wc = Client(practice_id=pid, display_name="Hasmik Demo", language="hy", timezone="Europe/Moscow")
    s.add(wc)
    await s.flush()
    s.add(Identity(practice_id=pid, client_id=wc.id, channel=Channel.email, external_id="waiting@example.test",
                   verified_at=now - timedelta(days=2)))
    s.add(WaitlistEntry(practice_id=pid, client_id=wc.id, problem_text="Evenings only, please.",
                        status=WaitlistStatus.new))

    # one content block with two revisions (the revisions page needs data)
    topic = (await s.execute(select(ContentTopic).where(ContentTopic.code == "work_terms"))).scalar_one()
    block = ContentBlock(practice_id=pid, topic_id=topic.id, lang="en", position=0, kind=ContentBlockKind.text,
                         body_md="A session lasts 60 minutes.\n\nWe meet online or in person.", version=2)
    s.add(block)
    await s.flush()
    s.add(ContentBlockRevision(block_id=block.id, version=1, body_md="A session lasts 60 minutes."))
    await s.flush()
    return True


async def main() -> int:
    refuse_unless_test_database()
    async with unit_of_work() as s:
        done = await seed(s)
    await dispose_engine()
    print("seed_demo: done" if done else "seed_demo: already seeded")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
