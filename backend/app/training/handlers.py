"""What each kind of background job actually does.

A job is stored as ``(kind, params)`` rather than as a closure, because the
process that runs it is often not the one that queued it: on Vercel the API
records the job and a GitHub Actions worker runs it. Each handler here takes
the JSON params and a progress reporter and returns a JSON-able result.

Heavy modules (FastF1, the model code, the circuit mapper) are imported
inside the handlers, never at the top: the API only needs this module to know
which kinds exist, and enqueueing must not pull in a training stack.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

Progress = Callable[..., None]
Handler = Callable[[dict[str, Any], Progress], dict[str, Any]]


def replay_build(params: dict[str, Any], progress: Progress) -> dict[str, Any]:
    """Build a replay bundle, publish it, then keep its telemetry and map."""
    from app.data import replay
    from app.data.replay import telemetry
    from app.db import replay_store
    from app.db.database import session_scope

    session_key = int(params["session_key"])
    force = bool(params.get("force", False))
    bundle = replay.build_bundle(
        session_key,
        force=force,
        progress=lambda stage, detail=None: progress(stage, detail),
    )
    # Publishing is the point: one person's build is everyone's replay.
    with session_scope() as db:
        size = replay_store.publish(db, session_key, bundle)
    progress("published", {"session_key": session_key, "bytes": size})
    # Keep every car's telemetry with the replay, so the probe never waits
    # on OpenF1 -- or finds it locked while another session is live.
    try:
        telemetry.prefetch_session(
            session_key, bundle, report=lambda stage, detail=None: progress(stage, detail)
        )
    except Exception as error:  # noqa: BLE001 - the replay plays without it
        log.warning("prefetching telemetry for %s failed: %s", session_key, error)
    # The first race built at a circuit maps its surroundings. Best effort: a
    # replay without a map still plays.
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


def _local_bundle(session_key: int) -> dict[str, Any]:
    """The built bundle, fetched from the shared store if this machine lacks it.

    A worker starts with an empty disk, so "built" has to mean "in the
    database", the same as it does for the API.
    """
    from app.api.deps import ensure_local
    from app.data import replay

    if not ensure_local(session_key):
        raise LookupError(f"replay for session {session_key} is not built")
    return replay.load_bundle(session_key) or {}


def replay_telemetry(params: dict[str, Any], progress: Progress) -> dict[str, Any]:
    """Fetch every car's telemetry for a built replay; cars on disk are skipped."""
    from app.data.replay import telemetry

    session_key = int(params["session_key"])
    written = telemetry.prefetch_session(
        session_key,
        _local_bundle(session_key),
        report=lambda stage, detail=None: progress(stage, detail),
    )
    return {"session_key": session_key, "cars": len(written)}


def replay_predict(params: dict[str, Any], progress: Progress) -> dict[str, Any]:
    """Run a prediction model over every lap of a replay, caching each answer."""
    from app.models import replay_predictor

    session_key = int(params["session_key"])
    _local_bundle(session_key)   # the predictor reads the bundle from disk
    model_id = params.get("model_id")
    return replay_predictor.run(
        session_key, progress, model_id=int(model_id) if model_id is not None else None
    )


def circuit_map(params: dict[str, Any], progress: Progress) -> dict[str, Any]:
    """Map a circuit's surroundings from any built replay there."""
    from app.api.replays import _bundle_at
    from app.data.circuits import build as circuit_builder
    from app.db.database import session_scope

    circuit = str(params["circuit"])
    with session_scope() as db:
        found = _bundle_at(circuit, db)
    if found is None:
        raise LookupError(f"no built replay at {circuit} to map it from")
    progress("mapping", {"circuit": circuit})
    path = circuit_builder.ensure_map(found)
    return {"circuit": circuit, "map": path.name if path else None}


def retrain_race(params: dict[str, Any], progress: Progress) -> dict[str, Any]:
    from app.training import retrain_race as pipeline

    return pipeline.run(int(params["session_id"]), progress)


def retrain_qualifying(params: dict[str, Any], progress: Progress) -> dict[str, Any]:
    from app.training import retrain_qualifying as pipeline

    return pipeline.run(int(params["session_id"]), progress)


HANDLERS: dict[str, Handler] = {
    "replay": replay_build,
    "telemetry": replay_telemetry,
    "predict": replay_predict,
    "circuit_map": circuit_map,
    "retrain_race": retrain_race,
    "retrain_qualifying": retrain_qualifying,
}

#: The params that make two jobs "the same work", for kinds where not every
#: param does. Building a replay with ``force`` and without is still building
#: that replay; a second build while one is under way would only race it.
#: Kinds not listed compare every param.
DEDUPE_KEYS: dict[str, tuple[str, ...]] = {
    "replay": ("session_key",),
}
