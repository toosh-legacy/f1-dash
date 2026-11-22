"""Files whose record is the database and whose local copy is only a cache.

Why this exists: on a Vercel function the only writable directory is ``/tmp``,
it belongs to one instance, and it is gone when that instance is recycled. A
model trained by the worker, a telemetry window fetched once from a
rate-limited API, a circuit map that took minutes of OpenStreetMap downloads --
each of those used to be a file that simply stayed where it was written. Now
the bytes go into the ``stored_files`` table as well, and any instance that
needs a file it does not have fetches it from there the first time.

The API mirrors the life of a file rather than the shape of the table:

* :func:`publish` -- the file has just been written locally; make it durable.
* :func:`materialize` -- make sure the file is on local disk, fetching it if not.
* :func:`exists` -- is there such a file anywhere, without downloading it?
* :func:`read_bytes`, :func:`delete`, :func:`keys`.

Keys are slash-separated, relative paths (``models/race_finish_position_v3.joblib``,
``telemetry/v2/9158/44/12.json.gz``, ``circuits/monza.json``), so the key reads
like the cache path it backs.

Every function that touches the database takes an optional ``db`` session. With
one, the work joins that transaction: a model's blob then commits or rolls back
together with its registry row, and -- on SQLite, which allows one writer at a
time -- a caller already holding the write lock is not deadlocked by a second
connection. Without one, a short :func:`session_scope` is used.

Only portable SQLAlchemy is used (a get-then-update/insert instead of a dialect
``ON CONFLICT``), so the same code runs on SQLite locally and on Neon Postgres.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DBSession

from app.db.database import Base, engine, session_scope
from app.db.stored_file import StoredFile

log = logging.getLogger(__name__)

#: How long a listing of keys under one prefix is trusted. Catalogue pages ask
#: :func:`exists` once per row; thirty seconds turns that into one ``SELECT key``
#: per prefix per half-minute, and the cost of being stale is only that a file
#: published by another instance a moment ago is reported a little late.
KEY_CACHE_TTL_S = 30.0

_keys_by_prefix: dict[str, tuple[float, set[str]]] = {}
_keys_lock = threading.Lock()

#: One lock per key being materialized, so several requests asking for the same
#: missing file at once download it once, while different files proceed in
#: parallel.
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()

_table_ready = False
_table_lock = threading.Lock()


# -- plumbing ----------------------------------------------------------------


def _ensure_table() -> None:
    """Create ``stored_files`` if this database predates it. Once per process.

    ``init_db`` creates it on a normal start, but a worker or script that never
    called ``init_db`` should not fail the first time it stores a file. With the
    table present this is only a catalogue read.
    """
    global _table_ready
    if _table_ready:
        return
    with _table_lock:
        if not _table_ready:
            Base.metadata.create_all(bind=engine, tables=[StoredFile.__table__], checkfirst=True)
            _table_ready = True


def _lock_for(key: str) -> threading.Lock:
    with _key_locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = threading.Lock()
        return lock


def _prefix_of(key: str) -> str:
    """The "directory" of a key, which is the unit key listings are cached by.

    Caching per directory rather than one set of every key keeps each listing
    small: there may be thousands of telemetry windows, but one car's windows
    for one session are a few dozen.
    """
    head, sep, _ = key.rpartition("/")
    return head + sep


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a sibling temporary file, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _remember_key(key: str, present: bool) -> None:
    """Keep a cached listing in step with what this process just did."""
    with _keys_lock:
        entry = _keys_by_prefix.get(_prefix_of(key))
        if entry is not None:
            (entry[1].add if present else entry[1].discard)(key)


def forget_keys() -> None:
    """Drop every cached key listing (tests, or after bulk changes)."""
    with _keys_lock:
        _keys_by_prefix.clear()


# -- writing -----------------------------------------------------------------


def _upsert(db: DBSession, key: str, data: bytes) -> None:
    row = db.get(StoredFile, key)
    now = datetime.now(timezone.utc)
    if row is None:
        db.add(StoredFile(key=key, data=data, size=len(data), updated_at=now))
    else:
        row.data = data
        row.size = len(data)
        row.updated_at = now
    db.flush()


def publish_bytes(key: str, data: bytes, *, db: DBSession | None = None) -> None:
    """Store bytes under a key, replacing any earlier copy."""
    _ensure_table()
    if db is not None:
        _upsert(db, key, data)
    else:
        with session_scope() as own:
            _upsert(own, key, data)
    _remember_key(key, True)


def publish(path: Path, key: str, *, db: DBSession | None = None) -> None:
    """Make a file that has just been written locally durable under ``key``.

    Raises if the database cannot be written: a caller for whom the file is
    only a nicety catches that itself, but a model artifact silently left on
    an instance's ``/tmp`` would be a registry row pointing at nothing.
    """
    publish_bytes(key, Path(path).read_bytes(), db=db)


def publish_many(items: Iterable[tuple[str, bytes]], *, db: DBSession | None = None) -> int:
    """Store several files in one transaction. Returns how many were written.

    For bulk writers such as the telemetry prefetch, which writes a car's whole
    session -- sixty-odd windows -- at once: one round trip per file to a
    remote database would cost more than the fetch did.
    """
    _ensure_table()
    written: list[str] = []

    def run(session: DBSession) -> None:
        for key, data in items:
            _upsert(session, key, data)
            written.append(key)

    if db is not None:
        run(db)
    else:
        with session_scope() as own:
            run(own)
    for key in written:
        _remember_key(key, True)
    return len(written)


def delete(key: str, *, db: DBSession | None = None) -> bool:
    """Remove the stored copy of a key. True if there was one.

    The local cache file, if any, is the caller's to remove: only it knows
    where its cache lives.
    """
    _ensure_table()

    def run(session: DBSession) -> bool:
        row = session.get(StoredFile, key)
        if row is None:
            return False
        session.delete(row)
        session.flush()
        return True

    if db is not None:
        found = run(db)
    else:
        with session_scope() as own:
            found = run(own)
    _remember_key(key, False)
    return found


# -- reading -----------------------------------------------------------------


def read_bytes(key: str, *, db: DBSession | None = None) -> bytes | None:
    """The stored bytes for a key, or ``None``. Downloads the blob."""
    try:
        _ensure_table()
        if db is not None:
            return db.scalar(select(StoredFile.data).where(StoredFile.key == key))
        with session_scope() as own:
            return own.scalar(select(StoredFile.data).where(StoredFile.key == key))
    except SQLAlchemyError as exc:
        # A read that cannot reach the store is a cache miss, not a crash:
        # the caller falls back to whatever it does when a file was never made.
        log.warning("file store unavailable reading %s: %s", key, exc)
        return None


def materialize(key: str, path: Path, *, db: DBSession | None = None) -> bool:
    """Make sure ``path`` exists locally, fetching it from the store if needed.

    Returns whether the file is now there. Concurrent callers for the same key
    wait for one download rather than each making their own; the file is
    written atomically so a reader never opens a partial one.
    """
    path = Path(path)
    if path.exists():
        return True
    with _lock_for(key):
        if path.exists():  # another thread fetched it while this one waited
            return True
        data = read_bytes(key, db=db)
        if data is None:
            return False
        try:
            _atomic_write(path, data)
        except OSError as exc:
            log.warning("could not cache %s at %s: %s", key, path, exc)
            return False
        log.info("materialized %s from the file store (%s bytes)", key, len(data))
        return True


def keys(prefix: str, *, db: DBSession | None = None, fresh: bool = False) -> set[str]:
    """Every stored key starting with ``prefix``, without reading any blob.

    Cached for :data:`KEY_CACHE_TTL_S` per prefix; ``fresh`` skips the cache.
    A store that cannot be reached lists as empty.
    """
    now = time.monotonic()
    if not fresh:
        with _keys_lock:
            entry = _keys_by_prefix.get(prefix)
            if entry is not None and now - entry[0] < KEY_CACHE_TTL_S:
                return set(entry[1])
    stmt = select(StoredFile.key).where(StoredFile.key.startswith(prefix, autoescape=True))
    try:
        _ensure_table()
        if db is not None:
            found = set(db.scalars(stmt).all())
        else:
            with session_scope() as own:
                found = set(own.scalars(stmt).all())
    except SQLAlchemyError as exc:
        log.warning("file store unavailable listing %s: %s", prefix, exc)
        return set()
    with _keys_lock:
        _keys_by_prefix[prefix] = (now, found)
    return set(found)


def exists(key: str, path: Path | None = None, *, db: DBSession | None = None) -> bool:
    """Whether a file is available, locally or in the store. Never downloads.

    Cheap enough to call once per row of a listing: the local check is a
    ``stat``, and the store check reads a cached listing of the key's directory.
    """
    if path is not None and Path(path).exists():
        return True
    return key in keys(_prefix_of(key), db=db)
