"""Turning stored feature snapshots into labelled training matrices.

Two jobs live here:

1. **Labelling** -- once a session completes, its real results are written back
   onto the :class:`FeatureSnapshot` rows that were used to predict it. That is
   what makes a prediction system self-feeding: today's prediction inputs are
   tomorrow's training data.
2. **Assembly** -- building ``(rows, labels, weights)`` while applying the
   transfer table (rule 3). Rows from an earlier regime are dropped when the
   matrix contains any regime-only feature, and down-weighted when it contains
   only weak-transfer ones.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.data.fastf1_client import LoadedSession
from app.db import models as m
from app.features.transfer import row_sample_weight
from app.models.qualifying_model import advancement_label
from app.models.race_model import strategy_label_from_stints

log = logging.getLogger(__name__)


class InsufficientTrainingData(RuntimeError):
    """Not enough labelled rows to train, or no held-out session to validate on."""


@dataclass
class TrainingSet:
    rows: list[dict[str, float]]
    labels: list[Any]
    weights: list[float]
    val_rows: list[dict[str, float]]
    val_labels: list[Any]
    #: Session held out for validation -- always the most recent one, so the
    #: gate answers "is this better on the newest data", not "on a random slice".
    holdout_session_id: int | None

    def __len__(self) -> int:
        return len(self.rows)


# -- labelling ---------------------------------------------------------------


def label_qualifying_session(db: DBSession, session: m.Session, loaded: LoadedSession) -> int:
    """Write real qualifying results back onto this session's snapshots."""
    by_driver = {r.driver_id: r for r in loaded.results}
    updated = 0
    for snapshot in _snapshots_for(db, session.id):
        result = by_driver.get(snapshot.driver_id)
        if result is None:
            continue
        period = snapshot.context if snapshot.context in {"Q1", "Q2", "Q3"} else "Q1"
        period_time = {"Q1": result.q1_s, "Q2": result.q2_s, "Q3": result.q3_s}.get(period)
        snapshot.label_qualifying_time_s = period_time or result.best_lap_s
        snapshot.label_advanced = advancement_label(result.position, period)
        updated += 1
    db.flush()
    log.info("labelled %s qualifying snapshots for session %s", updated, session.id)
    return updated


def label_race_session(db: DBSession, session: m.Session, loaded: LoadedSession) -> int:
    """Write finishing positions and actual strategies onto this session's snapshots."""
    by_driver = {r.driver_id: r for r in loaded.results}
    compounds: dict[str, list[str]] = {}
    for stint in sorted(loaded.stints, key=lambda s: (s.driver_id, s.stint_number)):
        if stint.compound:
            compounds.setdefault(stint.driver_id, []).append(stint.compound)

    was_wet = bool(loaded.weather.get("rainfall"))
    updated = 0
    for snapshot in _snapshots_for(db, session.id):
        result = by_driver.get(snapshot.driver_id)
        if result is None:
            continue
        # A DNF has no meaningful finishing position; park it at the back of the
        # field rather than dropping the row, so reliability stays learnable.
        snapshot.label_finish_position = (
            float(len(by_driver)) if result.is_dnf else result.position
        )
        snapshot.label_strategy = strategy_label_from_stints(
            compounds.get(snapshot.driver_id, []), was_wet=was_wet
        )
        updated += 1
    db.flush()
    log.info("labelled %s race snapshots for session %s", updated, session.id)
    return updated


def _snapshots_for(db: DBSession, session_id: int) -> list[m.FeatureSnapshot]:
    return list(
        db.scalars(
            select(m.FeatureSnapshot).where(m.FeatureSnapshot.session_id == session_id)
        ).all()
    )


# -- assembly ----------------------------------------------------------------


def build_training_set(
    db: DBSession,
    *,
    feature_names: list[str],
    label_getter: Callable[[m.FeatureSnapshot], Any],
    session_types: set[str],
    min_rows: int = 40,
    holdout_session_id: int | None = None,
) -> TrainingSet:
    """Assemble a labelled matrix, applying the transfer table row by row."""
    stmt = (
        select(m.FeatureSnapshot, m.Session)
        .join(m.Session, m.Session.id == m.FeatureSnapshot.session_id)
        .where(m.Session.session_type.in_(session_types))
        .order_by(m.Session.start_time.asc().nullslast(), m.FeatureSnapshot.id.asc())
    )
    pairs = list(db.execute(stmt).all())
    if not pairs:
        raise InsufficientTrainingData(
            f"no feature snapshots stored for session types {sorted(session_types)}"
        )

    # Hold out the most recent session unless the caller names one.
    if holdout_session_id is None:
        holdout_session_id = pairs[-1][1].id

    rows: list[dict[str, float]] = []
    labels: list[Any] = []
    weights: list[float] = []
    val_rows: list[dict[str, float]] = []
    val_labels: list[Any] = []
    dropped_regime = 0

    for snapshot, session in pairs:
        label = label_getter(snapshot)
        if label is None:
            continue
        row = {name: float(snapshot.features.get(name, 0.0)) for name in feature_names}

        if session.id == holdout_session_id:
            val_rows.append(row)
            val_labels.append(label)
            continue

        weight = row_sample_weight(snapshot.regs_regime, feature_names)
        if weight <= 0.0:
            dropped_regime += 1
            continue
        rows.append(row)
        labels.append(label)
        weights.append(weight)

    if dropped_regime:
        log.info(
            "dropped %s pre-%s rows: the feature set contains regime-only features "
            "(guide section 2 transfer table)",
            dropped_regime,
            settings.CURRENT_REGS_REGIME,
        )
    if len(rows) < min_rows:
        raise InsufficientTrainingData(
            f"only {len(rows)} labelled training rows available (need {min_rows}); "
            f"ingest more {settings.CURRENT_SEASON} sessions first"
        )
    if not val_rows:
        raise InsufficientTrainingData(
            "held-out session produced no labelled rows; the validation gate "
            "cannot run without one (guide rule 2)"
        )

    return TrainingSet(
        rows=rows,
        labels=labels,
        weights=weights,
        val_rows=val_rows,
        val_labels=val_labels,
        holdout_session_id=holdout_session_id,
    )
