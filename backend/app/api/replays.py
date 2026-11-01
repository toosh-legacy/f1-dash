"""Replays: the catalogue, the bundles, and what was predicted over them."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session as DBSession

from app import schemas
from app.api.deps import (
    BUNDLE_HEADERS,
    ensure_local,
    gzipped_json,
    require_built_replay,
)
from app.data import replay
from app.data.replay import telemetry
from app.db import projection_store
from app.db import replay_store
from app.db import session_index
from app.db.database import get_db, session_scope
from app.models import calibration
from app.models import replay_predictor
from app.training import jobs

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/replays", response_model=list[schemas.ReplayRoundOut], tags=["replays"])
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


#: The schedule changes a few times a season; ten minutes is fresh enough and
#: keeps opening the race picker from costing an OpenF1 request each time.
_SCHEDULE_TTL_S = 600
_schedule_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}


@router.get("/schedule", tags=["replays"])
def schedule(year: int | None = None) -> list[dict[str, Any]]:
    """Sessions still to come this season, soonest first, from OpenF1.

    The local calendar is seeded and can run out before the season does;
    OpenF1 publishes the whole weekend schedule ahead of time, so the race
    picker's countdowns read from it instead.
    """
    import time
    from datetime import datetime, timezone

    from app.config import settings
    from app.data.openf1_client import OpenF1Client
    from app.data.replay.shared import _parse_date

    year = year or settings.CURRENT_SEASON
    cached = _schedule_cache.get(year)
    if cached is None or time.monotonic() - cached[0] > _SCHEDULE_TTL_S:
        try:
            with OpenF1Client() as client:
                rows = client.sessions(year=year)
        except Exception as exc:
            if cached is None:
                raise HTTPException(status_code=502, detail=f"schedule unavailable: {exc}")
            rows = cached[1]   # stale beats nothing
        _schedule_cache[year] = (time.monotonic(), rows)
    rows = _schedule_cache[year][1]

    now = datetime.now(timezone.utc)
    upcoming = []
    for row in rows:
        start = _parse_date(row.get("date_start"))
        end = _parse_date(row.get("date_end"))
        if start is None or row.get("is_cancelled"):
            continue
        if (end or start) < now:
            continue
        upcoming.append({
            "session_key": row.get("session_key"),
            "name": row.get("session_name"),
            "type": row.get("session_type"),
            "circuit": row.get("circuit_short_name"),
            "country": row.get("country_name"),
            "date_start": row.get("date_start"),
            "date_end": row.get("date_end"),
            "running": start <= now,
        })
    upcoming.sort(key=lambda r: r["date_start"])
    return upcoming


@router.post("/replays/{session_key}/build", response_model=schemas.JobOut, tags=["replays"])
def build_replay(session_key: int, force: bool = False) -> dict[str, Any]:
    """Queue a replay build.

    Fetching a session's position feed is one request per car over tens of
    thousands of samples, so it goes to the same background pool retraining
    uses and the caller gets a job record straight back.
    """
    if not force and ensure_local(session_key):
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
        # The first race built at a circuit maps its surroundings. Best
        # effort: a replay without a map still plays.
        try:
            from app.data.circuits import build as circuit_builder

            if circuit_builder.ensure_map(bundle):
                progress("mapped", {"circuit": bundle["session"].get("circuit")})
        except Exception as error:  # noqa: BLE001 - never fail a build over its map
            log.warning("mapping the circuit of %s failed: %s", session_key, error)
        return {
            "session_key": session_key,
            "frames": len(bundle["frames"]),
            "total_laps": bundle["total_laps"],
            "bytes": size,
        }

    return jobs.submit("replay", None, run).as_dict()


@router.get("/replays/{session_key}", tags=["replays"])
def get_replay(session_key: int) -> FileResponse:
    """The built bundle.

    Served as the stored gzip rather than re-encoded: it is several megabytes
    of JSON uncompressed and the browser unwraps it for free.
    """
    require_built_replay(session_key)
    return FileResponse(
        str(replay.bundle_path(session_key)),
        media_type="application/json",
        headers=BUNDLE_HEADERS,
    )


@router.get("/replays/{session_key}/meta", tags=["replays"])
def get_replay_meta(session_key: int) -> Response:
    """Everything about a replay except the playback frames.

    The circuit, the drivers, the per-lap order, the race-control log, the
    overtakes and the classification: 36 KB against the bundle's 475 KB. A
    dashboard can draw the race from this and fetch the frames behind it,
    rather than showing nothing until half a megabyte has arrived.
    """
    return _bundle_part_response(session_key, "meta")


@router.get("/replays/{session_key}/frames", tags=["replays"])
def get_replay_frames(session_key: int) -> Response:
    """Just the playback frames -- the other 439 KB."""
    return _bundle_part_response(session_key, "frames")


@router.get("/replays/{session_key}/telemetry/{number}", tags=["replays"])
def get_replay_telemetry(
    session_key: int, number: int, t: float = Query(ge=0)
) -> dict[str, Any]:
    """One car's telemetry for the window of replay time containing ``t``.

    Fetched from OpenF1 on first ask and kept, so following a car costs one
    upstream request per two minutes of race. ``rows`` are
    ``[t, speed_kph, gear, throttle_pct, brake_pct, rpm]``; ``from``/``to``
    bound the window so the caller knows when to ask for the next.
    """
    require_built_replay(session_key)
    bundle = replay.load_bundle(session_key)
    t0 = telemetry.session_t0(bundle or {})
    if t0 is None:
        raise HTTPException(status_code=404, detail="replay has no start time to align telemetry to")
    index = telemetry.window_index(t)
    try:
        rows = telemetry.telemetry_window(session_key, number, index, t0)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"telemetry unavailable: {exc}")
    return {
        "number": number,
        "from": index * telemetry.WINDOW_S,
        "to": (index + 1) * telemetry.WINDOW_S,
        "rows": rows,
    }


def _bundle_part_response(session_key: int, part: str) -> Response:
    """Serve one slice of a built bundle -- the meta half or the frames half."""
    require_built_replay(session_key)
    data = replay.bundle_part(session_key, part)
    if data is None:
        raise HTTPException(
            status_code=404, detail=f"replay for session {session_key} has no {part}"
        )
    return gzipped_json(data)


# The bundle and a projection are deep, wide payloads whose shape belongs to
# the replay format rather than to the API; they are returned as-is rather than


@router.get("/replays/{session_key}/projection", tags=["replays"])
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

    require_built_replay(session_key)
    bundle = replay.load_bundle(session_key)
    if bundle is None:
        raise HTTPException(
            status_code=404, detail=f"replay for session {session_key} could not be read"
        )
    result = replay_predictor.projection_for(db, bundle, lap, use_cache=not fresh)
    if not result.get("cached"):
        # A read path that computed something expensive keeps it: the write is
        # the whole point of asking once.
        db.commit()
    return result


@router.post("/replays/{session_key}/predict", response_model=schemas.JobOut, tags=["replays"])
def evaluate_replay(session_key: int, model_id: int | None = None) -> dict[str, Any]:
    """Run a prediction model over every lap of a replay, and cache it.

    A batch pass, on the same background pool as retraining: after it, the
    prediction panel reads rows instead of running models.

    ``model_id`` evaluates under a model that is not the active one. Its
    answers are stored beside the incumbent's rather than replacing them --
    the cache is keyed by model version -- so ``GET /models/compare`` can then
    score the two against the same finishing order.
    """
    if not ensure_local(session_key):
        raise HTTPException(
            status_code=409,
            detail=f"replay for session {session_key} is not built yet; build it first",
        )

    def run(progress: jobs.ProgressReporter) -> dict[str, Any]:
        return replay_predictor.run(session_key, progress, model_id=model_id)

    return jobs.submit("predict", None, run).as_dict()


@router.get(
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


#: Circuits being mapped right now, so asking twice does not map twice.
_mapping: set[str] = set()


@router.get("/circuits/{circuit}/map", tags=["replays"])
def circuit_map(circuit: str, response: Response, db: DBSession = Depends(get_db)) -> Any:
    """What is around a circuit -- grass, gravel, stands, buildings, the pit
    lane -- from OpenStreetMap, in the same frame as the replay's racing line.

    Every replay build maps its circuit. A circuit with a built replay but no
    map yet (built before maps existed, or the mapping failed) is mapped on
    the spot in the background: the answer is then 202 ``{"status":
    "mapping"}`` and the caller asks again shortly.
    """
    from app.data import circuits
    from app.data.circuits import build as circuit_builder

    packed = circuits.gzipped(circuit)
    if packed is not None:
        return Response(content=packed, media_type="application/json", headers={
            "Content-Encoding": "gzip", "Cache-Control": "public, max-age=86400", "Vary": "Accept-Encoding",
        })

    key = circuit.strip().lower()
    if key not in _mapping:
        found = _bundle_at(circuit, db)
        if found is None:
            raise HTTPException(status_code=404, detail=f"no map of the surroundings at {circuit}")
        _mapping.add(key)

        def run(progress: jobs.ProgressReporter) -> dict[str, Any]:
            try:
                progress("mapping", {"circuit": circuit})
                path = circuit_builder.ensure_map(found)
                return {"circuit": circuit, "map": path.name if path else None}
            finally:
                _mapping.discard(key)

        jobs.submit("circuit_map", None, run)
    response.status_code = 202
    response.headers["Cache-Control"] = "no-store"
    return {"status": "mapping", "circuit": circuit}


def _bundle_at(circuit: str, db: DBSession) -> dict[str, Any] | None:
    """Any built replay at this circuit (a circuit does not change between visits)."""
    wanted = circuit.strip().lower()
    for row in replay_store.catalogue_rows(db):
        name = (row.get("circuit") or "").lower()
        if not name or (wanted not in name and name not in wanted):
            continue
        if not ensure_local(row["session_key"]):
            continue
        bundle = replay.load_bundle(row["session_key"])
        if bundle is not None:
            return bundle
    return None


@router.get("/layouts/{circuit}", tags=["replays"])
def circuit_layout(circuit: str, db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """The drawn shape of a circuit, from whichever race happens to have one.

    A race that has not run has no position feed and so no geometry of its own,
    but a circuit does not change between visits: the layout from any built
    replay at the same track is the same layout. This is what lets an upcoming
    round show its map instead of an apology.
    """
    bundle = _bundle_at(circuit, db)
    if bundle is not None:
        session = bundle.get("session") or {}
        return {
            "circuit": session.get("circuit"),
            "track": bundle["track"],
            "source_session_key": session.get("session_key"),
            "source_date": session.get("date_start"),
        }
    raise HTTPException(
        status_code=404,
        detail=f"no built replay carries a layout for {circuit}; build any race there first",
    )


@router.get("/replays/{session_key}/accuracy", tags=["replays"])
def replay_accuracy(session_key: int, db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """How close this replay's stored projections were to the classification.

    Scores three answers side by side -- where the car was at the time, the
    projection, and the trained model -- so the prediction has to earn its
    place against simply reading the running order.
    """
    return calibration.score_replay(db, session_key)


@router.get("/predictions/accuracy", tags=["replays"])
def prediction_accuracy(db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """The same measure across every replay that has been evaluated."""
    return calibration.summary(db)


@router.delete(
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
