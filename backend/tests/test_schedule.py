"""The race picker's schedule: sessions still to come, from OpenF1."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import replays as replays_api
from app.data import openf1_client
from app.main import app


def _row(key, start, hours=1, cancelled=False, name="Race"):
    return {
        "session_key": key, "session_name": name, "session_type": "Race",
        "circuit_short_name": "Somewhere", "country_name": "Nowhere",
        "date_start": start.isoformat(), "date_end": (start + timedelta(hours=hours)).isoformat(),
        "is_cancelled": cancelled,
    }


@pytest.fixture
def fake_openf1(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        _row(3, now + timedelta(days=2)),
        _row(1, now - timedelta(days=3)),                    # over
        _row(2, now - timedelta(minutes=30), hours=2),       # running
        _row(4, now + timedelta(days=1), cancelled=True),    # not happening
    ]

    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def sessions(self, year=None, **filters): return rows

    monkeypatch.setattr(openf1_client, "OpenF1Client", Client)
    replays_api._schedule_cache.clear()
    yield
    replays_api._schedule_cache.clear()


def test_schedule_lists_what_is_running_and_to_come_soonest_first(fake_openf1):
    with TestClient(app) as client:
        response = client.get("/schedule")
    assert response.status_code == 200
    body = response.json()
    assert [row["session_key"] for row in body] == [2, 3]
    assert body[0]["running"] is True
    assert body[1]["running"] is False
