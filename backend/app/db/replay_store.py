"""The shared home for built replays.

Building a replay costs one request per car against a rate-limited API and
several minutes of reconstruction, and a finished race never changes. So a
replay is built once for everybody: the gzipped bundle goes in the database,
and any server that has never built that race hydrates its local file from
there the first time somebody asks for it.

The file on disk stays, but only as a read cache. The database is what makes a
build other people's build too, and it is what survives a machine being
replaced. Everything here is ordinary columns plus one blob, so the move to
Postgres -- or to object storage for the blob, with the row keeping the
metadata -- is a change in one file.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.data import replay
from app.db import models as m

log = logging.getLogger(__name__)


def publish(db: DBSession, session_key: int, bundle: dict[str, Any]) -> int:
    """Store a freshly built bundle so every other client can have it.

    Returns the stored size in bytes. Replaces any earlier build of the same
    format version -- a rebuild means the last one was wrong or incomplete.
    """
    data = replay.bundle_bytes(session_key)
    if data is None:
        raise FileNotFoundError(f"no built bundle on disk for session {session_key}")

    session = bundle.get("session", {})
    row = _row(db, session_key)
    if row is None:
        row = m.ReplayBundle(
            openf1_session_key=session_key,
            version=replay.BUNDLE_VERSION,
            payload=b"",
            size_bytes=0,
        )
        db.add(row)

    row.payload = data
    row.size_bytes = len(data)
    row.circuit = session.get("circuit") or session.get("location")
    row.session_name = session.get("name")
    row.date_start = session.get("date_start")
    row.total_laps = bundle.get("total_laps")
    row.frames = len(bundle.get("frames") or [])
    db.flush()
    log.info("published replay %s (%s bytes) for every client", session_key, row.size_bytes)
    return row.size_bytes


def hydrate(db: DBSession, session_key: int) -> bool:
    """Write a shared bundle to the local file cache. True if one was found.

    This is the path taken by a server that never built the race itself: the
    row is the build, the file is just how it is read quickly afterwards.
    """
    if replay.is_cached(session_key):
        return True
    row = _row(db, session_key)
    if row is None:
        return False
    replay.write_bundle_bytes(session_key, row.payload)
    log.info("hydrated replay %s from the database", session_key)
    return True


def available(db: DBSession) -> set[int]:
    """Session keys with a shared build of the current format."""
    return {
        key
        for (key,) in db.execute(
            select(m.ReplayBundle.openf1_session_key).where(
                m.ReplayBundle.version == replay.BUNDLE_VERSION
            )
        ).all()
    }


def catalogue_rows(db: DBSession) -> list[dict[str, Any]]:
    """What is built, from the database alone.

    Deliberately does not unpack the payloads: the metadata is duplicated onto
    the row so listing the library never costs megabytes of JSON.
    """
    rows = db.scalars(
        select(m.ReplayBundle)
        .where(m.ReplayBundle.version == replay.BUNDLE_VERSION)
        .order_by(m.ReplayBundle.date_start.desc())
    ).all()
    return [
        {
            "session_key": row.openf1_session_key,
            "circuit": row.circuit,
            "name": row.session_name,
            "date_start": row.date_start,
            "total_laps": row.total_laps,
            "frames": row.frames,
            "size_bytes": row.size_bytes,
            "built_at": row.built_at.isoformat() if row.built_at else None,
        }
        for row in rows
    ]


def remove(db: DBSession, session_key: int) -> bool:
    row = _row(db, session_key)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def _row(db: DBSession, session_key: int) -> m.ReplayBundle | None:
    return db.scalar(
        select(m.ReplayBundle).where(
            m.ReplayBundle.openf1_session_key == session_key,
            m.ReplayBundle.version == replay.BUNDLE_VERSION,
        )
    )
