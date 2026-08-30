"""Race models: finishing position, and pit-strategy probabilities."""
from __future__ import annotations

from typing import Any

import numpy as np

from app.config import settings
from app.features.engineering import RACE_FEATURES
from app.models.base import TabularModel, ValidationReport

#: Strategy label vocabulary. Kept explicit rather than free-form so the label
#: space is stable across retrains and the dashboard can render it directly.
STRATEGY_LABELS = [
    "1-stop-medium-hard",
    "1-stop-hard-medium",
    "1-stop-soft-hard",
    "2-stop-soft-medium-soft",
    "2-stop-medium-hard-medium",
    "2-stop-soft-hard-soft",
    "3-plus-stop",
    "wet-variable",
]


class RaceFinishPositionModel(TabularModel):
    """Regressor over final classified position.

    Position rather than gap: it is what the dashboard shows, it is robust to
    safety-car-compressed gaps, and it needs no distance normalisation between
    circuits.
    """

    higher_is_better = False
    metric_name = "position_mae"

    def __init__(self, feature_names: list[str] | None = None, params: dict[str, Any] | None = None) -> None:
        super().__init__(feature_names or RACE_FEATURES, params)

    def _build_estimator(self) -> Any:
        from xgboost import XGBRegressor

        return XGBRegressor(objective="reg:squarederror", **self.params)

    def validate(self, rows: list[dict[str, float]], labels: list[Any]) -> ValidationReport:
        y_true = np.asarray(labels, dtype=float)
        if not len(y_true):
            return ValidationReport(self.metric_name, float("inf"), 0)
        y_pred = np.asarray(self.predict(rows), dtype=float)
        mae = float(np.mean(np.abs(y_true - y_pred)))
        within_one = float(np.mean(np.abs(y_true - y_pred) <= 1.0))
        # Podium-style classification is the guide's sanity reference (~high 80s%).
        podium_true = y_true <= 3
        podium_pred = y_pred <= 3.5
        podium_accuracy = float(np.mean(podium_true == podium_pred))
        return ValidationReport(
            metric=self.metric_name,
            score=mae,
            n_rows=len(y_true),
            detail={
                "within_one_position": within_one,
                "podium_accuracy": podium_accuracy,
            },
            leakage_suspected=mae < 0.1 and len(y_true) > 10,
        )


class RaceStrategyModel(TabularModel):
    """Multiclass classifier over :data:`STRATEGY_LABELS`.

    The label *vocabulary* is fixed, but any single season only contains some of
    it -- a dry-weather season never produces a ``wet-variable`` row. XGBoost's
    sklearn API requires contiguous classes, so the model encodes against the
    labels actually present in the training data and stores that mapping in
    ``classes_``; probabilities are reported back under their real names, and
    strategies never seen in training simply carry no mass.
    """

    higher_is_better = True
    metric_name = "accuracy"

    def __init__(self, feature_names: list[str] | None = None, params: dict[str, Any] | None = None) -> None:
        super().__init__(feature_names or RACE_FEATURES, params)
        self.classes_ = []

    def _build_estimator(self) -> Any:
        from xgboost import XGBClassifier

        return XGBClassifier(
            objective="multi:softprob",
            num_class=len(self.classes_ or STRATEGY_LABELS),
            eval_metric="mlogloss",
            **self.params,
        )

    @staticmethod
    def normalise_label(label: str) -> str:
        """Map any label onto the known vocabulary."""
        return label if label in STRATEGY_LABELS else "3-plus-stop"

    def fit(
        self,
        rows: list[dict[str, float]],
        labels: list[Any],
        sample_weight: list[float] | None = None,
    ) -> "RaceStrategyModel":
        normalised = [self.normalise_label(str(label)) for label in labels]
        present = sorted(set(normalised), key=STRATEGY_LABELS.index)
        if len(present) < 2:
            # One observed strategy: nothing to discriminate. Keep the model
            # usable as a constant predictor rather than failing the retrain.
            self.classes_ = present or ["1-stop-medium-hard"]
            self.estimator = None
            self._constant_only = True
            return self
        self._constant_only = False
        self.classes_ = present
        encoded = [present.index(label) for label in normalised]
        super().fit(rows, encoded, sample_weight)
        return self

    _constant_only = False

    def predict_proba(self, rows: list[dict[str, float]]) -> np.ndarray:
        if self._constant_only:
            return np.ones((len(rows), 1), dtype=float)
        return super().predict_proba(rows)

    def strategy_probabilities(self, rows: list[dict[str, float]]) -> list[dict[str, float]]:
        """Per-row ``{strategy: probability}``, largest first, small tails dropped."""
        proba = self.predict_proba(rows)
        labels = self.classes_ or STRATEGY_LABELS
        out: list[dict[str, float]] = []
        for row in proba:
            pairs = sorted(zip(labels, (float(p) for p in row)), key=lambda kv: kv[1], reverse=True)
            out.append({label: round(p, 4) for label, p in pairs if p >= 0.01})
        return out

    @classmethod
    def load(cls, path: Any) -> "RaceStrategyModel":
        model = super().load(path)  # type: ignore[assignment]
        model._constant_only = model.estimator is None
        return model  # type: ignore[return-value]

    def most_likely(self, rows: list[dict[str, float]]) -> list[str]:
        labels = self.classes_ or STRATEGY_LABELS
        return [labels[int(np.argmax(row))] for row in self.predict_proba(rows)]

    def validate(self, rows: list[dict[str, float]], labels: list[Any]) -> ValidationReport:
        known = self.classes_ or STRATEGY_LABELS
        y_true = [self.normalise_label(str(label)) for label in labels]
        if not y_true:
            return ValidationReport(self.metric_name, 0.0, 0)
        proba = self.predict_proba(rows)
        predicted = [known[int(np.argmax(row))] for row in proba]
        accuracy = float(np.mean([p == t for p, t in zip(predicted, y_true)]))
        top2 = float(
            np.mean(
                [
                    true in [known[i] for i in np.argsort(row)[-2:]]
                    for true, row in zip(y_true, proba)
                ]
            )
        )
        # A strategy the model never saw in training cannot be predicted; report
        # it so a validation score is read in the right context.
        unseen = sorted(set(y_true) - set(known))
        return ValidationReport(
            metric=self.metric_name,
            score=accuracy,
            n_rows=len(y_true),
            detail={
                "top2_accuracy": top2,
                "n_classes_trained": len(known),
                "unseen_labels": unseen,
            },
            leakage_suspected=accuracy >= settings.LEAKAGE_SUSPICION_SCORE and len(y_true) > 10,
        )


def strategy_label_from_stints(compounds: list[str], *, was_wet: bool = False) -> str:
    """Derive the strategy label a driver actually ran, from their stint list."""
    if was_wet or any(c.upper() in {"WET", "INTERMEDIATE"} for c in compounds if c):
        return "wet-variable"
    clean = [c.upper()[0] for c in compounds if c]  # S / M / H
    stops = max(0, len(clean) - 1)
    if stops >= 3:
        return "3-plus-stop"
    mapping = {
        ("M", "H"): "1-stop-medium-hard",
        ("H", "M"): "1-stop-hard-medium",
        ("S", "H"): "1-stop-soft-hard",
        ("S", "M", "S"): "2-stop-soft-medium-soft",
        ("M", "H", "M"): "2-stop-medium-hard-medium",
        ("S", "H", "S"): "2-stop-soft-hard-soft",
    }
    return mapping.get(tuple(clean), "1-stop-medium-hard" if stops <= 1 else "3-plus-stop")
