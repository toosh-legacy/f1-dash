"""Qualifying models: predicted lap time, and probability of advancing a period."""
from __future__ import annotations

from typing import Any

import numpy as np

from app.config import settings
from app.features.engineering import QUALIFYING_FEATURES
from app.models.base import TabularModel, ValidationReport

# Cars eliminated at the end of each period under the standard format.
PERIOD_CUTOFF = {"Q1": 15, "Q2": 10, "Q3": 10}


class QualifyingTimeModel(TabularModel):
    """Regressor over best lap time in seconds."""

    higher_is_better = False
    metric_name = "mae_s"

    def __init__(self, feature_names: list[str] | None = None, params: dict[str, Any] | None = None) -> None:
        super().__init__(feature_names or QUALIFYING_FEATURES, params)

    def _build_estimator(self) -> Any:
        from xgboost import XGBRegressor

        return XGBRegressor(objective="reg:squarederror", **self.params)

    def validate(self, rows: list[dict[str, float]], labels: list[Any]) -> ValidationReport:
        y_true = np.asarray(labels, dtype=float)
        y_pred = np.asarray(self.predict(rows), dtype=float)
        mae = float(np.mean(np.abs(y_true - y_pred))) if len(y_true) else float("inf")
        rmse = float(np.sqrt(np.mean((y_true - y_pred) ** 2))) if len(y_true) else float("inf")
        # Ordering matters more than absolute time for a grid prediction.
        order_true = np.argsort(np.argsort(y_true))
        order_pred = np.argsort(np.argsort(y_pred))
        order_mae = float(np.mean(np.abs(order_true - order_pred))) if len(y_true) else float("inf")
        return ValidationReport(
            metric=self.metric_name,
            score=mae,
            n_rows=len(y_true),
            detail={"rmse_s": rmse, "grid_order_mae": order_mae},
            # Sub-10ms MAE on held-out qualifying is not a good model, it is a
            # leaked lap time.
            leakage_suspected=mae < 0.01 and len(y_true) > 5,
        )


class QualifyingAdvancementModel(TabularModel):
    """Classifier: does this car advance out of its current period?

    Reference accuracy for this shape of problem is ~70-75% (guide M3). A score
    near 99% is a leakage bug until proven otherwise.
    """

    higher_is_better = True
    metric_name = "accuracy"

    def __init__(self, feature_names: list[str] | None = None, params: dict[str, Any] | None = None) -> None:
        super().__init__(feature_names or QUALIFYING_FEATURES, params)
        self.classes_ = [0, 1]

    def _build_estimator(self) -> Any:
        from xgboost import XGBClassifier

        return XGBClassifier(
            objective="binary:logistic", eval_metric="logloss", **self.params
        )

    def advancement_probability(self, rows: list[dict[str, float]]) -> list[float]:
        proba = self.predict_proba(rows)
        return [float(p[1]) for p in proba]

    def validate(self, rows: list[dict[str, float]], labels: list[Any]) -> ValidationReport:
        y_true = np.asarray([1 if bool(v) else 0 for v in labels])
        if not len(y_true):
            return ValidationReport(self.metric_name, 0.0, 0)
        proba = np.asarray(self.advancement_probability(rows))
        y_pred = (proba >= 0.5).astype(int)
        accuracy = float(np.mean(y_pred == y_true))
        # Brier score: calibration matters, since the UI shows the probability.
        brier = float(np.mean((proba - y_true) ** 2))
        base_rate = float(np.mean(y_true))
        return ValidationReport(
            metric=self.metric_name,
            score=accuracy,
            n_rows=len(y_true),
            detail={
                "brier": brier,
                "base_rate": base_rate,
                "lift_over_majority": accuracy - max(base_rate, 1 - base_rate),
            },
            leakage_suspected=accuracy >= settings.LEAKAGE_SUSPICION_SCORE and len(y_true) > 10,
        )


def advancement_label(position: float | None, period: str) -> bool | None:
    """Did a car finishing at ``position`` in ``period`` advance?"""
    if position is None or period not in PERIOD_CUTOFF:
        return None
    if period == "Q3":
        return None  # nothing to advance to
    return position <= PERIOD_CUTOFF[period]
