"""The calendar: listing sessions, and one session's practice form."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app import schemas
from app.api.deps import require_session
from app.data.fastf1_client import FastF1Client, FastF1Unavailable
from app.db import models as m
from app.db.database import get_db

router = APIRouter()


@router.get("/sessions", response_model=list[schemas.SessionOut], tags=["sessions"])
def list_sessions(
    year: int | None = None,
    circuit_id: str | None = None,
    session_type: str | None = None,
    status: str | None = None,
    db: DBSession = Depends(get_db),
):
    stmt = select(m.Session).order_by(m.Session.start_time.asc().nullslast(), m.Session.id)
    if year is not None:
        stmt = stmt.where(m.Session.year == year)
    if circuit_id:
        stmt = stmt.where(m.Session.circuit_id == circuit_id)
    if session_type:
        stmt = stmt.where(m.Session.session_type == session_type)
    if status:
        stmt = stmt.where(m.Session.status == status)
    return db.scalars(stmt).all()


@router.get("/sessions/{session_id}", response_model=schemas.SessionOut, tags=["sessions"])
def get_session(session_id: int, db: DBSession = Depends(get_db)):
    return require_session(db, session_id)


@router.get("/sessions/{session_id}/practice", response_model=schemas.PracticeSummaryOut, tags=["predictions"])
def practice_summary(session_id: int, db: DBSession = Depends(get_db)):
    """Current-form summary for a practice session -- not a forward prediction."""
    session = require_session(db, session_id)
    if not m.SessionType(session.session_type).is_practice:
        raise HTTPException(status_code=400, detail=f"{session.session_type} is not a practice session")

    client = FastF1Client(session.year)
    try:
        loaded = client.load_session(session.year, session.circuit.name, session.session_type)
    except FastF1Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    times = [r.best_lap_s for r in loaded.results if r.best_lap_s]
    best = min(times) if times else None
    laps_by_driver: dict[str, int] = {}
    compound_by_driver: dict[str, str | None] = {}
    for stint in loaded.stints:
        span = (stint.lap_end or 0) - (stint.lap_start or 0) + 1
        laps_by_driver[stint.driver_id] = laps_by_driver.get(stint.driver_id, 0) + max(0, span)
        compound_by_driver[stint.driver_id] = stint.compound

    drivers = [
        schemas.PracticeDriverForm(
            driver_id=r.driver_id,
            driver_name=r.driver_name,
            team=r.team_name,
            best_lap_s=r.best_lap_s,
            gap_to_best_s=round(r.best_lap_s - best, 3) if r.best_lap_s and best else None,
            laps_completed=laps_by_driver.get(r.driver_id, 0),
            compound=compound_by_driver.get(r.driver_id),
        )
        for r in loaded.results
    ]
    drivers.sort(key=lambda d: (d.best_lap_s is None, d.best_lap_s))
    return schemas.PracticeSummaryOut(
        session_id=session_id, session_type=session.session_type, drivers=drivers
    )
