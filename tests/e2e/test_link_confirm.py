"""Emailed links are confirmed, not spent, by a GET (IMPLEMENTATION.md §6.2, §12.1; NP2, REVIEW F11).

Mail scanners and link previews fetch every URL in a message. A login or request
link that a GET spends is dead before the person opens it; the GET now shows a
button and the POST spends the token. Query strings are kept out of the access log.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import TokenPurpose
from app.core.models import AuthToken, BookingRequest, Identity
from app.core.services.clients import issue_token, resolve_client
from app.core.enums import Channel
from app.main import StripQueryFromAccessLog
from tests.e2e.test_web_booking import (  # noqa: F401  (fixtures)
    EMAIL,
    _csrf,
    committed,
    web,
    web_slot,
)
from tests.e2e.test_unverified_holds import _book


async def _used(committed: AsyncSession, raw_token: str) -> bool:
    from app.core.services.clients import _hash

    await committed.rollback()
    committed.expunge_all()
    row = (
        await committed.execute(select(AuthToken).where(AuthToken.token_hash == _hash(raw_token)))
    ).scalar_one()
    return row.used_at is not None


async def test_a_get_leaves_a_login_token_unspent_and_the_post_spends_it(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    client = await resolve_client(committed, Channel.email, EMAIL)
    raw = await issue_token(
        committed, TokenPurpose.login, client_id=client.id, payload={"email": EMAIL}
    )
    await committed.commit()

    page = web.get("/auth/callback", params={"token": raw})
    assert page.status_code == 200
    assert 'action="/auth/callback"' in page.text
    assert not web.cookies.get("pb_client")
    assert not await _used(committed, raw)

    spent = web.post(
        "/auth/callback", data={"csrf_token": _csrf(web), "token": raw}, follow_redirects=False
    )
    assert spent.status_code == 303
    assert await _used(committed, raw)

    # Without the form's CSRF token nothing is spent either.
    again = await issue_token(committed, TokenPurpose.login, client_id=client.id, payload={})
    await committed.commit()
    refused = web.post("/auth/callback", data={"token": again}, follow_redirects=False)
    assert refused.status_code == 403
    assert not await _used(committed, again)


async def test_a_get_leaves_a_request_link_token_unspent(
    web: TestClient, web_slot: int, committed: AsyncSession
) -> None:
    await _book(web, committed, web_slot)
    await committed.rollback()
    ident = (
        await committed.execute(select(Identity).where(Identity.external_id == EMAIL))
    ).scalar_one()
    request = (
        await committed.execute(
            select(BookingRequest).where(BookingRequest.client_id == ident.client_id)
        )
    ).scalar_one()
    raw = await issue_token(committed, TokenPurpose.view_request, client_id=ident.client_id)
    await committed.commit()

    page = web.get(f"/r/{request.uuid}", params={"token": raw})
    assert page.status_code == 200
    assert f'action="/r/{request.uuid}/enter"' in page.text
    assert not await _used(committed, raw)

    entered = web.post(
        f"/r/{request.uuid}/enter",
        data={"csrf_token": _csrf(web), "token": raw},
        follow_redirects=False,
    )
    assert entered.status_code == 303
    assert await _used(committed, raw)


def test_the_access_log_line_loses_its_query_string() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/auth/callback?token=SECRET", "1.1", 200),
        None,
    )
    assert StripQueryFromAccessLog().filter(record)
    assert "SECRET" not in record.getMessage()
    assert "/auth/callback" in record.getMessage()
