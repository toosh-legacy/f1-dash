"""Run the prediction model over a whole replay, once.

The live path predicts the lap it is on and moves on. A replay is different: a
viewer scrubs freely, so every lap is a lap someone might ask about, and asking
lazily means recomputing the same expensive answer whenever they scrub back.

So a replay gets *evaluated* -- a single background pass over every lap of the
race, each result written to :mod:`app.db.projection_store`. After that the
panel is reading rows, not running models, and it stays that way until a new
model version is promoted, at which point the cache stops matching and the race
can be re-run against the new model for comparison.

Two ways in:

* :func:`run` -- the batch pass, submitted as a job like retraining is. It never
  runs on a request path.
* :func:`projection_for` -- one lap, cache first, computed and stored on a miss.
  This is what the panel calls, so an un-evaluated replay still answers, just
  more slowly the first time.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.data import replay
from app.db import models as m
from app.db import projection_store
from app.db.database import session_scope
from app.live import projection
from app.models import registry

log = logging.getLogger(__name__)

Progress = Callable[..., None]

#: Report progress every this many laps: often enough to watch, rarely enough
#: not to flood a websocket with one message per lap.
PROGRESS_EVERY_LAPS = 5


def active_model_version(db: DBSession, regs_regime: str | None = None) -> int | None:
    """The finish-position model currently serving, or ``None`` if none is.

    This is the cache key. A projection computed under model 4 is not an answer
    to a question asked of model 7.
    """
    record = registry.active_record(db, m.ModelType.RACE_FINISH_POSITION.value)
    if record is None:
        return None
    wanted = regs_regime or settings.CURRENT_REGS_REGIME
    # A model trained under another regime is refused at serve time, so it is
    # not the version anything here was computed under either.
    return record.id if record.regs_regime == wanted else None


def projection_for(
    db: DBSession,
    bundle: dict[str, Any],
    lap: int,
    *,
    use_cache: bool = True,
    model_id: int | None = None,
) -> dict[str, Any]:
    """One lap's projected finishing order, from cache where possible.

    ``model_id`` scores the lap under a model that is not the active one. The
    cache is keyed by model version, so a challenger's answers are stored
    beside the incumbent's rather than displacing them, and both can be scored
    against the same classification afterwards.
    """
    session_key = bundle.get("session", {}).get("session_key")
    version = model_id if model_id is not None else active_model_version(db)

    if use_cache and session_key is not None:
        cached = projection_store.read(db, int(session_key), lap, version)
        if cached is not None:
            cached["cached"] = True
            return cached

    computed = projection.project_finish(bundle, lap, db=db, model_id=model_id)
    computed["cached"] = False
    if session_key is not None and computed.get("entries"):
        projection_store.write(db, int(session_key), lap, computed, version)
    return computed


def run(
    session_key: int,
    progress: Progress | None = None,
    *,
    model_id: int | None = None,
) -> dict[str, Any]:
    """Evaluate every lap of a replay and cache the results.

    Safe to call from a job thread: it owns its own database sessions and
    commits as it goes, so a failure halfway leaves the laps it did finish in
    the cache rather than throwing them away.
    """
    report: Progress = progress or (lambda *_args, **_kwargs: None)

    bundle = replay.load_bundle(session_key)
    if bundle is None:
        raise replay.ReplayUnavailable(
            f"replay for session {session_key} is not built; build it before evaluating it"
        )

    laps = sorted(int(lap) for lap in (bundle.get("laps") or {}))
    if not laps:
        raise replay.ReplayUnavailable(f"replay for session {session_key} carries no lap timing")

    report("evaluating", {"session_key": session_key, "laps": len(laps)})

    computed = 0
    reused = 0
    version: int | None = None
    with session_scope() as db:
        version = model_id if model_id is not None else active_model_version(db)

    # One transaction per chunk of laps: a long single transaction would hold a
    # write lock across the whole pass, and the point of the cache is that the
    # rows are useful the moment they exist.
    for index, lap in enumerate(laps, start=1):
        with session_scope() as db:
            if projection_store.read(db, session_key, lap, version) is not None:
                reused += 1
            else:
                result = projection.project_finish(bundle, lap, db=db, model_id=model_id)
                if result.get("entries"):
                    projection_store.write(db, session_key, lap, result, version)
                    computed += 1
        if index % PROGRESS_EVERY_LAPS == 0 or index == len(laps):
            report("evaluating", {"lap": lap, "done": index, "of": len(laps)})

    summary = {
        "session_key": session_key,
        "laps": len(laps),
        "computed": computed,
        "reused": reused,
        "model_version": version,
    }
    log.info("evaluated replay %s: %s", session_key, summary)
    report("stored", summary)
    return summary
