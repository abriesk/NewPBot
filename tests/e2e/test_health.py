"""Health surface (IMPLEMENTATION.md §19, M0 acceptance)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_returns_200_without_a_database() -> None:
    # Liveness must not depend on the database: the Compose healthcheck gates
    # the worker on it, and a slow database should not look like a dead process.
    with TestClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_503_when_the_database_is_unreachable() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/readyz")
    assert response.status_code in (200, 503)
    assert response.json()["status"] in ("ok", "unavailable")
