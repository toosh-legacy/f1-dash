"""The projection behind the prediction panel.

Its job is to be readable, not clever: every term shown in the panel has to be
the term the ranking actually used. These tests pin the reasoning rather than
the numbers -- that a car quick enough catches one ahead of it, that a stop
still owed costs a pit loss, and that a single fast lap early on cannot throw
the order fifty laps into the future.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.db import models as m
from app.live import projection as P


def bundle(total_laps: int = 60, rows: dict[int, list[dict]] | None = None, **session):
    return {
        "total_laps": total_laps,
        "session": {"session_key": 9999, "circuit": "Hungaroring", **session},
        "drivers": [
            {"number": 1, "code": "AAA", "name": "A Driver", "team": "Apex", "colour": "#ff0000"},
            {"number": 2, "code": "BBB", "name": "B Driver", "team": "Apex", "colour": "#00ff00"},
        ],
        "laps": {str(lap): entries for lap, entries in (rows or {}).items()},
    }


def car(number: int, position: int, *, gap=0.0, pace_delta=0.0, compound="MEDIUM", age=10, stops=1):
    return {
        "number": number,
        "position": position,
        "gap_to_leader_s": gap,
        "pace_s": 80.0,
        "pace_delta_s": pace_delta,
        "compound": compound,
        "tyre_age": age,
        "stops": stops,
    }


class TestProjection:
    def test_a_quicker_car_is_projected_past_the_one_ahead(self):
        data = bundle(rows={40: [
            car(1, 1, gap=0.0, pace_delta=0.0),
            car(2, 2, gap=6.0, pace_delta=0.5),   # half a second a lap quicker
        ]})
        result = P.project_finish(data, 40)
        assert [entry["number"] for entry in result["entries"]] == [2, 1]
        assert result["entries"][0]["position_change"] == 1

    def test_a_gap_too_large_to_close_survives_a_pace_advantage(self):
        data = bundle(rows={55: [
            car(1, 1, gap=0.0, pace_delta=0.0),
            car(2, 2, gap=40.0, pace_delta=0.5),  # only five laps left to use it
        ]})
        result = P.project_finish(data, 55)
        assert [entry["number"] for entry in result["entries"]] == [1, 2]

    def test_a_stop_still_owed_costs_a_pit_loss(self):
        """Track position means nothing if the car has not stopped yet."""
        data = bundle(rows={30: [
            car(1, 1, gap=0.0, compound="SOFT", age=17, stops=0),   # must stop again
            car(2, 2, gap=8.0, compound="HARD", age=2, stops=1),    # to the flag on these
        ]})
        result = P.project_finish(data, 30)
        assert result["entries"][0]["number"] == 2
        assert result["entries"][0]["stops_owed"] == 0
        leader = next(e for e in result["entries"] if e["number"] == 1)
        assert leader["stops_owed"] == 1

    def test_one_fast_lap_at_the_start_cannot_win_the_race(self):
        """Pace is trusted in proportion to how much of it has been seen."""
        data = bundle(rows={1: [
            car(1, 1, gap=0.0, pace_delta=0.0),
            car(2, 20, gap=25.0, pace_delta=3.0),  # a wild reading on lap one
        ]})
        result = P.project_finish(data, 1)
        assert result["entries"][0]["number"] == 1
        gained = next(e for e in result["entries"] if e["number"] == 2)["pace_gain_s"]
        assert gained < P.PACE_CLAMP_S * data["total_laps"], "pace must be clamped and ramped"

    def test_every_term_behind_the_ranking_is_reported(self):
        data = bundle(rows={20: [car(1, 1), car(2, 2, gap=3.0)]})
        entry = P.project_finish(data, 20)["entries"][0]
        for field in ("pace_delta_s", "pace_gain_s", "stops_owed", "gap_to_leader_s",
                      "projected_delta_s", "position_change", "compound", "tyre_age"):
            assert field in entry, f"the panel shows {field}, so the projection must return it"

    def test_a_lap_with_no_timing_is_empty_rather_than_wrong(self):
        assert P.project_finish(bundle(rows={}), 12)["entries"] == []


class TestStopsOwed:
    def test_fresh_rubber_late_on_owes_nothing(self):
        assert P._stops_owed(car(1, 1, compound="HARD", age=2), remaining=20) == 0

    def test_worn_rubber_with_a_long_way_to_go_owes_a_stop(self):
        assert P._stops_owed(car(1, 1, compound="SOFT", age=16), remaining=40) >= 1

    def test_nothing_is_owed_inside_the_last_stint(self):
        """Near the end a car takes whatever is fitted to the flag."""
        assert P._stops_owed(car(1, 1, compound="SOFT", age=30), remaining=5) == 0


class TestSessionResolution:
    def test_the_openf1_key_is_recorded_the_first_time_a_date_matches(self, db, seeded):
        race = seeded["race"]
        race.start_time = datetime(2026, 7, 26, 13, tzinfo=timezone.utc)
        db.flush()

        data = bundle(name="Race", date_start="2026-07-26T13:00:00+00:00")
        resolved = P.resolve_db_session(db, data)
        assert resolved is not None and resolved.id == race.id
        assert resolved.openf1_session_key == 9999, "the join should be remembered, not re-derived"

    def test_an_unknown_session_resolves_to_nothing(self, db, seeded):
        data = bundle(name="Race", date_start="2011-01-01T00:00:00+00:00")
        assert P.resolve_db_session(db, data) is None

    def test_a_missing_session_leaves_the_projection_standing(self, db, seeded):
        """The arithmetic never depends on a model being available."""
        data = bundle(rows={10: [car(1, 1), car(2, 2, gap=2.0)]},
                      date_start="2011-01-01T00:00:00+00:00")
        result = P.project_finish(data, 10, db=db)
        assert len(result["entries"]) == 2
        assert result["model"]["available"] is False
        assert "not in the database" in result["model"]["reason"]


class TestPitLoss:
    def test_the_circuit_s_own_pit_loss_is_used_when_it_is_known(self, db, seeded):
        data = bundle(circuit="Silverstone")
        assert P._pit_loss(data, db) == seeded["circuit"].avg_pit_loss_s

    def test_an_unknown_circuit_falls_back(self, db, seeded):
        assert P._pit_loss(bundle(circuit="Nowhere"), db) == P.DEFAULT_PIT_LOSS_S
