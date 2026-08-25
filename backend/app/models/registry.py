"""Model registry: versioning, the validation gate, and activation.

Rules enforced here:

* **Rule 2** -- a new version is written to disk and recorded in the registry,
  but it only becomes active if it beats the currently active version on a
  held-out recent session. A worse model is never auto-promoted; it stays in the
  registry, inactive, for inspection.
* **Regime safety** -- ``load_active`` refuses a model trained under a different
  ``regs_regime`` than the session it is about to serve.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.db import models as m
from app.models.base import TabularModel, ValidationReport, score_is_better
from app.models.qualifying_model import QualifyingAdvancementModel, QualifyingTimeModel
from app.models.race_model import RaceFinishPositionModel, RaceStrategyModel

log = logging.getLogger(__name__)

MODEL_CLASSES: dict[str, type[TabularModel]] = {
    m.ModelType.QUALIFYING_TIME.value: QualifyingTimeModel,
    m.ModelType.QUALIFYING_ADVANCEMENT.value: QualifyingAdvancementModel,
    m.ModelType.RACE_FINISH_POSITION.value: RaceFinishPositionModel,
    m.ModelType.RACE_STRATEGY.value: RaceStrategyModel,
}


class RegimeMismatch(RuntimeError):
    """An active model was trained under a different regulation regime."""


@dataclass
class GateDecision:
    promoted: bool
    reason: str
    candidate_score: float
    active_score: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "promoted": self.promoted,
            "reason": self.reason,
            "candidate_score": self.candidate_score,
            "active_score": self.active_score,
        }


# -- lookups -----------------------------------------------------------------


def active_record(db: DBSession, model_type: str) -> m.PredictionModel | None:
    return db.scalar(
        select(m.PredictionModel).where(
            m.PredictionModel.model_type == model_type,
            m.PredictionModel.is_active.is_(True),
        )
    )


def next_version(db: DBSession, model_type: str) -> int:
    latest = db.scalar(
        select(m.PredictionModel.version)
        .where(m.PredictionModel.model_type == model_type)
        .order_by(m.PredictionModel.version.desc())
        .limit(1)
    )
    return (latest or 0) + 1


def list_models(db: DBSession, model_type: str | None = None) -> list[m.PredictionModel]:
    stmt = select(m.PredictionModel).order_by(
        m.PredictionModel.model_type, m.PredictionModel.version.desc()
    )
    if model_type:
        stmt = stmt.where(m.PredictionModel.model_type == model_type)
    return list(db.scalars(stmt).all())


# -- artifact cache ----------------------------------------------------------

_loaded: dict[tuple[str, int], TabularModel] = {}


def load_active(
    db: DBSession, model_type: str, *, regs_regime: str | None = None
) -> tuple[TabularModel, m.PredictionModel] | None:
    """Load the active model for a type, refusing a cross-regime mismatch."""
    record = active_record(db, model_type)
    if record is None:
        return None
    wanted = regs_regime or settings.CURRENT_REGS_REGIME
    if record.regs_regime != wanted:
        raise RegimeMismatch(
            f"active {model_type} v{record.version} was trained under regime "
            f"{record.regs_regime!r} but is being asked to serve {wanted!r}; "
            "retrain before serving predictions (guide section 8)"
        )
    key = (model_type, record.version)
    cached = _loaded.get(key)
    if cached is None:
        cached = MODEL_CLASSES[model_type].load(Path(record.artifact_path))
        _loaded[key] = cached
    return cached, record


def clear_model_cache() -> None:
    _loaded.clear()


# -- registration + gate -----------------------------------------------------


def artifact_path(model_type: str, version: int) -> Path:
    return settings.MODEL_ARTIFACT_DIR / f"{model_type}_v{version}.joblib"


def register(
    db: DBSession,
    model_type: str,
    model: TabularModel,
    report: ValidationReport,
    *,
    training_rows: int,
    regs_regime: str | None = None,
    auto_promote: bool | None = None,
) -> tuple[m.PredictionModel, GateDecision]:
    """Persist a freshly trained model and run it through the validation gate.

    The model is always recorded. Whether it goes live is the gate's decision --
    and by default, even a passing model waits for an explicit
    ``POST /models/{id}/promote``, keeping promotion a deliberate act.
    """
    version = next_version(db, model_type)
    path = artifact_path(model_type, version)
    model.save(path)

    record = m.PredictionModel(
        model_type=model_type,
        version=version,
        artifact_path=str(path),
        trained_at=datetime.now(timezone.utc),
        validation_score=report.score,
        validation_metric=report.metric,
        validation_detail=report.as_dict(),
        training_rows=training_rows,
        is_active=False,
        regs_regime=regs_regime or settings.CURRENT_REGS_REGIME,
    )
    db.add(record)
    db.flush()

    decision = evaluate_gate(db, model_type, model, report)
    should_promote = auto_promote if auto_promote is not None else settings.AUTO_PROMOTE
    if decision.promoted and should_promote:
        promote(db, record.id)
    elif decision.promoted:
        log.info(
            "%s v%s passed the validation gate but auto-promotion is off; "
            "promote it explicitly via POST /models/%s/promote",
            model_type, version, record.id,
        )
    else:
        log.warning("%s v%s not promoted: %s", model_type, version, decision.reason)
    return record, decision


def evaluate_gate(
    db: DBSession, model_type: str, model: TabularModel, report: ValidationReport
) -> GateDecision:
    """Compare a candidate against the active version on held-out results."""
    if report.n_rows == 0:
        return GateDecision(False, "no held-out rows to validate against", report.score, None)
    if report.leakage_suspected:
        return GateDecision(
            False,
            f"validation score {report.score:.4f} is implausibly good -- "
            "check for data leakage before promoting (guide M3)",
            report.score,
            None,
        )

    active = active_record(db, model_type)
    if active is None:
        return GateDecision(True, "no active model for this type", report.score, None)
    if active.regs_regime != settings.CURRENT_REGS_REGIME:
        return GateDecision(
            True,
            f"active model was trained under regime {active.regs_regime!r}; "
            "a current-regime model supersedes it",
            report.score,
            active.validation_score,
        )

    better = score_is_better(
        report.score,
        active.validation_score,
        higher_is_better=model.higher_is_better,
        margin=settings.VALIDATION_MIN_IMPROVEMENT,
    )
    direction = "higher" if model.higher_is_better else "lower"
    return GateDecision(
        better,
        (
            f"candidate {report.metric}={report.score:.4f} "
            f"{'beats' if better else 'does not beat'} active v{active.version} "
            f"({active.validation_score}); {direction} is better"
        ),
        report.score,
        active.validation_score,
    )


def promote(db: DBSession, model_id: int) -> m.PredictionModel:
    """Flip ``is_active`` to this model, deactivating its siblings.

    Exactly one model per type is active; the swap happens in one transaction so
    a live request can never observe zero or two active models.
    """
    record = db.get(m.PredictionModel, model_id)
    if record is None:
        raise LookupError(f"no model with id {model_id}")

    for sibling in db.scalars(
        select(m.PredictionModel).where(
            m.PredictionModel.model_type == record.model_type,
            m.PredictionModel.is_active.is_(True),
        )
    ).all():
        sibling.is_active = False
    record.is_active = True
    db.flush()
    clear_model_cache()
    log.info("promoted %s v%s (id=%s) to active", record.model_type, record.version, record.id)
    return record
