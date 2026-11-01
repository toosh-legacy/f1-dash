"""Health, and what the dashboard should open on."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app import schemas
from app.config import settings
from app.data import replay
from app.db import models as m
from app.db import replay_store
from app.db import session_index
from app.db.database import get_db
from app.data.openf1_client import auth
from app.live.base import loops
from app.live.director import director
from app.live.feed import feeds
from app.models import calibration
from app.training import jobs

#: A live loop that has not completed a poll in this long is not keeping up
#: with a session, whatever its thread says about being alive.
STALE_POLL_S = 120.0

router = APIRouter()


@router.get("/health", response_model=schemas.HealthOut, tags=["meta"])
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


@router.get("/now", tags=["meta"])
def now(db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """What the dashboard should open on when somebody hits Connect.

    In order of interest: a session being polled right now, otherwise the next
    session on the calendar whatever its kind -- a Friday practice is what a
    viewer wants on a Friday -- and, either way, the most recent race with a
    replay built, since that is the one thing always worth showing.
    """
    moment = datetime.now(timezone.utc)

    # A practice session has a position feed and no prediction loop.
    running = loops.statuses() or feeds.statuses()
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
        "live_watch": {
            "state": director.state,
            "message": director.message,
            "latest_session": director.latest,
            "openf1_signed_in": auth.status()["signed_in"],
            "openf1_configured": auth.configured,
        },
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
