"""FastAPI application: REST + WebSocket routes (guide section 9).

Rule 1 is enforced at the routing layer: ``POST /sessions/{id}/retrain`` hands
the work to a background thread and returns a job record immediately. No route
in this file blocks on training, and no route trains inline.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app import schemas
from app.config import settings
from app.data import seed as seeding
from app.data.fastf1_client import FastF1Client, FastF1Unavailable
from app.db import models as m
from app.db.database import get_db, init_db
from app.live.base import loops
from app.live.broadcast import broadcaster
from app.live.qualifying_loop import QualifyingLoop
from app.live.race_loop import RaceLoop
from app.models import registry
from app.training import jobs
from app.training import retrain_qualifying, retrain_race

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("app.main")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Background threads publish into this loop; capture it once at startup.
    broadcaster.bind_loop(asyncio.get_running_loop())
    log.info("API ready (regime %s, season %s)", settings.CURRENT_REGS_REGIME, settings.CURRENT_SEASON)
    try:
        yield
    finally:
        loops.stop_all()
        jobs.shutdown()


app = FastAPI(
    title="F1 Live Prediction Dashboard",
    version="1.0.0",
    description=(
        "Per-car live predictions for the current F1 season. Retraining is a "
        "batch job; live inference never triggers training."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- health ------------------------------------------------------------------


@app.get("/health", response_model=schemas.HealthOut, tags=["meta"])
def health(db: DBSession = Depends(get_db)) -> schemas.HealthOut:
    active = {
        row.model_type: row.version
        for row in db.scalars(
            select(m.PredictionModel).where(m.PredictionModel.is_active.is_(True))
        ).all()
    }
    return schemas.HealthOut(
        status="ok",
        regs_regime=settings.CURRENT_REGS_REGIME,
        season=settings.CURRENT_SEASON,
        database=settings.DATABASE_URL.split("://")[0],
        active_models=active,
        live_loops=len(loops.statuses()),
        pending_jobs=jobs.pending_count(),
    )


# -- reference data ----------------------------------------------------------


@app.get("/circuits", response_model=list[schemas.CircuitOut], tags=["reference"])
def list_circuits(db: DBSession = Depends(get_db)):
    return db.scalars(select(m.Circuit).order_by(m.Circuit.name)).all()


@app.get("/drivers", response_model=list[schemas.DriverOut], tags=["reference"])
def list_drivers(db: DBSession = Depends(get_db)):
    return db.scalars(select(m.Driver).order_by(m.Driver.driver_number)).all()


@app.post("/seed", tags=["reference"])
def seed(request: schemas.SeedRequest, db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """Seed circuits, calendar, teams and drivers from real season data (M1)."""
    try:
        result = seeding.seed_all(db, request.year, request.limit_events)
    except (RuntimeError, FastF1Unavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.commit()
    return result


# -- sessions ----------------------------------------------------------------


@app.get("/sessions", response_model=list[schemas.SessionOut], tags=["sessions"])
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


@app.get("/sessions/{session_id}", response_model=schemas.SessionOut, tags=["sessions"])
def get_session(session_id: int, db: DBSession = Depends(get_db)):
    return _require_session(db, session_id)


@app.post("/sessions/{session_id}/retrain", response_model=schemas.JobOut, tags=["training"])
def retrain(session_id: int, db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """Queue the offline retraining pipeline. Returns immediately (rule 1)."""
    session = _require_session(db, session_id)
    session_type = m.SessionType(session.session_type)
    if session_type.is_qualifying:
        return retrain_qualifying.trigger(session_id)
    if session_type.is_race:
        return retrain_race.trigger(session_id)
    raise HTTPException(
        status_code=400,
        detail=f"{session.session_type} sessions are not a training target; "
        "retrain from a qualifying or race session",
    )


@app.get("/jobs/{job_id}", response_model=schemas.JobOut, tags=["training"])
def get_job(job_id: str) -> dict[str, Any]:
    record = jobs.jobs.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return record.as_dict()


@app.get("/jobs", response_model=list[schemas.JobOut], tags=["training"])
def list_jobs(limit: int = Query(default=20, le=50)) -> list[dict[str, Any]]:
    return [record.as_dict() for record in jobs.jobs.recent(limit)]


# -- practice ----------------------------------------------------------------


@app.get("/sessions/{session_id}/practice", response_model=schemas.PracticeSummaryOut, tags=["predictions"])
def practice_summary(session_id: int, db: DBSession = Depends(get_db)):
    """Current-form summary for a practice session -- not a forward prediction."""
    session = _require_session(db, session_id)
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


# -- predictions -------------------------------------------------------------


@app.get(
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
    _require_session(db, session_id)
    stmt = (
        select(m.QualifyingPrediction)
        .where(m.QualifyingPrediction.session_id == session_id)
        .order_by(m.QualifyingPrediction.created_at.desc(), m.QualifyingPrediction.id.desc())
    )
    if period:
        stmt = stmt.where(m.QualifyingPrediction.period == period)
    return _latest_per_driver(db.scalars(stmt).all())


@app.get(
    "/sessions/{session_id}/race/predictions",
    response_model=list[schemas.RacePredictionOut],
    tags=["predictions"],
)
def race_predictions(
    session_id: int,
    lap: int | None = Query(default=None, ge=1),
    db: DBSession = Depends(get_db),
):
    _require_session(db, session_id)
    stmt = (
        select(m.RacePrediction)
        .where(m.RacePrediction.session_id == session_id)
        .order_by(m.RacePrediction.lap_number.desc(), m.RacePrediction.id.desc())
    )
    if lap is not None:
        stmt = stmt.where(m.RacePrediction.lap_number == lap)
    rows = _latest_per_driver(db.scalars(stmt).all())
    rows.sort(
        key=lambda r: (
            r.predicted_finish_position is None,
            r.predicted_finish_position or 0.0,
        )
    )
    return rows


@app.get(
    "/sessions/{session_id}/race_control",
    response_model=list[schemas.RaceControlEventOut],
    tags=["predictions"],
)
def race_control_events(session_id: int, db: DBSession = Depends(get_db)):
    _require_session(db, session_id)
    return db.scalars(
        select(m.RaceControlEvent)
        .where(m.RaceControlEvent.session_id == session_id)
        .order_by(m.RaceControlEvent.created_at.desc())
        .limit(50)
    ).all()


# -- model registry ----------------------------------------------------------


@app.get("/models", response_model=list[schemas.PredictionModelOut], tags=["models"])
def list_models(model_type: str | None = None, db: DBSession = Depends(get_db)):
    return registry.list_models(db, model_type)


@app.post("/models/{model_id}/promote", response_model=schemas.PromoteResponse, tags=["models"])
def promote_model(model_id: int, db: DBSession = Depends(get_db)):
    """Flip ``is_active`` after manual review -- deliberately separate from retraining."""
    previous = None
    record = db.get(m.PredictionModel, model_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no model {model_id}")
    if record.regs_regime != settings.CURRENT_REGS_REGIME:
        raise HTTPException(
            status_code=409,
            detail=(
                f"model was trained under regime {record.regs_regime!r}, current regime is "
                f"{settings.CURRENT_REGS_REGIME!r}; promoting it would serve predictions "
                "from a superseded set of regulations"
            ),
        )
    active = registry.active_record(db, record.model_type)
    if active is not None:
        previous = active.version
    promoted = registry.promote(db, model_id)
    db.commit()
    return schemas.PromoteResponse(
        id=promoted.id,
        model_type=promoted.model_type,
        version=promoted.version,
        is_active=promoted.is_active,
        previous_active_version=previous,
    )


# -- live loop control -------------------------------------------------------


@app.get("/live", response_model=list[schemas.LiveLoopOut], tags=["live"])
def list_live_loops():
    return loops.statuses()


@app.post("/sessions/{session_id}/live/start", response_model=schemas.LiveLoopOut, tags=["live"])
def start_live_loop(
    session_id: int, request: schemas.StartLoopRequest, db: DBSession = Depends(get_db)
):
    """Start the polling loop for a session (qualifying or race cadence)."""
    session = _require_session(db, session_id)
    session_key = request.openf1_session_key or session.openf1_session_key
    if session_key is None:
        raise HTTPException(
            status_code=400,
            detail="no OpenF1 session key: pass openf1_session_key, or store one on the session",
        )
    session.openf1_session_key = session_key
    db.commit()

    session_type = m.SessionType(session.session_type)
    kwargs: dict[str, Any] = {}
    if request.poll_interval_s:
        kwargs["poll_interval_s"] = request.poll_interval_s
    if session_type.is_qualifying:
        loop = QualifyingLoop(session_id, session_key, **kwargs)
    elif session_type.is_race:
        loop = RaceLoop(session_id, session_key, total_laps=request.total_laps, **kwargs)
    else:
        raise HTTPException(
            status_code=400, detail=f"{session.session_type} has no live prediction loop"
        )
    return loops.start(loop).status.as_dict()


@app.post("/sessions/{session_id}/live/stop", tags=["live"])
def stop_live_loop(session_id: int) -> dict[str, Any]:
    stopped = loops.stop(session_id)
    if not stopped:
        raise HTTPException(status_code=404, detail=f"no live loop running for session {session_id}")
    return {"session_id": session_id, "stopped": True}


# -- websocket ---------------------------------------------------------------


@app.websocket("/sessions/{session_id}/live")
async def live_socket(websocket: WebSocket, session_id: int) -> None:
    """One message per period boundary (qualifying) or per lap (race), plus
    immediate ``race_control`` messages on any state change."""
    await websocket.accept()
    queue = await broadcaster.subscribe(session_id)
    await websocket.send_json(
        {
            "type": "connected",
            "session_id": session_id,
            "regs_regime": settings.CURRENT_REGS_REGIME,
            "server_time": datetime.now(timezone.utc).isoformat(),
        }
    )
    try:
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    except RuntimeError:  # socket closed mid-send
        pass
    finally:
        await broadcaster.unsubscribe(session_id, queue)
        with suppress(Exception):
            await websocket.close()


# -- dashboard ---------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(str(STATIC_DIR / "dashboard.html"))


# -- helpers -----------------------------------------------------------------


def _require_session(db: DBSession, session_id: int) -> m.Session:
    session = db.get(m.Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no session with id {session_id}")
    return session


def _latest_per_driver(rows: list[Any]) -> list[Any]:
    """Keep only the newest row per driver; input must be newest-first."""
    seen: set[str] = set()
    latest = []
    for row in rows:
        if row.driver_id in seen:
            continue
        seen.add(row.driver_id)
        latest.append(row)
    return latest
