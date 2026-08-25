"""The model registry and its validation gate (rule 2)."""
from __future__ import annotations

import random

import pytest

from app.db import models as m
from app.features.engineering import QUALIFYING_FEATURES
from app.models import registry
from app.models.base import ValidationReport, score_is_better
from app.models.qualifying_model import QualifyingAdvancementModel, advancement_label
from app.models.race_model import RaceStrategyModel, strategy_label_from_stints


def _rows(n: int = 60) -> tuple[list[dict[str, float]], list[int]]:
    """Synthetic but learnable: fast cars with good drivers advance."""
    random.seed(7)
    rows, labels = [], []
    for _ in range(n):
        pace = random.random()
        driver = random.random()
        row = {name: 0.0 for name in QUALIFYING_FEATURES}
        row["team_quali_pace_percentile"] = pace
        row["driver_historical_baseline"] = driver
        rows.append(row)
        labels.append(1 if (0.7 * pace + 0.3 * driver) > 0.5 else 0)
    return rows, labels


class TestGate:
    def test_first_model_passes_with_no_incumbent(self, db):
        model = QualifyingAdvancementModel()
        report = ValidationReport("accuracy", 0.72, 20)
        decision = registry.evaluate_gate(db, m.ModelType.QUALIFYING_ADVANCEMENT.value, model, report)
        assert decision.promoted

    def test_worse_candidate_is_rejected(self, db):
        rows, labels = _rows()
        model = QualifyingAdvancementModel().fit(rows, labels)
        good = ValidationReport("accuracy", 0.80, 20)
        record, decision = registry.register(
            db, m.ModelType.QUALIFYING_ADVANCEMENT.value, model, good, training_rows=len(rows)
        )
        registry.promote(db, record.id)

        worse = ValidationReport("accuracy", 0.61, 20)
        decision = registry.evaluate_gate(db, m.ModelType.QUALIFYING_ADVANCEMENT.value, model, worse)
        assert not decision.promoted
        assert "does not beat" in decision.reason

    def test_better_candidate_passes(self, db):
        rows, labels = _rows()
        model = QualifyingAdvancementModel().fit(rows, labels)
        record, _ = registry.register(
            db,
            m.ModelType.QUALIFYING_ADVANCEMENT.value,
            model,
            ValidationReport("accuracy", 0.70, 20),
            training_rows=len(rows),
        )
        registry.promote(db, record.id)
        decision = registry.evaluate_gate(
            db,
            m.ModelType.QUALIFYING_ADVANCEMENT.value,
            model,
            ValidationReport("accuracy", 0.78, 20),
        )
        assert decision.promoted

    def test_implausible_score_is_treated_as_leakage(self, db):
        model = QualifyingAdvancementModel()
        report = ValidationReport("accuracy", 0.995, 40, leakage_suspected=True)
        decision = registry.evaluate_gate(db, m.ModelType.QUALIFYING_ADVANCEMENT.value, model, report)
        assert not decision.promoted
        assert "leakage" in decision.reason

    def test_regressor_gate_prefers_lower_error(self):
        assert score_is_better(0.12, 0.20, higher_is_better=False)
        assert not score_is_better(0.31, 0.20, higher_is_better=False)

    def test_registration_does_not_auto_activate(self, db):
        rows, labels = _rows()
        model = QualifyingAdvancementModel().fit(rows, labels)
        record, decision = registry.register(
            db,
            m.ModelType.QUALIFYING_ADVANCEMENT.value,
            model,
            ValidationReport("accuracy", 0.74, 20),
            training_rows=len(rows),
        )
        assert decision.promoted, "gate should pass"
        assert not record.is_active, "promotion stays a deliberate, separate action"


class TestPromotion:
    def test_exactly_one_active_per_model_type(self, db):
        rows, labels = _rows()
        model = QualifyingAdvancementModel().fit(rows, labels)
        first, _ = registry.register(
            db, m.ModelType.QUALIFYING_ADVANCEMENT.value, model,
            ValidationReport("accuracy", 0.70, 20), training_rows=len(rows),
        )
        second, _ = registry.register(
            db, m.ModelType.QUALIFYING_ADVANCEMENT.value, model,
            ValidationReport("accuracy", 0.76, 20), training_rows=len(rows),
        )
        registry.promote(db, first.id)
        registry.promote(db, second.id)

        active = [
            r for r in registry.list_models(db, m.ModelType.QUALIFYING_ADVANCEMENT.value) if r.is_active
        ]
        assert len(active) == 1
        assert active[0].id == second.id

    def test_versions_increment(self, db):
        rows, labels = _rows()
        model = QualifyingAdvancementModel().fit(rows, labels)
        versions = [
            registry.register(
                db, m.ModelType.QUALIFYING_TIME.value, model,
                ValidationReport("mae_s", 0.2, 20), training_rows=len(rows),
            )[0].version
            for _ in range(3)
        ]
        assert versions == sorted(versions) and len(set(versions)) == 3

    def test_cross_regime_model_is_refused_at_serve_time(self, db):
        rows, labels = _rows()
        model = QualifyingAdvancementModel().fit(rows, labels)
        record, _ = registry.register(
            db, m.ModelType.QUALIFYING_ADVANCEMENT.value, model,
            ValidationReport("accuracy", 0.72, 20), training_rows=len(rows),
            regs_regime="2025",
        )
        registry.promote(db, record.id)
        with pytest.raises(registry.RegimeMismatch):
            registry.load_active(db, m.ModelType.QUALIFYING_ADVANCEMENT.value, regs_regime="2026")

    def test_saved_model_round_trips(self, db):
        rows, labels = _rows()
        model = QualifyingAdvancementModel().fit(rows, labels)
        record, _ = registry.register(
            db, m.ModelType.QUALIFYING_ADVANCEMENT.value, model,
            ValidationReport("accuracy", 0.72, 20), training_rows=len(rows),
        )
        registry.promote(db, record.id)
        loaded, loaded_record = registry.load_active(db, m.ModelType.QUALIFYING_ADVANCEMENT.value)
        assert loaded_record.id == record.id
        assert loaded.feature_names == model.feature_names
        assert loaded.advancement_probability(rows[:3]) == pytest.approx(
            model.advancement_probability(rows[:3]), abs=1e-6
        )


class TestLabels:
    def test_advancement_cutoffs(self):
        assert advancement_label(15, "Q1") is True
        assert advancement_label(16, "Q1") is False
        assert advancement_label(10, "Q2") is True
        assert advancement_label(11, "Q2") is False
        assert advancement_label(1, "Q3") is None

    def test_strategy_labels_from_stints(self):
        assert strategy_label_from_stints(["MEDIUM", "HARD"]) == "1-stop-medium-hard"
        assert strategy_label_from_stints(["SOFT", "MEDIUM", "SOFT"]) == "2-stop-soft-medium-soft"
        assert strategy_label_from_stints(["SOFT", "MEDIUM", "HARD", "SOFT"]) == "3-plus-stop"
        assert strategy_label_from_stints(["INTERMEDIATE", "MEDIUM"]) == "wet-variable"

    def test_strategy_model_learns_a_separable_signal(self):
        random.seed(3)
        rows, labels = [], []
        from app.features.engineering import RACE_FEATURES

        for _ in range(80):
            deg = random.random()
            row = {name: 0.0 for name in RACE_FEATURES}
            row["circuit_tyre_deg_rate"] = deg
            rows.append(row)
            labels.append("2-stop-soft-medium-soft" if deg > 0.5 else "1-stop-medium-hard")
        model = RaceStrategyModel().fit(rows, labels)
        report = model.validate(rows, labels)
        assert report.score > 0.8
        probabilities = model.strategy_probabilities(rows[:1])[0]
        assert abs(sum(probabilities.values()) - 1.0) < 0.1
