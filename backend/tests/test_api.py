"""API surface (guide section 9), including the rule-1 boundary."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import models as m
from app.db.database import SessionLocal, init_db
from app.main import app


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def fixture_ids():
    """Committed rows, since the API opens its own sessions."""
    init_db()
    db = SessionLocal()
    try:
        circuit = db.get(m.Circuit, "api_test_circuit") or m.Circuit(
            id="api_test_circuit", name="API Test Circuit", type="permanent"
        )
        db.add(circuit)
        session = m.Session(
            year=2026,
            circuit_id="api_test_circuit",
            session_type="Race",
            regs_regime="2026",
            status=m.SessionStatus.SCHEDULED.value,
        )
        practice = m.Session(
            year=2026,
            circuit_id="api_test_circuit",
            session_type="FP1",
            regs_regime="2026",
            status=m.SessionStatus.SCHEDULED.value,
        )
        db.add_all([session, practice])
        db.commit()
        ids = {"race": session.id, "practice": practice.id}
    finally:
        db.close()
    yield ids
    db = SessionLocal()
    try:
        for table_id in ids.values():
            row = db.get(m.Session, table_id)
            if row:
                db.delete(row)
        db.commit()
    finally:
        db.close()


class TestMeta:
    def test_health_reports_the_regime(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["regs_regime"] == "2026"

    def test_openapi_documents_the_specified_routes(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        for path in (
            "/circuits",
            "/sessions",
            "/sessions/{session_id}",
            "/sessions/{session_id}/retrain",
            "/sessions/{session_id}/practice",
            "/sessions/{session_id}/qualifying/predictions",
            "/sessions/{session_id}/race/predictions",
            "/models",
            "/models/{model_id}/promote",
        ):
            assert path in paths, f"{path} is missing from the API"

    def test_dashboard_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Live Prediction Dashboard" in response.text
        # The three things the page is: a circuit map, a replay library and a
        # live view. If one stops being served the page is half a dashboard.
        for marker in ('id="map"', 'id="view-replays"', 'id="view-live"'):
            assert marker in response.text


class TestSessions:
    def test_list_and_filter(self, client, fixture_ids):
        assert client.get("/sessions?year=2026").status_code == 200
        filtered = client.get("/sessions?year=2026&session_type=Race").json()
        assert all(s["session_type"] == "Race" for s in filtered)

    def test_missing_session_is_404(self, client):
        assert client.get("/sessions/999999").status_code == 404

    def test_session_detail_exposes_regs_regime(self, client, fixture_ids):
        body = client.get(f"/sessions/{fixture_ids['race']}").json()
        assert body["regs_regime"] == "2026"


class TestRetrainIsNotOnTheRequestPath:
    def test_retrain_returns_immediately_with_a_job(self, client, fixture_ids):
        """Rule 1: the request enqueues a batch job, it does not train."""
        response = client.post(f"/sessions/{fixture_ids['race']}/retrain")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] in {"queued", "running", "failed", "succeeded"}
        assert body["job_id"]

    def test_practice_sessions_are_not_a_training_target(self, client, fixture_ids):
        response = client.post(f"/sessions/{fixture_ids['practice']}/retrain")
        assert response.status_code == 400

    def test_job_status_is_queryable(self, client, fixture_ids):
        job_id = client.post(f"/sessions/{fixture_ids['race']}/retrain").json()["job_id"]
        assert client.get(f"/jobs/{job_id}").json()["job_id"] == job_id
        assert client.get("/jobs/does-not-exist").status_code == 404


class TestPredictions:
    def test_empty_prediction_lists(self, client, fixture_ids):
        assert client.get(f"/sessions/{fixture_ids['race']}/race/predictions").json() == []
        assert client.get(f"/sessions/{fixture_ids['race']}/qualifying/predictions").json() == []

    def test_period_filter_is_validated(self, client, fixture_ids):
        assert (
            client.get(
                f"/sessions/{fixture_ids['race']}/qualifying/predictions?period=Q9"
            ).status_code
            == 422
        )


class TestLiveControl:
    def test_starting_a_loop_needs_a_session_key(self, client, fixture_ids):
        response = client.post(f"/sessions/{fixture_ids['race']}/live/start", json={})
        assert response.status_code == 400
        assert "OpenF1 session key" in response.json()["detail"]

    def test_stopping_an_unknown_loop_is_404(self, client, fixture_ids):
        assert client.post(f"/sessions/{fixture_ids['race']}/live/stop").status_code == 404


class TestWebSocket:
    def test_client_receives_the_handshake_frame(self, client, fixture_ids):
        with client.websocket_connect(f"/sessions/{fixture_ids['race']}/live") as socket:
            message = socket.receive_json()
        assert message["type"] == "connected"
        assert message["regs_regime"] == "2026"

    def test_broadcast_reaches_a_connected_client(self, client, fixture_ids):
        from app.live.broadcast import broadcaster, race_control

        session_id = fixture_ids["race"]
        with client.websocket_connect(f"/sessions/{session_id}/live") as socket:
            assert socket.receive_json()["type"] == "connected"
            broadcaster.publish_threadsafe(session_id, race_control("safety_car", 24))
            message = socket.receive_json()
        assert message["type"] == "race_control"
        assert message["event_type"] == "safety_car"


class TestModels:
    def test_registry_listing(self, client):
        assert isinstance(client.get("/models").json(), list)

    def test_promoting_an_unknown_model_is_404(self, client):
        assert client.post("/models/999999/promote").status_code == 404
