"""Shared XGBoost model wrapper.

XGBoost for all tabular predictions (guide section 3). The 2026 corpus is small
by construction -- a season's worth of sessions -- so the defaults here are
deliberately conservative: shallow trees, strong regularisation, few estimators.
A model that cannot overfit 400 rows is the point, not a limitation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np

log = logging.getLogger(__name__)

#: Hyperparameters tuned for a small-N, low-signal-to-noise tabular problem.
SMALL_DATA_PARAMS: dict[str, Any] = {
    "n_estimators": 300,
    "max_depth": 4,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 3,
    "reg_lambda": 2.0,
    "random_state": 42,
    "n_jobs": 2,
}


class ModelNotTrained(RuntimeError):
    pass


@dataclass
class ValidationReport:
    metric: str
    score: float
    n_rows: int
    detail: dict[str, Any] = field(default_factory=dict)
    #: Set when the score is implausibly good -- almost always leakage (M3).
    leakage_suspected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "score": self.score,
            "n_rows": self.n_rows,
            "leakage_suspected": self.leakage_suspected,
            **self.detail,
        }


class TabularModel:
    """Base class: feature ordering, fit, predict, persistence.

    Feature order is stored *with* the artifact. A model must never be fed
    columns in a different order than it was trained on, and the only reliable
    way to guarantee that is to carry the ordering along with the weights.
    """

    #: Higher score is better for classifiers, lower for regressors.
    higher_is_better = True
    metric_name = "score"

    def __init__(self, feature_names: list[str], params: dict[str, Any] | None = None) -> None:
        self.feature_names = list(feature_names)
        self.params = {**SMALL_DATA_PARAMS, **(params or {})}
        self.estimator: Any | None = None
        self.classes_: list[Any] | None = None

    # -- construction ------------------------------------------------------
    def _build_estimator(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def fit(
        self,
        rows: list[dict[str, float]],
        labels: list[Any],
        sample_weight: list[float] | None = None,
    ) -> "TabularModel":
        X = self.to_matrix(rows)
        y = np.asarray(labels)
        self.estimator = self._build_estimator()
        weights = np.asarray(sample_weight) if sample_weight is not None else None
        self.estimator.fit(X, y, sample_weight=weights)
        return self

    # -- inference ---------------------------------------------------------
    def to_matrix(self, rows: list[dict[str, float]]) -> np.ndarray:
        if not rows:
            return np.empty((0, len(self.feature_names)), dtype=float)
        return np.asarray(
            [[float(row.get(name, 0.0)) for name in self.feature_names] for row in rows],
            dtype=float,
        )

    def _require_fitted(self) -> Any:
        if self.estimator is None:
            raise ModelNotTrained(f"{type(self).__name__} has not been trained or loaded")
        return self.estimator

    def predict(self, rows: list[dict[str, float]]) -> np.ndarray:
        return self._require_fitted().predict(self.to_matrix(rows))

    def predict_proba(self, rows: list[dict[str, float]]) -> np.ndarray:
        estimator = self._require_fitted()
        if not hasattr(estimator, "predict_proba"):
            raise ModelNotTrained(f"{type(self).__name__} is not a classifier")
        return estimator.predict_proba(self.to_matrix(rows))

    def feature_importance(self) -> dict[str, float]:
        estimator = self._require_fitted()
        importances = getattr(estimator, "feature_importances_", None)
        if importances is None:
            return {}
        return dict(sorted(zip(self.feature_names, (float(i) for i in importances)),
                           key=lambda kv: kv[1], reverse=True))

    # -- validation --------------------------------------------------------
    def validate(self, rows: list[dict[str, float]], labels: list[Any]) -> ValidationReport:  # pragma: no cover
        raise NotImplementedError

    # -- persistence -------------------------------------------------------
    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        # ``estimator`` may legitimately be None for a degenerate model (a single
        # observed class), which still needs to round-trip through the registry.
        joblib.dump(
            {
                "class": type(self).__name__,
                "feature_names": self.feature_names,
                "params": self.params,
                "estimator": self.estimator,
                "classes": self.classes_,
            },
            path,
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "TabularModel":
        payload = joblib.load(path)
        model = cls(payload["feature_names"], payload.get("params"))
        model.estimator = payload["estimator"]
        model.classes_ = payload.get("classes")
        return model


def score_is_better(score: float, baseline: float | None, *, higher_is_better: bool, margin: float = 0.0) -> bool:
    """Validation-gate comparison (rule 2): never promote a worse model."""
    if baseline is None:
        return True
    return score >= baseline + margin if higher_is_better else score <= baseline - margin
