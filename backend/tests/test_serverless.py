"""The Vercel function entry (api/index.py) and serverless start-up."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_entry():
    """Import api/index.py the way Vercel does: as a file, by path."""
    spec = importlib.util.spec_from_file_location("vercel_api_index", REPO_ROOT / "api" / "index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def entry():
    return _load_entry()


def test_health_answers_under_api_prefix(entry):
    client = TestClient(entry.app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert isinstance(response.json(), dict)


def test_routes_outside_the_prefix_still_reach_the_app(entry):
    # Should a rewrite ever hand over the path without /api, nothing breaks.
    assert TestClient(entry.app).get("/health").status_code == 200


def test_app_config_reports_websockets(entry):
    body = TestClient(entry.app).get("/api/app-config").json()
    assert body["websockets"] is True  # the test process is not serverless


def test_openapi_knows_its_prefix(entry):
    assert TestClient(entry.app).get("/api/openapi.json").status_code == 200


def test_serverless_lifespan_starts_no_background_work(monkeypatch):
    from app import main
    from app.config import settings

    started: list[str] = []
    monkeypatch.setattr(settings, "SERVERLESS", True)
    monkeypatch.setattr(settings, "LIVE_AUTOSTART", True)
    monkeypatch.setattr(main.f1live_hub, "start", lambda: started.append("hub"))
    monkeypatch.setattr(main.director, "start", lambda: started.append("director"))

    with TestClient(main.app) as client:  # the context manager runs lifespan
        assert client.get("/health").status_code == 200
    assert started == []


def test_local_lifespan_still_starts_the_hub(monkeypatch):
    from app import main
    from app.config import settings

    started: list[str] = []
    monkeypatch.setattr(settings, "SERVERLESS", False)
    monkeypatch.setattr(settings, "LIVE_AUTOSTART", False)
    monkeypatch.setattr(main.f1live_hub, "start", lambda: started.append("hub"))
    monkeypatch.setattr(main.director, "start", lambda: started.append("director"))

    with TestClient(main.app):
        pass
    assert started == ["hub"]
