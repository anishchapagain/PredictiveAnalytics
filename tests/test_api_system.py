"""Tests for the system endpoints: GET /, /health, /ready."""

from fastapi.testclient import TestClient

from app.api.deps import get_repository
from app.core.exceptions import DataSourceError
from app.data.repository import DataRepository
from app.main import app


def test_root(client: TestClient):
    res = client.get("/")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == app.title
    assert body["version"] == app.version
    for key in ("docs_url", "redoc_url", "dashboard_url", "health_url", "ready_url"):
        assert body[key], f"{key} should be a non-empty link"


def test_health_is_always_ok_and_never_touches_the_data_source():
    # No dependency override at all here -- if /health ever starts calling
    # get_repository(), this test would fail with a 500 instead of 200, since the real
    # CSVDataRepository would try (and fail) to read a file that isn't present in tests.
    with TestClient(app) as c:
        res = c.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_ready_succeeds_against_the_fixture(client: TestClient):
    res = client.get("/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["data_backend"] == "csv"
    assert body["rows_loaded"] == 240  # 120 fixture days x 2 corridors


def test_ready_reports_503_when_the_data_source_is_unavailable():
    class BrokenRepository(DataRepository):
        def load(self):
            raise DataSourceError("simulated outage")

    app.dependency_overrides[get_repository] = lambda: BrokenRepository()
    try:
        with TestClient(app) as c:
            res = c.get("/ready")
        assert res.status_code == 503
        assert res.json()["detail"] == "Data source unavailable"
    finally:
        app.dependency_overrides.clear()
