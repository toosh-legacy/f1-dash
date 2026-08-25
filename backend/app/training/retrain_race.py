"""Offline retraining for the race models (finish position + strategy).

Same shape as :mod:`app.training.retrain_qualifying`, with a per-lap snapshot
grid instead of per-period: a race prediction depends on lap number, tyre age
and track position, so the training rows have to span the race, not just its
start.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.data.fastf1_client import FastF1Client, FastF1Unavailable, LoadedSession
from app.db import models as m
from app.db.database import session_scope
from app.features.builder import FeatureBuilder, invalidate_aggregates
from app.features.engineering import LiveState, RACE_FEATURES
from app.models import registry
from app.models.race_model import RaceFinishPositionModel, RaceStrategyModel
from app.training.dataset import build_training_set, label_race_session
from app.training.jobs import ProgressReporter, submit

log = logging.getLogger(__name__)

RACE_SESSION_TYPES = {"Race", "Sprint"}

#: Snapshots are taken every N laps when backfilling a completed race. Every lap
#: would be ~20x more rows that are near-duplicates of their neighbours; every
#: fifth lap keeps the race arc without drowning the small 2026 corpus in
#: correlated rows.
BACKFILL_LAP_STEP = 5


def trigger(session_id: int) -> dict[str, Any]:
    record = submit("retrain_race", session_id, lambda r: run(session_id, r))
    return record.as_dict()


def run(session_id: int, progress: ProgressReporter) -> dict[str, Any]:
    """Batch job; job thread only.

    As with qualifying, ingestion is committed before training runs, so a
    training failure never throws away the results just ingested.
    """
    results = _ingest(session_id, progress)

    with session_scope() as db:
        progress("training_race_finish_position")
        results["models"]["race_finish_position"] = _train_finish_model(db, progress)

    with session_scope() as db:
        progress("training_race_strategy")
        results["models"]["race_strategy"] = _train_strategy_model(db, progress)

    return results


def _ingest(session_id: int, progress: ProgressReporter) -> dict[str, Any]:
    with session_scope() as db:
        session = db.get(m.Session, session_id)
        if session is None:
            raise LookupError(f"no session with id {session_id}")
        if not m.SessionType(session.session_type).is_race:
            raise ValueError(f"session {session_id} is {session.session_type}, not a race session")

        progress("ingesting_results", {"circuit": session.circuit_id})
        client = FastF1Client(session.year)
        try:
            loaded = client.load_session(session.year, session.circuit.name, session.session_type)
        except FastF1Unavailable as exc:
            raise RuntimeError(f"cannot retrain without completed results: {exc}") from exc

        session.status = m.SessionStatus.COMPLETED.value
        if loaded.weather:
            session.weather_snapshot = loaded.weather

        progress("recording_race_control")
        recorded = _record_race_control(db, session, loaded)

        progress("backfilling_snapshots")
        builder = FeatureBuilder(db, fastf1=client, season=session.year)
        deg_rate = _median_degradation(loaded)
        snapshots = _backfill_race_snapshots(db, builder, session, loaded, deg_rate)

        progress("labelling")
        labelled = label_race_session(db, session, loaded)
        invalidate_aggregates()

        return {
            "session_id": session_id,
            "snapshots": snapshots,
            "labelled_rows": labelled,
            "race_control_events": recorded,
            "tyre_deg_rate_2026": deg_rate,
            "models": {},
        }


def _median_degradation(loaded: LoadedSession) -> float | None:
    """Circuit tyre-degradation rate derived from **this season's** stints only.

    New tyre dimensions reset the degradation curve's shape, so this is computed
    fresh from 2026 data rather than carried over (guide section 2).
    """
    rates = sorted(s.degradation_s_per_lap for s in loaded.stints if s.degradation_s_per_lap)
    if not rates:
        return None
    mid = len(rates) // 2
    return rates[mid] if len(rates) % 2 else (rates[mid - 1] + rates[mid]) / 2


def _record_race_control(db, session: m.Session, loaded: LoadedSession) -> int:
    """Persist historical race-control events so gating is replayable (M6)."""
    from app.live.race_control import classify_fastf1_message

    existing = db.scalar(
        select(m.RaceControlEvent.id).where(m.RaceControlEvent.session_id == session.id).limit(1)
    )
    if existing:
        return 0
    count = 0
    for message in loaded.race_control:
        event_type = classify_fastf1_message(message)
        if event_type is None:
            continue
        db.add(
            m.RaceControlEvent(
                session_id=session.id,
                event_type=event_type.value,
                lap_number=message.get("lap_number"),
                message=str(message.get("message", ""))[:512],
            )
        )
        count += 1
    db.flush()
    return count


def _backfill_race_snapshots(
    db, builder: FeatureBuilder, session: m.Session, loaded: LoadedSession, deg_rate: float | None
) -> int:
    """Reconstruct per-lap feature snapshots for a completed race."""
    drivers = {d.id: d for d in db.scalars(select(m.Driver)).all()}
    total_laps = max((s.lap_end or 0) for s in loaded.stints) if loaded.stints else 0
    if not total_laps:
        total_laps = 57  # a nominal race distance when stint data is unavailable

    stints_by_driver: dict[str, list] = {}
    for stint in loaded.stints:
        stints_by_driver.setdefault(stint.driver_id, []).append(stint)

    count = 0
    for result in loaded.results:
        driver = drivers.get(result.driver_id)
        if driver is None:
            continue
        for lap in range(1, total_laps + 1, BACKFILL_LAP_STEP):
            stint = _stint_at_lap(stints_by_driver.get(result.driver_id, []), lap)
            live = LiveState(
                lap_number=lap,
                total_laps=total_laps,
                position=int(result.grid_position) if result.grid_position else None,
                compound=stint.compound if stint else None,
                tyre_age_laps=(lap - (stint.lap_start or lap)) if stint else None,
                compound_deg_rate=stint.degradation_s_per_lap if stint else deg_rate,
            )
            builder.build_and_store(
                session, driver, f"lap_{lap}", live=live, tyre_deg_rate=deg_rate
            )
            count += 1
    return count


def _stint_at_lap(stints: list, lap: int):
    for stint in stints:
        if (stint.lap_start or 0) <= lap <= (stint.lap_end or 0):
            return stint
    return None


def _train_finish_model(db, progress: ProgressReporter) -> dict[str, Any]:
    dataset = build_training_set(
        db,
        feature_names=RACE_FEATURES,
        label_getter=lambda s: s.label_finish_position,
        session_types=RACE_SESSION_TYPES,
    )
    model = RaceFinishPositionModel()
    model.fit(dataset.rows, dataset.labels, dataset.weights)
    report = model.validate(dataset.val_rows, dataset.val_labels)
    progress("validated_race_finish", {"metric": report.metric, "score": report.score})
    record, decision = registry.register(
        db,
        m.ModelType.RACE_FINISH_POSITION.value,
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


def _train_strategy_model(db, progress: ProgressReporter) -> dict[str, Any]:
    dataset = build_training_set(
        db,
        feature_names=RACE_FEATURES,
        label_getter=lambda s: s.label_strategy,
        session_types=RACE_SESSION_TYPES,
    )
    model = RaceStrategyModel()
    model.fit(dataset.rows, dataset.labels, dataset.weights)
    report = model.validate(dataset.val_rows, dataset.val_labels)
    progress("validated_race_strategy", {"metric": report.metric, "score": report.score})
    record, decision = registry.register(
        db,
        m.ModelType.RACE_STRATEGY.value,
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
