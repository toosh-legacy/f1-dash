"""Stored per-driver predictions, and the race-control log beside them."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app import schemas
from app.api.deps import latest_per_driver, require_session
from app.db import models as m
from app.db.database import get_db

router = APIRouter()


@router.get(
    "/sessions/{session_id}/qualifying/predictions",
    response_model=list[schemas.QualifyingPredictionOut],
    tags=["predictions"],
)
def qualifying_predictions(
    session_id: int,
    period: str | None = Query(default=None, pattern="^(Q1|Q2|Q3)$"),
    db: DBSession = Depends(get_db),
):
    """Latest qualifying predictions -- one row per driver, most recent first."""
    require_session(db, session_id)
    stmt = (
        select(m.QualifyingPrediction)
        .where(m.QualifyingPrediction.session_id == session_id)
        .order_by(m.QualifyingPrediction.created_at.desc(), m.QualifyingPrediction.id.desc())
    )
    if period:
        stmt = stmt.where(m.QualifyingPrediction.period == period)
    return latest_per_driver(db.scalars(stmt).all())


@router.get(
    "/sessions/{session_id}/race/predictions",
    response_model=list[schemas.RacePredictionOut],
    tags=["predictions"],
)
def race_predictions(
    session_id: int,
    lap: int | None = Query(default=None, ge=1),
    db: DBSession = Depends(get_db),
):
    require_session(db, session_id)
    stmt = (
        select(m.RacePrediction)
        .where(m.RacePrediction.session_id == session_id)
        .order_by(m.RacePrediction.lap_number.desc(), m.RacePrediction.id.desc())
    )
    if lap is not None:
        stmt = stmt.where(m.RacePrediction.lap_number == lap)
    rows = latest_per_driver(db.scalars(stmt).all())
    rows.sort(
        key=lambda r: (
            r.predicted_finish_position is None,
            r.predicted_finish_position or 0.0,
        )
    )
    return rows


@router.get(
    "/sessions/{session_id}/race_control",
    response_model=list[schemas.RaceControlEventOut],
    tags=["predictions"],
)
def race_control_events(session_id: int, db: DBSession = Depends(get_db)):
    require_session(db, session_id)
    return db.scalars(
        select(m.RaceControlEvent)
        .where(m.RaceControlEvent.session_id == session_id)
        .order_by(m.RaceControlEvent.created_at.desc())
        .limit(50)
    ).all()
