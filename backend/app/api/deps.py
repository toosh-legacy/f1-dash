"""Helpers shared by more than one router.

Anything here is used by at least two route modules. A helper needed by exactly
one router lives in that router, next to its caller, where it is easier to read
than it would be behind an import.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Response
from sqlalchemy.orm import Session as DBSession

from app.data import replay
from app.db import models as m
from app.db import replay_store
from app.db.database import session_scope

#: Replay payloads are stored gzipped and served as stored. A day is safe
#: because a built bundle never changes: rebuilding writes a new version.
BUNDLE_HEADERS = {
    "content-encoding": "gzip",
    "cache-control": "public, max-age=86400",
}


def require_session(db: DBSession, session_id: int) -> m.Session:
    """Load a session or raise the 404 every session route would otherwise write."""
    session = db.get(m.Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no session with id {session_id}")
    return session


def ensure_local(session_key: int) -> bool:
    """Make sure this server has the replay file, fetching the shared copy.

    A build belongs to everyone, so "is it built?" is a question about the
    database, not about this machine's disk.
    """
    if replay.is_cached(session_key):
        return True
    with session_scope() as db:
        return replay_store.hydrate(db, session_key)


def require_built_replay(session_key: int) -> None:
    """Raise the 404 that four replay routes would otherwise each spell out.

    The message names the way forward rather than only the problem: a replay
    that is not built is a request away from being built.
    """
    if not ensure_local(session_key):
        raise HTTPException(
            status_code=404,
            detail=(
                f"replay for session {session_key} is not built yet; "
                f"POST to /replays/{session_key}/build"
            ),
        )


def gzipped_json(data: bytes) -> Response:
    """Return stored gzip as-is -- the browser unwraps it for free."""
    return Response(content=data, media_type="application/json", headers=BUNDLE_HEADERS)


def latest_per_driver(rows: list[Any]) -> list[Any]:
    """Keep only the newest row per driver; input must be newest-first."""
    seen: set[str] = set()
    latest = []
    for row in rows:
        if row.driver_id in seen:
            continue
        seen.add(row.driver_id)
        latest.append(row)
    return latest
