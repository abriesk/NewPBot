"""The admin guide at /admin/help (§12.2).

The page is a shipped file rather than a template, which moves the risk: not
"does it render" but "is the file still intact and still self-contained". A
guide that reaches for a CDN would simply fail to style itself on the day the
practice's server has no outbound network, and a guide whose encoding was
mangled by a careless edit is unreadable in exactly the language its reader
needs.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.channels.web.help import HELP_DIR, HELP_LANGUAGES, help_guide
from app.channels.web.security import ADMIN_COOKIE, CSRF_COOKIE
from app.config import get_settings
from app.main import create_app

ADMIN_USER = get_settings().admin_username
ADMIN_PASSWORD = get_settings().admin_password


@pytest.fixture
def web() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


def _sign_in(client: TestClient) -> None:
    client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={
            "csrf_token": client.cookies.get(CSRF_COOKIE, ""),
            "username": ADMIN_USER,
            "password": ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, "admin sign-in failed"
    assert client.cookies.get(ADMIN_COOKIE)


# --- The route --------------------------------------------------------------


def test_the_guide_needs_a_session(web: TestClient) -> None:
    response = web.get("/admin/help", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_the_guide_is_served_in_english_by_default(web: TestClient) -> None:
    _sign_in(web)
    response = web.get("/admin/help")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Admin user guide" in response.text


def test_the_guide_is_served_in_russian(web: TestClient) -> None:
    _sign_in(web)
    response = web.get("/admin/help?lang=ru")

    assert response.status_code == 200
    assert "Руководство администратора" in response.text


@pytest.mark.parametrize("lang", ["hy", "am", "xx", "", "../../etc/passwd"])
def test_a_language_with_no_guide_falls_back_to_english(web: TestClient, lang: str) -> None:
    """A missing page is worse than the wrong language -- and the fallback is
    also what stops the parameter being a path."""
    _sign_in(web)
    response = web.get("/admin/help", params={"lang": lang})

    assert response.status_code == 200
    assert "Admin user guide" in response.text


# --- The shipped files ------------------------------------------------------


def test_every_declared_language_has_a_guide() -> None:
    for lang in HELP_LANGUAGES:
        assert (HELP_DIR / f"admin-guide.{lang}.html").is_file()


@pytest.mark.parametrize("lang", HELP_LANGUAGES)
def test_a_guide_fetches_nothing_from_the_network(lang: str) -> None:
    """Self-contained, like every other admin page: no CDN, no web font."""
    text = help_guide(lang)

    for forbidden in ("<script src", "<link rel=\"stylesheet\"", "@import", "<iframe"):
        assert forbidden not in text, f"{lang} guide pulls in {forbidden}"


@pytest.mark.parametrize("lang", HELP_LANGUAGES)
def test_a_guide_is_intact_utf8(lang: str) -> None:
    """The double-encoding guard. `Ð` and `â` in quantity mean a UTF-8 file was
    read as Latin-1 somewhere and written back out."""
    text = help_guide(lang)

    assert "Ð" not in text and "â€" not in text
    assert "Psychobooking" in text


def test_the_two_guides_link_to_each_other() -> None:
    assert "/admin/help?lang=ru" in help_guide("en")
    assert "/admin/help?lang=en" in help_guide("ru")
    for lang in HELP_LANGUAGES:
        assert 'href="/admin/requests"' in help_guide(lang), "no way back to the console"


def test_the_russian_guide_is_actually_in_russian() -> None:
    text = help_guide("ru")

    for word in ("Заявки", "Лист ожидания", "Слоты", "Настройки"):
        assert word in text
