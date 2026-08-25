"""Offline retraining for the qualifying models.

Triggered *after* a qualifying session ends, using that session's real results
as new training data. Runs on the job thread from :mod:`app.training.jobs`;
nothing here is reachable from a live prediction path (rule 1).

Pipeline: ingest completed results -> label stored snapshots -> assemble a
matrix under the transfer table -> fit -> validate on a held-out recent session
-> register behind the validation gate.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.data.fastf1_client import FastF1Client, FastF1Unavailable
from app.db import models as m
from app.db.database import session_scope
from app.features.builder import FeatureBuilder, invalidate_aggregates
from app.features.engineering import QUALIFYING_FEATURES
from app.models import registry
from app.models.qualifying_model import QualifyingAdvancementModel, QualifyingTimeModel
from app.training.dataset import build_training_set, label_qualifying_session
from app.training.jobs import ProgressReporter, submit

log = logging.getLogger(__name__)

QUALIFYING_SESSION_TYPES = {"Q1", "Q2", "Q3", "Qualifying", "SQ"}
PERIODS = ("Q1", "Q2", "Q3")


def trigger(session_id: int) -> dict[str, Any]:
    """Queue a qualifying retrain. Returns immediately with the job record."""
    record = submit("retrain_qualifying", session_id, lambda r: run(session_id, r))
    return record.as_dict()


def run(session_id: int, progress: ProgressReporter) -> dict[str, Any]:
    """The actual batch job. Blocking; job thread only.

    Ingestion/labelling and training are committed separately on purpose: a
    training failure (too few rows early in a season, for instance) must not
    discard the session results that were just ingested, or the corpus would
    never grow past the first session.
    """
    results = _ingest(session_id, progress)

    with session_scope() as db:
        progress("training_qualifying_time")
        results["models"]["qualifying_time"] = _train_time_model(db, progress)

    with session_scope() as db:
        progress("training_qualifying_advancement")
        results["models"]["qualifying_advancement"] = _train_advancement_model(db, progress)

    return results


def _ingest(session_id: int, progress: ProgressReporter) -> dict[str, Any]:
    """Load real results, backfill feature snapshots, and label them. Committed."""
    with session_scope() as db:
        session = db.get(m.Session, session_id)
        if session is None:
            raise LookupError(f"no session with id {session_id}")
        if not m.SessionType(session.session_type).is_qualifying:
            raise ValueError(f"session {session_id} is {session.session_type}, not a qualifying session")

        progress("ingesting_results", {"session": session.session_type, "circuit": session.circuit_id})
        client = FastF1Client(session.year)
        try:
            loaded = client.load_session(session.year, session.circuit.name, "Qualifying")
        except FastF1Unavailable as exc:
            raise RuntimeError(f"cannot retrain without completed results: {exc}") from exc

        session.status = m.SessionStatus.COMPLETED.value
        if loaded.weather:
            session.weather_snapshot = loaded.weather

        progress("backfilling_snapshots")
        builder = FeatureBuilder(db, fastf1=client, season=session.year)
        _ensure_snapshots(db, builder, session)

        progress("labelling")
        labelled = label_qualifying_session(db, session, loaded)
        invalidate_aggregates()
        return {"session_id": session_id, "labelled_rows": labelled, "models": {}}


def _ensure_snapshots(db, builder: FeatureBuilder, session: m.Session) -> None:
    """Guarantee one snapshot per driver per period for a completed session.

    A session that was never watched live has no stored snapshots, so its
    results would be unusable as training data. Building them here is a
    backfill, not a prediction.
    """
    drivers = db.scalars(select(m.Driver)).all()
    for driver in drivers:
        for period in PERIODS:
            builder.build_and_store(session, driver, period)


def _train_time_model(db, progress: ProgressReporter) -> dict[str, Any]:
    dataset = build_training_set(
        db,
        feature_names=QUALIFYING_FEATURES,
        label_getter=lambda s: s.label_qualifying_time_s,
        session_types=QUALIFYING_SESSION_TYPES,
    )
    model = QualifyingTimeModel()
    model.fit(dataset.rows, dataset.labels, dataset.weights)
    report = model.validate(dataset.val_rows, dataset.val_labels)
    progress(
        "validated_qualifying_time",
        {"metric": report.metric, "score": report.score, "rows": len(dataset)},
    )
    record, decision = registry.register(
        db,
        m.ModelType.QUALIFYING_TIME.value,
        model,
        report,
        training_rows=len(dataset),
        regs_regime=settings.CURRENT_REGS_REGIME,
    )
    return {
        "version": record.version,
        "model_id": record.id,
        "validation": report.as_dict(),
        "gate": decision.as_dict(),
        "top_features": list(model.feature_importance())[:5],
    }


def _train_advancement_model(db, progress: ProgressReporter) -> dict[str, Any]:
    dataset = build_training_set(
        db,
        feature_names=QUALIFYING_FEATURES,
        label_getter=lambda s: s.label_advanced,
        session_types=QUALIFYING_SESSION_TYPES,
    )
    model = QualifyingAdvancementModel()
    model.fit(dataset.rows, [1 if v else 0 for v in dataset.labels], dataset.weights)
    report = model.validate(dataset.val_rows, dataset.val_labels)
    progress(
        "validated_qualifying_advancement",
        {"metric": report.metric, "score": report.score, "rows": len(dataset)},
    )
    if report.leakage_suspected:
        # Guide M3: a near-perfect score is a bug report, not a result.
        log.warning(
            "advancement accuracy %.3f is implausible for this problem "
            "(reference range ~0.70-0.75); check for leakage before promoting",
            report.score,
        )
    record, decision = registry.register(
        db,
        m.ModelType.QUALIFYING_ADVANCEMENT.value,
        model,
        report,
        training_rows=len(dataset),
        regs_regime=settings.CURRENT_REGS_REGIME,
    )
    return {
        "version": record.version,
        "model_id": record.id,
        "validation": report.as_dict(),
        "gate": decision.as_dict(),
        "top_features": list(model.feature_importance())[:5],
    }
