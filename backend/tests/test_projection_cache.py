"""The projection cache and the batch pass that fills it.

Two properties matter. A finished race must be evaluated once -- scrubbing it
should read rows, not run models. And a cached answer must never outlive the
model that produced it: promoting a new version has to change what the panel
shows, not serve the old model's opinion under the new one's name.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.db import models as m
from app.db import projection_store as store
from app.models import replay_predictor


def payload(lap: int, leader: str = "AAA") -> dict[str, Any]:
    return {"lap": lap, "entries": [{"code": leader, "projected_position": 1}]}


class TestStore:
    def test_a_stored_projection_reads_back(self, db):
        store.write(db, 11342, 30, payload(30), model_version=7)
        assert store.read(db, 11342, 30, 7) == payload(30)

    def test_another_model_s_answer_is_a_miss_not_a_hit(self, db):
        """A projection is an answer to a question asked of one model."""
        store.write(db, 11342, 30, payload(30, "AAA"), model_version=7)
        assert store.read(db, 11342, 30, 9) is None
        assert store.read(db, 11342, 30, None) is None

    def test_a_run_with_no_model_is_cached_under_no_version(self, db):
        """The arithmetic projection stands on its own and is worth caching."""
        store.write(db, 11342, 12, payload(12), model_version=None)
        assert store.read(db, 11342, 12, None) == payload(12)

    def test_rerunning_a_lap_replaces_it_rather_than_duplicating(self, db):
        store.write(db, 11342, 30, payload(30, "AAA"), model_version=7)
        store.write(db, 11342, 30, payload(30, "BBB"), model_version=7)
        assert store.read(db, 11342, 30, 7)["entries"][0]["code"] == "BBB"
        assert db.query(m.ReplayProjection).count() == 1

    def test_coverage_reports_what_has_been_run(self, db):
        for lap in (1, 2, 3):
            store.write(db, 11342, lap, payload(lap), model_version=7)
        store.write(db, 11342, 1, payload(1), model_version=9)

        runs = {run["model_version"]: run for run in store.coverage(db, 11342)["runs"]}
        assert runs[7]["laps"] == 3 and runs[7]["last_lap"] == 3
        assert runs[9]["laps"] == 1

    def test_clearing_is_scoped(self, db):
        store.write(db, 11342, 1, payload(1), model_version=7)
        store.write(db, 11353, 1, payload(1), model_version=7)
        assert store.clear(db, 11342) == 1
        assert store.read(db, 11353, 1, 7) is not None


class TestPredictor:
    def _bundle(self, laps: int = 3) -> dict[str, Any]:
        return {
            "total_laps": laps,
            "session": {"session_key": 4242, "circuit": "Hungaroring"},
            "drivers": [{"number": 1, "code": "AAA", "team": "Apex", "colour": "#fff"}],
            "laps": {
                str(lap): [
                    {
                        "number": 1,
                        "position": 1,
                        "gap_to_leader_s": 0.0,
                        "pace_s": 80.0,
                        "pace_delta_s": 0.0,
                        "compound": "HARD",
                        "tyre_age": 5,
                        "stops": 1,
                    }
                ]
                for lap in range(1, laps + 1)
            },
        }

    def test_the_first_ask_computes_and_the_second_reads(self, db):
        bundle = self._bundle()
        first = replay_predictor.projection_for(db, bundle, 2)
        assert first["cached"] is False

        second = replay_predictor.projection_for(db, bundle, 2)
        assert second["cached"] is True
        assert second["entries"][0]["code"] == first["entries"][0]["code"]

    def test_a_caller_can_insist_on_a_fresh_answer(self, db):
        bundle = self._bundle()
        replay_predictor.projection_for(db, bundle, 2)
        assert replay_predictor.projection_for(db, bundle, 2, use_cache=False)["cached"] is False

    def test_evaluating_an_unbuilt_replay_says_so(self):
        with pytest.raises(Exception) as raised:
            replay_predictor.run(999_999)
        assert "not built" in str(raised.value)

    def test_the_cache_key_follows_the_active_model(self, db, seeded):
        """Nothing is cached under a model that is not the one serving."""
        assert replay_predictor.active_model_version(db) is None

        record = m.PredictionModel(
            model_type=m.ModelType.RACE_FINISH_POSITION.value,
            version=1,
            regs_regime="2026",
            artifact_path="unused.json",
            is_active=True,
            validation_metric="mae",
            validation_score=2.0,
        )
        db.add(record)
        db.flush()
        assert replay_predictor.active_model_version(db) == record.id

        # A model from another regime is refused at serve time, so it is not
        # the version anything is cached under either.
        record.regs_regime = "2025"
        db.flush()
        assert replay_predictor.active_model_version(db) is None
