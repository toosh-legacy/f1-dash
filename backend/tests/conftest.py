"""Test fixtures: an isolated on-disk SQLite database per test session."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Point the app at a throwaway database before anything imports app.config.
_TMP = Path(tempfile.mkdtemp(prefix="f1dash-tests-"))
os.environ.setdefault("F1_DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ.setdefault("F1_MODEL_DIR", str(_TMP / "artifacts"))
os.environ.setdefault("F1_FASTF1_CACHE", str(_TMP / "cache"))

from app.db import models as m  # noqa: E402
from app.db.database import Base, SessionLocal, engine  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def seeded(db):
    """A circuit, a team, two drivers, and a qualifying + race session."""
    circuit = m.Circuit(
        id="silverstone",
        name="British Grand Prix",
        type="permanent",
        altitude_m=153,
        avg_pit_loss_s=20.5,
        historical_sc_rate=0.4,
        historical_overtaking_difficulty=0.35,
    )
    team = m.Team(id="apex", name="Apex Racing", power_unit="Mercedes")
    drivers = [
        m.Driver(id="ABC", name="A Driver", driver_number=1, team_id="apex"),
        m.Driver(id="XYZ", name="X Driver", driver_number=2, team_id="apex", is_rookie=True),
    ]
    quali = m.Session(
        year=2026,
        circuit_id="silverstone",
        session_type="Qualifying",
        regs_regime="2026",
        status=m.SessionStatus.SCHEDULED.value,
        weather_snapshot={"air_temp": 21.0, "track_temp": 30.0, "humidity": 55.0, "rainfall": False},
    )
    race = m.Session(
        year=2026,
        circuit_id="silverstone",
        session_type="Race",
        regs_regime="2026",
        status=m.SessionStatus.SCHEDULED.value,
    )
    db.add_all([circuit, team, *drivers, quali, race])
    db.flush()
    return {"circuit": circuit, "team": team, "drivers": drivers, "quali": quali, "race": race}
