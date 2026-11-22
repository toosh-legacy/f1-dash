"""Where a built bundle lives, and how it is read back.

A bundle is expensive to build and never changes once a session is over, so it
is written once as gzipped JSON to disk, and recently used ones are kept parsed
in a small in-process cache.

The file is only a cache of the database's copy (``replay_bundles``, see
:mod:`app.db.replay_store`): on a Vercel function the disk is a per-instance
``/tmp`` that starts empty. So every read here -- :func:`is_cached`,
:func:`load_bundle`, :func:`bundle_bytes` -- treats a missing file as "fetch the
shared copy", not as "not built". Bundles deliberately do not go through the
generic file store in :mod:`app.storage.files`: ``replay_bundles`` already
holds them, with catalogue metadata beside the blob, and storing them twice
would only double the database.

The parsed cache is what makes the difference at request time. Unzipping and
parsing a race is slow enough to be felt, and the dashboard asks for the same
race repeatedly, so recently used bundles stay in memory and the halves the
dashboard actually fetches stay pre-encoded beside them.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from app.config import settings
from app.data.replay.constants import BUNDLE_VERSION, META_FIELDS
from app.data.replay.narrative import _overtakes
from app.data.replay.shared import log

#: Parsed bundles, least recently used first.
#:
#: A race is half a megabyte of gzip and rather more once parsed, and reading
#: one takes about a third of a second. Scrubbing a replay asks about the same
#: race over and over -- a projection per lap, seventy laps -- and each of those
#: was re-reading and re-parsing the identical file. This cache is deliberately
#: small: it is here so a burst about one race reads the file once, not to hold
#: a season in memory.
_BUNDLE_CACHE: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
_BUNDLE_LOCK = threading.Lock()
BUNDLE_CACHE_SIZE = 3

#: Serialised slices of a bundle, keyed by ``(session_key, part)``.
#:
#: The whole bundle is 475 KB gzipped and 439 KB of that is frames. Everything
#: a dashboard needs to draw the circuit, fill the running order and name the
#: race is in the other 36 KB -- so a client that fetches the two separately can
#: put a race on screen from a payload thirteen times smaller, and stream the
#: playback data behind it. Re-encoding either is not free, so the encoded bytes
#: are kept rather than the work repeated.
_PART_CACHE: "OrderedDict[tuple[int, str], bytes]" = OrderedDict()
PART_CACHE_SIZE = 8
_bundle_generation = 0


def bundle_path(session_key: int) -> Path:
    return settings.REPLAY_DIR / f"session_{session_key}_v{BUNDLE_VERSION}.json.gz"


#: How long the list of bundles in the database is trusted. ``is_cached`` is
#: asked once per row of the session index, so it must not be a query per call.
SHARED_KEYS_TTL_S = 30.0
_shared_keys: tuple[float, frozenset[int]] | None = None
_hydrate_locks: dict[int, threading.Lock] = {}


def is_cached(session_key: int) -> bool:
    """Whether the bundle can be served without rebuilding.

    True if the file is on disk, or if the database holds a build of the
    current format that :func:`load_bundle` / :func:`bundle_bytes` will fetch
    on first read. Never downloads a payload.
    """
    return bundle_path(session_key).exists() or session_key in _shared_session_keys()


def _shared_session_keys() -> frozenset[int]:
    """Session keys with a bundle in ``replay_bundles``, cached briefly."""
    global _shared_keys
    now = time.monotonic()
    held = _shared_keys
    if held is not None and now - held[0] < SHARED_KEYS_TTL_S:
        return held[1]
    # Imported late: app.db.replay_store imports this package.
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from app.db import models as m
    from app.db.database import session_scope

    try:
        with session_scope() as db:
            found = frozenset(
                db.scalars(
                    select(m.ReplayBundle.openf1_session_key).where(
                        m.ReplayBundle.version == BUNDLE_VERSION
                    )
                ).all()
            )
    except SQLAlchemyError as exc:  # no database is the same as no shared builds
        log.warning("could not list shared replay bundles: %s", exc)
        return frozenset()
    _shared_keys = (now, found)
    return found


def _hydrate(session_key: int) -> bool:
    """Fetch the database's copy of a bundle into the file cache.

    True if the file is now on disk. One download per bundle however many
    requests ask at once: a race is half a megabyte, and a fresh instance is
    typically asked for the meta and frames halves in the same instant.
    """
    path = bundle_path(session_key)
    if path.exists():
        return True
    with _BUNDLE_LOCK:
        lock = _hydrate_locks.setdefault(session_key, threading.Lock())
    with lock:
        if path.exists():
            return True
        from sqlalchemy import select
        from sqlalchemy.exc import SQLAlchemyError

        from app.db import models as m
        from app.db.database import session_scope

        try:
            with session_scope() as db:
                payload = db.scalar(
                    select(m.ReplayBundle.payload).where(
                        m.ReplayBundle.openf1_session_key == session_key,
                        m.ReplayBundle.version == BUNDLE_VERSION,
                    )
                )
        except SQLAlchemyError as exc:
            log.warning("could not fetch shared replay %s: %s", session_key, exc)
            return False
        if payload is None:
            return False
        write_bundle_bytes(session_key, payload)
        log.info("hydrated replay %s from the database", session_key)
        return True


def bundle_generation() -> int:
    return _bundle_generation


def forget_bundle(session_key: int | None = None) -> None:
    """Drop cached parses. Called wherever a bundle file is written."""
    global _bundle_generation
    _bundle_generation += 1
    with _BUNDLE_LOCK:
        if session_key is None:
            _BUNDLE_CACHE.clear()
            _PART_CACHE.clear()
        else:
            _BUNDLE_CACHE.pop(session_key, None)
            for part in ("meta", "frames"):
                _PART_CACHE.pop((session_key, part), None)


def bundle_cache_stats() -> dict[str, Any]:
    """What the cache is holding -- reported by ``/health``."""
    with _BUNDLE_LOCK:
        return {
            "held": len(_BUNDLE_CACHE),
            "capacity": BUNDLE_CACHE_SIZE,
            "session_keys": list(_BUNDLE_CACHE),
        }


def load_bundle(session_key: int, *, use_cache: bool = True) -> dict[str, Any] | None:
    """The parsed bundle for a session, or ``None`` if it is not built here.

    The returned dictionary is shared between callers and is to be treated as
    read-only; anything that needs a changed bundle builds a new one and stores
    it, which drops the cached parse.
    """
    if use_cache:
        with _BUNDLE_LOCK:
            hit = _BUNDLE_CACHE.get(session_key)
            if hit is not None:
                _BUNDLE_CACHE.move_to_end(session_key)
                return hit

    path = bundle_path(session_key)
    if not _hydrate(session_key):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            bundle = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:  # a corrupt cache is a miss
        log.warning("discarding unreadable replay bundle %s: %s", path, exc)
        path.unlink(missing_ok=True)
        forget_bundle(session_key)
        return None

    if _upgrade(bundle):
        # Paid once, and never with a refetch: everything added here comes out
        # of frames and timing the bundle is already carrying.
        _store_bundle(session_key, bundle)
        log.info("upgraded replay bundle %s in place", session_key)

    if use_cache:
        with _BUNDLE_LOCK:
            _BUNDLE_CACHE[session_key] = bundle
            _BUNDLE_CACHE.move_to_end(session_key)
            while len(_BUNDLE_CACHE) > BUNDLE_CACHE_SIZE:
                _BUNDLE_CACHE.popitem(last=False)
    return bundle


def _upgrade(bundle: dict[str, Any]) -> bool:
    """Fill in anything a newer build derives, without rebuilding the bundle.

    A replay costs one request per car against a rate-limited API and several
    minutes of reconstruction, so a bundle already on disk is not thrown away
    because this module learned to describe it better. Both fields below are
    derived from data the bundle already holds -- the frames, the per-lap
    order, and the race-control text -- so an old bundle can simply be brought
    forward where it lies.

    Returns whether anything changed, so the caller can write it back.
    """
    from app.live import race_control as rc

    changed = False

    if bundle.get("events") is None:
        track = bundle.get("track") or {}
        bundle["events"] = _overtakes(
            bundle.get("frames") or [],
            bundle.get("laps") or {},
            len(track.get("path") or ()),
        )
        changed = True

    control = bundle.get("race_control") or []
    if control and "severity" not in control[0]:
        for event in control:
            event["severity"] = rc.severity_of(event.get("message"), None)
        changed = True

    return changed


def bundle_part(session_key: int, part: str) -> bytes | None:
    """One slice of a bundle as gzipped JSON: ``"meta"`` or ``"frames"``."""
    if part not in {"meta", "frames"}:
        raise ValueError(f"unknown bundle part {part!r}")

    key = (session_key, part)
    with _BUNDLE_LOCK:
        hit = _PART_CACHE.get(key)
        if hit is not None:
            _PART_CACHE.move_to_end(key)
            return hit

    bundle = load_bundle(session_key)
    if bundle is None:
        return None

    if part == "meta":
        payload: Any = {k: bundle.get(k) for k in META_FIELDS if k in bundle}
        payload["frame_count"] = len(bundle.get("frames") or ())
    else:
        payload = bundle.get("frames") or []

    data = gzip.compress(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"), mtime=0
    )
    with _BUNDLE_LOCK:
        _PART_CACHE[key] = data
        _PART_CACHE.move_to_end(key)
        while len(_PART_CACHE) > PART_CACHE_SIZE:
            _PART_CACHE.popitem(last=False)
    return data


def bundle_bytes(session_key: int) -> bytes | None:
    """The stored gzip for a built replay, or ``None`` if it is not built here.

    Raw bytes rather than the parsed bundle: this is what gets handed to a
    client, and what the database copy is made from. A file this instance does
    not have is fetched from the database first.
    """
    if not _hydrate(session_key):
        return None
    return bundle_path(session_key).read_bytes()


def write_bundle_bytes(session_key: int, data: bytes) -> Path:
    """Restore the file from the database copy of a bundle."""
    settings.REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    path = bundle_path(session_key)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    forget_bundle(session_key)
    return path


def _store_bundle(session_key: int, bundle: dict[str, Any]) -> Path:
    settings.REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    path = bundle_path(session_key)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        json.dump(bundle, handle, separators=(",", ":"))
    tmp.replace(path)
    forget_bundle(session_key)
    return path
