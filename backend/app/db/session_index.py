"""The OpenF1 session catalogue, kept locally instead of fetched per request.

``GET /replays`` answers one question -- which races have run this season, and
which of them have a replay built. The second half has always come from the
database. The first half was a live call to OpenF1 on every single request,
with a fresh HTTP client each time, which cost about 1.3 seconds and put a
rate-limited third party in the path of opening the dashboard.

Nothing about that answer is volatile. A race that ran last month will still
have run last month, and the only thing that changes is a new session appearing
when a weekend starts. So the catalogue is a cache with a stale-while-revalidate
policy:

* rows are served from the ``openf1_session_index`` table immediately;
* if they are older than :data:`REFRESH_AFTER_S`, one background thread is sent
  to refresh them and the *current* rows are returned anyway;
* only a completely empty index blocks on the network, which happens once.

The upstream call is unchanged and still lives in :mod:`app.data.replay`; this
module decides when to make it.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.data import replay
from app.data.openf1_client import OpenF1Client
from app.db import models as m
from app.db.database import session_scope

log = logging.getLogger(__name__)

#: How old the index may be before a refresh is sent for. A session appears in
#: the catalogue when it starts, so this is the worst case for noticing that a
#: race weekend has begun -- and it is noticed *behind* a served response, not
#: in front of one.
REFRESH_AFTER_S = 900.0

#: One refresh at a time, per year. A burst of requests against a stale index
#: should send one thread upstream, not one per request.
_refreshing: set[int] = set()
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_entry(row: m.OpenF1SessionIndex) -> replay.ReplayCatalogueEntry:
    return replay.ReplayCatalogueEntry(
        session_key=row.session_key,
        meeting_key=row.meeting_key,
        name=row.name or "Race",
        session_type=row.session_type or "Race",
        circuit=row.circuit,
        country=row.country,
        location=row.location,
        date_start=row.date_start,
        year=row.year,
    )


def rows(db: DBSession, year: int) -> list[m.OpenF1SessionIndex]:
    return list(
        db.scalars(
            select(m.OpenF1SessionIndex)
            .where(m.OpenF1SessionIndex.year == year)
            .order_by(m.OpenF1SessionIndex.date_start.desc())
        ).all()
    )


def indexed_at(db: DBSession, year: int) -> datetime | None:
    """When this year's index was last refreshed, or ``None`` if never."""
    return db.scalar(
        select(m.OpenF1SessionIndex.indexed_at)
        .where(m.OpenF1SessionIndex.year == year)
        .order_by(m.OpenF1SessionIndex.indexed_at.desc())
        .limit(1)
    )


def is_stale(db: DBSession, year: int, max_age_s: float = REFRESH_AFTER_S) -> bool:
    stamp = indexed_at(db, year)
    if stamp is None:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return _now() - stamp > timedelta(seconds=max_age_s)


def refresh(db: DBSession, year: int, *, client: OpenF1Client | None = None) -> int:
    """Fetch the catalogue from OpenF1 and write it into the index.

    Upserted rather than replaced: a session that vanishes from an upstream
    response is far more likely to be a bad response than a race that never
    happened.
    """
    entries = replay.catalogue(year, client=client, include_sprints=True)
    stamp = _now()
    existing = {row.session_key: row for row in rows(db, year)}

    for entry in entries:
        row = existing.get(entry.session_key)
        if row is None:
            row = m.OpenF1SessionIndex(session_key=entry.session_key, year=year)
            db.add(row)
        row.meeting_key = entry.meeting_key
        row.name = entry.name
        row.session_type = entry.session_type
        row.circuit = entry.circuit
        row.country = entry.country
        row.location = entry.location
        row.date_start = entry.date_start
        row.year = entry.year or year
        row.indexed_at = stamp

    db.flush()
    log.info("session index for %s refreshed: %s sessions", year, len(entries))
    return len(entries)


def _refresh_in_background(year: int) -> None:
    with _lock:
        if year in _refreshing:
            return
        _refreshing.add(year)

    def work() -> None:
        try:
            with session_scope() as db:
                refresh(db, year)
        except Exception as exc:  # a stale index is not worth an error page
            log.warning("background session-index refresh for %s failed: %s", year, exc)
        finally:
            with _lock:
                _refreshing.discard(year)

    threading.Thread(target=work, name=f"session-index-{year}", daemon=True).start()


def catalogue(
    db: DBSession,
    year: int | None = None,
    *,
    include_sprints: bool = True,
    max_age_s: float = REFRESH_AFTER_S,
) -> list[replay.ReplayCatalogueEntry]:
    """This season's races, from the index, refreshing behind the response."""
    year = year or settings.CURRENT_SEASON
    indexed = rows(db, year)

    if not indexed:
        # Nothing to serve, so this one call does pay for the fetch.
        refresh(db, year)
        db.commit()
        indexed = rows(db, year)
    elif is_stale(db, year, max_age_s):
        _refresh_in_background(year)

    entries = [_as_entry(row) for row in indexed]
    if not include_sprints:
        entries = [e for e in entries if (e.name or "").lower() != "sprint"]

    for entry in entries:
        entry.cached = replay.is_cached(entry.session_key)
        if entry.cached:
            path = replay.bundle_path(entry.session_key)
            entry.size_bytes = path.stat().st_size if path.exists() else None
    return entries


def status(db: DBSession, year: int | None = None) -> dict[str, Any]:
    """Index freshness -- reported by ``/health``."""
    year = year or settings.CURRENT_SEASON
    stamp = indexed_at(db, year)
    return {
        "year": year,
        "sessions": len(rows(db, year)),
        "indexed_at": stamp.isoformat() if stamp else None,
        "stale": is_stale(db, year),
        "refreshing": year in _refreshing,
    }
