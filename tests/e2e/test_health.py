"""Health surface (IMPLEMENTATION.md §19, M0 acceptance).

The liveness/readiness split, and nothing beyond it: /healthz answers without
a database because it reports that the process is up, and /readyz answers 503
without one because it reports that the process can do its work.

The rest of the health story is elsewhere -- the thresholds in
tests/core/test_status.py, the file the worker writes in
tests/core/test_status_job.py, and the page the therapist reads in
tests/e2e/test_health_page.py.
"""

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
