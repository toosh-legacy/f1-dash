"""Scoring the predictions against what actually happened.

The point of this module is to be unflattering when the predictions deserve it,
so the tests are mostly about that: a projection that got the order wrong has
to score worse than one that got it right, and the baseline it is measured
against -- the running order at the time -- has to be scored on the same terms
rather than given a handicap.
"""
from __future__ import annotations

from app.db import projection_store as store
from app.models import calibration


def entry(number: int, *, position: int, projected: int, model: int | None = None):
    row = {"number": number, "position": position, "projected_position": projected}
    if model is not None:
        row["model_position"] = model
    return row


def payload(*entries):
    return {"entries": list(entries)}


def perfect_lap(actual: dict[int, int]):
    return payload(*[entry(n, position=p, projected=p) for n, p in actual.items()])


ACTUAL = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}


class TestScoringOneLap:
    def test_a_perfect_order_scores_zero(self):
        scored = calibration._score_lap(perfect_lap(ACTUAL), ACTUAL)
        assert scored["projection"] == 0.0
        assert scored["track_position"] == 0.0

    def test_error_is_measured_in_places(self):
        # Everyone projected exactly one place out.
        rows = payload(*[entry(n, position=p, projected=min(p + 1, 6)) for n, p in ACTUAL.items()])
        scored = calibration._score_lap(rows, ACTUAL)
        assert 0.5 < scored["projection"] < 1.5

    def test_the_baseline_is_scored_on_the_same_terms(self):
        """Track position has to be judged as a prediction, not as context."""
        rows = payload(
            entry(1, position=6, projected=1),
            entry(2, position=5, projected=2),
            entry(3, position=4, projected=3),
            entry(4, position=3, projected=4),
            entry(5, position=2, projected=5),
            entry(6, position=1, projected=6),
        )
        scored = calibration._score_lap(rows, ACTUAL)
        assert scored["projection"] == 0.0, "the projection called it exactly"
        assert scored["track_position"] > 2, "the order on the road was reversed"

    def test_the_model_is_only_scored_where_it_ran(self):
        scored = calibration._score_lap(perfect_lap(ACTUAL), ACTUAL)
        assert "model" not in scored

        with_model = payload(
            *[entry(n, position=p, projected=p, model=p) for n, p in ACTUAL.items()]
        )
        assert calibration._score_lap(with_model, ACTUAL)["model"] == 0.0

    def test_a_handful_of_cars_is_not_a_finishing_order(self):
        rows = payload(entry(1, position=1, projected=1), entry(2, position=2, projected=2))
        assert calibration._score_lap(rows, ACTUAL) is None

    def test_cars_missing_from_the_classification_are_left_out(self):
        rows = payload(*[entry(n, position=n, projected=n) for n in range(1, 9)])
        assert calibration._score_lap(rows, ACTUAL)["cars"] == len(ACTUAL)


class TestStages:
    def test_a_lap_lands_in_the_quarter_it_belongs_to(self):
        assert calibration.stage_for(5, 60) == "opening quarter"
        assert calibration.stage_for(20, 60) == "second quarter"
        assert calibration.stage_for(40, 60) == "third quarter"
        assert calibration.stage_for(59, 60) == "final quarter"

    def test_the_last_lap_is_in_the_race(self):
        assert calibration.stage_for(60, 60) == "final quarter"

    def test_a_race_of_no_laps_has_no_stages(self):
        assert calibration.stage_for(1, 0) is None


class TestSummary:
    def test_the_best_method_is_named(self):
        laps = [
            {"stage": "final quarter", "track_position": 3.0, "projection": 1.0, "model": 2.0},
            {"stage": "final quarter", "track_position": 3.0, "projection": 1.0, "model": 2.0},
        ]
        means = calibration._means(laps)
        assert means["best"] == "projection"
        assert means["projection"] == 1.0
        assert means["laps"] == 2

    def test_a_baseline_that_wins_is_reported_as_winning(self):
        """The measure has to be able to say the predictions are not helping."""
        laps = [{"stage": "second quarter", "track_position": 1.0, "projection": 4.0}]
        assert calibration._means(laps)["best"] == "track_position"

    def test_stages_without_scored_laps_are_omitted(self):
        laps = [{"stage": "opening quarter", "track_position": 2.0, "projection": 2.0}]
        stages = calibration._by_stage(laps)
        assert [row["stage"] for row in stages] == ["opening quarter"]

    def test_an_unevaluated_replay_scores_nothing_rather_than_failing(self, db):
        assert calibration.score_replay(db, 987_654)["scored_laps"] == 0

    def test_the_summary_reads_only_what_has_been_stored(self, db):
        store.write(db, 4242, 10, perfect_lap(ACTUAL), model_version=None)
        # No bundle on disk for this key, so there is nothing to score against.
        result = calibration.summary(db, [4242])
        assert result["scored_laps"] == 0
        assert result["races"] == []
