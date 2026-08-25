"""Labelling, training-matrix assembly, and the leakage guards around them."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.data.fastf1_client import LoadedSession, SessionResult, Stint
from app.db import models as m
from app.features.builder import FeatureBuilder
from app.features.engineering import RACE_FEATURES
from app.training.dataset import (
    InsufficientTrainingData,
    build_training_set,
    label_race_session,
)


def _result(driver_id: str, position: float | None, status: str = "Finished") -> SessionResult:
    return SessionResult(
        driver_id=driver_id,
        driver_number=None,
        driver_name=driver_id,
        team_name="Apex Racing",
        position=position,
        grid_position=position,
        best_lap_s=90.0 + position if position is not None else None,
        status=status,
    )


def _snapshot(session_id: int, driver_id: str, context: str, regime: str = "2026", **labels):
    return m.FeatureSnapshot(
        session_id=session_id,
        driver_id=driver_id,
        context=context,
        regs_regime=regime,
        features={name: 0.5 for name in RACE_FEATURES},
        **labels,
    )


class TestRaceLabelling:
    def test_finishers_keep_their_position(self, db, seeded):
        race = seeded["race"]
        db.add_all([_snapshot(race.id, "ABC", "lap_5"), _snapshot(race.id, "XYZ", "lap_5")])
        db.flush()
        loaded = LoadedSession(
            year=2026, event_name="X", circuit_key="silverstone", session_type="Race",
            start_time=None, is_sprint_weekend=False,
            results=[_result("ABC", 1.0), _result("XYZ", 4.0)],
            stints=[
                Stint("ABC", 1, "MEDIUM", 1, 25, 0.0),
                Stint("ABC", 2, "HARD", 26, 57, 5.0),
            ],
        )
        assert label_race_session(db, race, loaded) == 2
        labels = {s.driver_id: s for s in db.query(m.FeatureSnapshot).all()}
        assert labels["ABC"].label_finish_position == 1.0
        assert labels["ABC"].label_strategy == "1-stop-medium-hard"
        assert labels["XYZ"].label_finish_position == 4.0

    def test_a_retirement_is_parked_at_the_back_not_dropped(self, db, seeded):
        """Reliability stays learnable only if DNF rows survive labelling."""
        race = seeded["race"]
        db.add(_snapshot(race.id, "ABC", "lap_5"))
        db.flush()
        loaded = LoadedSession(
            year=2026, event_name="X", circuit_key="silverstone", session_type="Race",
            start_time=None, is_sprint_weekend=False,
            results=[_result("ABC", None, "Engine"), _result("XYZ", 1.0)],
        )
        label_race_session(db, race, loaded)
        snapshot = db.query(m.FeatureSnapshot).filter_by(driver_id="ABC").one()
        assert snapshot.label_finish_position == 2.0  # field size, i.e. last

    def test_unknown_status_is_not_a_retirement(self):
        """2026 classification data is often absent; absence is not a DNF."""
        assert not _result("ABC", 3.0, "").is_dnf
        assert not _result("ABC", 3.0, "nan").is_dnf
        assert _result("ABC", None, "Accident").is_dnf


class TestTrainingSetAssembly:
    def _two_sessions(self, db, seeded):
        base = datetime(2026, 3, 8, 15, tzinfo=timezone.utc)
        first = seeded["race"]
        first.start_time = base
        second = m.Session(
            year=2026, circuit_id="silverstone", session_type="Sprint",
            regs_regime="2026", status=m.SessionStatus.COMPLETED.value,
            start_time=base + timedelta(days=7),
        )
        db.add(second)
        db.flush()
        for index in range(30):
            db.add(_snapshot(first.id, "ABC", f"lap_{index}", label_finish_position=float(index % 20 + 1)))
            db.add(_snapshot(second.id, "XYZ", f"lap_{index}", label_finish_position=float(index % 20 + 1)))
        db.flush()
        return first, second

    def test_most_recent_session_is_held_out(self, db, seeded):
        _first, second = self._two_sessions(db, seeded)
        dataset = build_training_set(
            db,
            feature_names=RACE_FEATURES,
            label_getter=lambda s: s.label_finish_position,
            session_types={"Race", "Sprint"},
            min_rows=10,
        )
        assert dataset.holdout_session_id == second.id
        assert len(dataset.val_rows) == 30
        assert len(dataset.rows) == 30

    def test_pre_regime_rows_are_dropped_for_a_regime_only_matrix(self, db, seeded):
        first, _second = self._two_sessions(db, seeded)
        for index in range(30, 40):
            db.add(
                _snapshot(
                    first.id, "ABC", f"lap_{index}", regime="2025",
                    label_finish_position=1.0,
                )
            )
        db.flush()
        dataset = build_training_set(
            db,
            feature_names=RACE_FEATURES,  # contains regime-only tyre/team features
            label_getter=lambda s: s.label_finish_position,
            session_types={"Race", "Sprint"},
            min_rows=10,
        )
        assert len(dataset.rows) == 30, "2025 rows must not train a 2026 model"

    def test_too_little_data_raises_rather_than_training(self, db, seeded):
        self._two_sessions(db, seeded)
        with pytest.raises(InsufficientTrainingData):
            build_training_set(
                db,
                feature_names=RACE_FEATURES,
                label_getter=lambda s: s.label_finish_position,
                session_types={"Race", "Sprint"},
                min_rows=500,
            )


class TestAsOfLeakageGuard:
    def test_aggregates_exclude_the_session_being_predicted(self, db, seeded):
        """A session's own results must not feed the features predicting it."""
        base = datetime(2026, 3, 8, 15, tzinfo=timezone.utc)
        earlier = seeded["race"]
        earlier.start_time = base
        earlier.status = m.SessionStatus.COMPLETED.value
        target = m.Session(
            year=2026, circuit_id="silverstone", session_type="Sprint",
            regs_regime="2026", status=m.SessionStatus.COMPLETED.value,
            start_time=base + timedelta(days=7),
        )
        db.add(target)
        db.flush()

        builder = FeatureBuilder(db, season=2026, as_of=target.start_time)
        visible = {s.session_type for s in builder._session_rows(2026)}
        assert "Race" in visible
        assert "Sprint" not in visible, "the target session must be invisible to its own features"

    def test_no_cutoff_sees_everything(self, db, seeded):
        seeded["race"].status = m.SessionStatus.COMPLETED.value
        seeded["race"].start_time = datetime(2026, 3, 8, 15, tzinfo=timezone.utc)
        db.flush()
        builder = FeatureBuilder(db, season=2026)
        assert {s.session_type for s in builder._session_rows(2026)} == {"Race"}
