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
from fastapi import Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app import schemas
from app.config import settings
from app.data import replay
from app.data import seed as seeding
from app.data.fastf1_client import FastF1Client, FastF1Unavailable
from app.db import models as m
from app.db import projection_store
from app.db import replay_store
from app.db import session_index
from app.db.database import get_db, init_db, session_scope
from app.live.base import loops
from app.live.broadcast import broadcaster
from app.live.qualifying_loop import QualifyingLoop
from app.live.race_loop import RaceLoop
from app.models import calibration
from app.models import registry
from app.models import replay_predictor
from app.training import jobs
from app.training import retrain_qualifying, retrain_race

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("app.main")

STATIC_DIR = Path(__file__).parent / "static"

#: A live loop that has not completed a poll in this long is not keeping up
#: with a session, whatever its thread says about being alive.
STALE_POLL_S = 120.0


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
    statuses = loops.statuses()
    trained = db.scalars(
        select(m.PredictionModel).order_by(m.PredictionModel.trained_at.desc()).limit(1)
    ).first()
    stale = [s for s in statuses if (s.get("last_poll_age_s") or 0) > STALE_POLL_S]
    failing = [s for s in statuses if (s.get("consecutive_errors") or 0) >= 3]

    return schemas.HealthOut(
        status="degraded" if (stale or failing) else "ok",
        regs_regime=settings.CURRENT_REGS_REGIME,
        season=settings.CURRENT_SEASON,
        database=settings.DATABASE_URL.split("://")[0],
        active_models=active,
        live_loops=len(statuses),
        pending_jobs=jobs.pending_count(),
        live=statuses,
        jobs=jobs.stats(),
        caches={
            "replay_bundles": replay.bundle_cache_stats(),
            "scores": calibration.cache_stats(),
            "session_index": session_index.status(db),
        },
        last_trained_at=trained.trained_at.isoformat() if trained and trained.trained_at else None,
    )


@app.get("/now", tags=["meta"])
def now(db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """What the dashboard should open on when somebody hits Connect.

    In order of interest: a session being polled right now, otherwise the next
    session on the calendar whatever its kind -- a Friday practice is what a
    viewer wants on a Friday -- and, either way, the most recent race with a
    replay built, since that is the one thing always worth showing.
    """
    moment = datetime.now(timezone.utc)

    running = loops.statuses()
    live = None
    if running:
        status = running[0]
        session = db.get(m.Session, status.get("session_id"))
        live = {
            "session_id": status.get("session_id"),
            "openf1_session_key": status.get("session_key"),
            "kind": status.get("kind"),
            "context": status.get("last_context"),
            **_session_brief(session),
        }

    upcoming = db.scalars(
        select(m.Session)
        .where(m.Session.start_time.isnot(None), m.Session.start_time >= moment)
        .order_by(m.Session.start_time.asc())
        .limit(1)
    ).first()

    previous = db.scalars(
        select(m.Session)
        .where(m.Session.start_time.isnot(None), m.Session.start_time < moment)
        .order_by(m.Session.start_time.desc())
        .limit(1)
    ).first()

    built = replay_store.catalogue_rows(db)
    return {
        "server_time": moment.isoformat(),
        "regs_regime": settings.CURRENT_REGS_REGIME,
        "season": settings.CURRENT_SEASON,
        "live": live,
        "next_session": _session_brief(upcoming),
        "last_session": _session_brief(previous),
        "latest_replay": built[0] if built else None,
        "replays_built": len(built),
    }


def _session_brief(session: m.Session | None) -> dict[str, Any]:
    if session is None:
        return {}
    return {
        "session_id": session.id,
        "circuit_id": session.circuit_id,
        "circuit": session.circuit.name if session.circuit else session.circuit_id,
        "session_type": session.session_type,
        "start_time": session.start_time.isoformat() if session.start_time else None,
        "status": session.status,
        "openf1_session_key": session.openf1_session_key,
    }


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


# -- replays -----------------------------------------------------------------


@app.get("/replays", response_model=list[schemas.ReplayRoundOut], tags=["replays"])
def list_replays(
    year: int | None = None, sprints: bool = True, db: DBSession = Depends(get_db)
) -> list[dict[str, Any]]:
    """Every race that has already run, and whether its replay is built.

    Served from the local session index rather than from OpenF1. The index
    refreshes itself behind the response when it goes stale, so opening the
    dashboard does not wait on a third party -- see
    :mod:`app.db.session_index`.
    """
    try:
        rounds = session_index.catalogue(db, year, include_sprints=sprints)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"replay catalogue unavailable: {exc}")

    shared = replay_store.available(db)
    listed = []
    for entry in rounds:
        row = entry.as_dict()
        # "Built" means built by anyone: the database is the shared library and
        # this server may simply not have fetched a copy yet.
        row["cached"] = row["cached"] or entry.session_key in shared
        listed.append(row)
    return listed


@app.post("/replays/{session_key}/build", response_model=schemas.JobOut, tags=["replays"])
def build_replay(session_key: int, force: bool = False) -> dict[str, Any]:
    """Queue a replay build.

    Fetching a session's position feed is one request per car over tens of
    thousands of samples, so it goes to the same background pool retraining
    uses and the caller gets a job record straight back.
    """
    if not force and _ensure_local(session_key):
        return jobs.jobs.create("replay", None).as_dict() | {
            "status": "succeeded",
            "result": {"session_key": session_key, "cached": True},
        }

    def run(progress: jobs.ProgressReporter) -> dict[str, Any]:
        bundle = replay.build_bundle(
            session_key,
            force=force,
            progress=lambda stage, detail=None: progress(stage, detail),
        )
        # Publishing is the point: one person's build is everyone's replay.
        with session_scope() as db:
            size = replay_store.publish(db, session_key, bundle)
        progress("published", {"session_key": session_key, "bytes": size})
        return {
            "session_key": session_key,
            "frames": len(bundle["frames"]),
            "total_laps": bundle["total_laps"],
            "bytes": size,
        }

    return jobs.submit("replay", None, run).as_dict()


@app.get("/replays/{session_key}", tags=["replays"])
def get_replay(session_key: int) -> FileResponse:
    """The built bundle.

    Served as the stored gzip rather than re-encoded: it is several megabytes
    of JSON uncompressed and the browser unwraps it for free.
    """
    if not _ensure_local(session_key):
        raise HTTPException(
            status_code=404,
            detail=f"replay for session {session_key} is not built yet; POST to /replays/{session_key}/build",
        )
    path = replay.bundle_path(session_key)
    return FileResponse(
        str(path),
        media_type="application/json",
        headers={"content-encoding": "gzip", "cache-control": "public, max-age=86400"},
    )


@app.get("/replays/{session_key}/meta", tags=["replays"])
def get_replay_meta(session_key: int) -> Response:
    """Everything about a replay except the playback frames.

    The circuit, the drivers, the per-lap order, the race-control log, the
    overtakes and the classification: 36 KB against the bundle's 475 KB. A
    dashboard can draw the race from this and fetch the frames behind it,
    rather than showing nothing until half a megabyte has arrived.
    """
    return _bundle_part_response(session_key, "meta")


@app.get("/replays/{session_key}/frames", tags=["replays"])
def get_replay_frames(session_key: int) -> Response:
    """Just the playback frames -- the other 439 KB."""
    return _bundle_part_response(session_key, "frames")


def _bundle_part_response(session_key: int, part: str) -> Response:
    if not _ensure_local(session_key):
        raise HTTPException(
            status_code=404,
            detail=f"replay for session {session_key} is not built yet; POST to /replays/{session_key}/build",
        )
    data = replay.bundle_part(session_key, part)
    if data is None:
        raise HTTPException(status_code=404, detail=f"replay for session {session_key} is not built yet")
    return Response(
        content=data,
        media_type="application/json",
        headers={"content-encoding": "gzip", "cache-control": "public, max-age=86400"},
    )


# The bundle and a projection are deep, wide payloads whose shape belongs to
# the replay format rather than to the API; they are returned as-is rather than
# mirrored in a schema that would have to be kept in step with every field.
@app.get("/replays/{session_key}/projection", tags=["replays"])
def replay_projection(
    session_key: int,
    lap: int = Query(ge=1),
    fresh: bool = False,
    db: DBSession = Depends(get_db),
) -> dict[str, Any]:
    """Where the race is heading as of ``lap`` -- the prediction panel's data.

    Served from the projection cache when the lap has already been evaluated
    under the active model; computed and stored on a miss. ``fresh=true``
    recomputes regardless, which is how a cached answer gets checked.

    A cache hit answers without the bundle. The session key is in the path, the
    cache is keyed by it, and the stored row is the whole response -- so the
    common case is one indexed row read rather than unzipping and parsing a
    race to reach a value that was already computed.
    """
    if not fresh:
        cached = projection_store.read(
            db, session_key, lap, replay_predictor.active_model_version(db)
        )
        if cached is not None:
            cached["cached"] = True
            return cached

    _ensure_local(session_key)
    bundle = replay.load_bundle(session_key)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"replay for session {session_key} is not built yet")
    result = replay_predictor.projection_for(db, bundle, lap, use_cache=not fresh)
    if not result.get("cached"):
        # A read path that computed something expensive keeps it: the write is
        # the whole point of asking once.
        db.commit()
    return result


@app.post("/replays/{session_key}/predict", response_model=schemas.JobOut, tags=["replays"])
def evaluate_replay(session_key: int, model_id: int | None = None) -> dict[str, Any]:
    """Run a prediction model over every lap of a replay, and cache it.

    A batch pass, on the same background pool as retraining: after it, the
    prediction panel reads rows instead of running models.

    ``model_id`` evaluates under a model that is not the active one. Its
    answers are stored beside the incumbent's rather than replacing them --
    the cache is keyed by model version -- so ``GET /models/compare`` can then
    score the two against the same finishing order.
    """
    if not _ensure_local(session_key):
        raise HTTPException(
            status_code=409,
            detail=f"replay for session {session_key} is not built yet; build it first",
        )

    def run(progress: jobs.ProgressReporter) -> dict[str, Any]:
        return replay_predictor.run(session_key, progress, model_id=model_id)

    return jobs.submit("predict", None, run).as_dict()


@app.get(
    "/replays/{session_key}/predictions",
    response_model=schemas.ProjectionCoverageOut,
    tags=["replays"],
)
def replay_prediction_coverage(
    session_key: int, db: DBSession = Depends(get_db)
) -> dict[str, Any]:
    """What has already been evaluated for a replay, per model version."""
    return projection_store.coverage(db, session_key) | {
        "active_model_version": replay_predictor.active_model_version(db),
        "built": replay.is_cached(session_key),
    }


@app.get("/layouts/{circuit}", tags=["replays"])
def circuit_layout(circuit: str, db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """The drawn shape of a circuit, from whichever race happens to have one.

    A race that has not run has no position feed and so no geometry of its own,
    but a circuit does not change between visits: the layout from any built
    replay at the same track is the same layout. This is what lets an upcoming
    round show its map instead of an apology.
    """
    wanted = circuit.strip().lower()
    for row in replay_store.catalogue_rows(db):
        name = (row.get("circuit") or "").lower()
        if not name or (wanted not in name and name not in wanted):
            continue
        if not _ensure_local(row["session_key"]):
            continue
        bundle = replay.load_bundle(row["session_key"])
        if bundle is None:
            continue
        return {
            "circuit": row.get("circuit"),
            "track": bundle["track"],
            "source_session_key": row["session_key"],
            "source_date": row.get("date_start"),
        }
    raise HTTPException(
        status_code=404,
        detail=f"no built replay carries a layout for {circuit}; build any race there first",
    )


@app.get("/replays/{session_key}/accuracy", tags=["replays"])
def replay_accuracy(session_key: int, db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """How close this replay's stored projections were to the classification.

    Scores three answers side by side -- where the car was at the time, the
    projection, and the trained model -- so the prediction has to earn its
    place against simply reading the running order.
    """
    return calibration.score_replay(db, session_key)


@app.get("/models/compare", tags=["models"])
def compare_models(db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """Score every evaluated model against the others on the laps they share.

    Promotion is deliberate here, and this is the evidence for it: run a
    challenger over a replay with ``POST /replays/{key}/predict?model_id=N``,
    then read this to see whether it was actually closer to the finishing order
    than the model already serving.
    """
    return calibration.compare_models(db)


@app.get("/predictions/accuracy", tags=["replays"])
def prediction_accuracy(db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """The same measure across every replay that has been evaluated."""
    return calibration.summary(db)


@app.delete(
    "/replays/{session_key}/predictions",
    response_model=schemas.ClearedOut,
    tags=["replays"],
)
def clear_replay_predictions(
    session_key: int, db: DBSession = Depends(get_db)
) -> dict[str, Any]:
    """Drop a replay's cached projections, so the next run recomputes them."""
    removed = projection_store.clear(db, session_key)
    db.commit()
    return {"session_key": session_key, "cleared": removed}


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


def _ensure_local(session_key: int) -> bool:
    """Make sure this server has the replay file, fetching the shared copy.

    A build belongs to everyone, so "is it built?" is a question about the
    database, not about this machine's disk.
    """
    if replay.is_cached(session_key):
        return True
    with session_scope() as db:
        return replay_store.hydrate(db, session_key)


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
