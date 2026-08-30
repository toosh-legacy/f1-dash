"""Where computed projections are kept, so a replay is evaluated once.

Scrubbing a replay walks seventy laps; each lap's projection assembles feature
vectors from the season's aggregates and runs the trained model over the field.
Doing that on every scrub is the wrong shape of work for something that can
never change: a finished race is a fixed input, so its answers are cached.

The cache is keyed by the model version that produced each row. Promoting a new
model therefore does not invalidate anything explicitly -- it simply stops
matching, the old rows stay for comparison, and the new ones are computed on
first use.

**On the database.** This is SQLAlchemy against SQLite today and nothing here
knows that. The rows are ordinary columns plus one JSON payload, the writes go
through the same session machinery as the rest of the app, and the only SQLite
specifics in the project live in ``database.py``'s connection pragmas. Moving to
Postgres is a connection-string change; if the cache outgrows a table, the
interface below is small enough to reimplement over Redis or a key-value store
without touching a caller.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.db import models as m

log = logging.getLogger(__name__)

#: Bumped whenever a stored projection changes.
#:
#: Scoring the corpus reads every stored projection of every replay and folds
#: them into a handful of averages -- half a second of work for an answer that
#: only moves when a projection is written or dropped. Callers memoise that
#: answer against this number, so their cache is exact rather than timed: it
#: cannot serve a stale figure, and it never expires while nothing has changed.
_generation = 0


def generation() -> int:
    return _generation


def _touch() -> None:
    global _generation
    _generation += 1


def read(
    db: DBSession,
    session_key: int,
    lap: int,
    model_version: int | None,
) -> dict[str, Any] | None:
    """A stored projection, or ``None`` if this lap has not been run yet.

    A row produced by a different model version is a miss, not a hit: it is an
    answer to a different question.
    """
    row = db.scalar(
        select(m.ReplayProjection).where(
            m.ReplayProjection.openf1_session_key == session_key,
            m.ReplayProjection.lap_number == lap,
            m.ReplayProjection.model_version.is_(None)
            if model_version is None
            else m.ReplayProjection.model_version == model_version,
        )
    )
    return dict(row.payload) if row else None


def write(
    db: DBSession,
    session_key: int,
    lap: int,
    payload: dict[str, Any],
    model_version: int | None,
    *,
    regs_regime: str | None = None,
) -> None:
    """Store one lap's projection, replacing any earlier run of the same model."""
    existing = db.scalar(
        select(m.ReplayProjection).where(
            m.ReplayProjection.openf1_session_key == session_key,
            m.ReplayProjection.lap_number == lap,
            m.ReplayProjection.model_version.is_(None)
            if model_version is None
            else m.ReplayProjection.model_version == model_version,
        )
    )
    if existing is None:
        existing = m.ReplayProjection(
            openf1_session_key=session_key,
            lap_number=lap,
            model_version=model_version,
            regs_regime=regs_regime or settings.CURRENT_REGS_REGIME,
            payload={},
        )
        db.add(existing)
    existing.payload = payload
    db.flush()
    _touch()


def coverage(db: DBSession, session_key: int) -> dict[str, Any]:
    """What has been computed for a replay, per model version."""
    rows = db.execute(
        select(
            m.ReplayProjection.model_version,
            func.count(m.ReplayProjection.id),
            func.min(m.ReplayProjection.lap_number),
            func.max(m.ReplayProjection.lap_number),
            func.max(m.ReplayProjection.created_at),
        )
        .where(m.ReplayProjection.openf1_session_key == session_key)
        .group_by(m.ReplayProjection.model_version)
    ).all()
    return {
        "session_key": session_key,
        "runs": [
            {
                "model_version": version,
                "laps": count,
                "first_lap": first,
                "last_lap": last,
                "computed_at": computed.isoformat() if computed else None,
            }
            for version, count, first, last, computed in rows
        ],
    }


def clear(db: DBSession, session_key: int | None = None, model_version: int | None = None) -> int:
    """Drop cached projections. Returns how many rows went."""
    statement = delete(m.ReplayProjection)
    if session_key is not None:
        statement = statement.where(m.ReplayProjection.openf1_session_key == session_key)
    if model_version is not None:
        statement = statement.where(m.ReplayProjection.model_version == model_version)
    removed = db.execute(statement).rowcount or 0
    log.info("cleared %s cached projections", removed)
    _touch()
    return removed
