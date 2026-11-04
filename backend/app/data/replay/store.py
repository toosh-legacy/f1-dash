"""Where a built bundle lives, and how it is read back.

A bundle is expensive to build and never changes once a session is over, so it
is written once as gzipped JSON and cached in three places at once: on disk, in
a small in-process cache of parsed bundles, and -- in production -- in object
storage, which is the copy that survives the container.

The parsed cache is what makes the difference at request time. Unzipping and
parsing a race is slow enough to be felt, and the dashboard asks for the same
race repeatedly, so recently used bundles stay in memory and the halves the
dashboard actually fetches stay pre-encoded beside them.
"""
from __future__ import annotations

import gzip
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from app.config import settings
from app.data.replay.constants import BUNDLE_VERSION, META_FIELDS
from app.data.replay.narrative import _overtakes
from app.data.replay.shared import log
from app.storage import blobs

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


def bundle_key(session_key: int) -> str:
    """The bundle's name in blob storage -- stable across containers."""
    return f"replays/session_{session_key}_v{BUNDLE_VERSION}.json.gz"


def is_cached(session_key: int) -> bool:
    """Whether this box can serve the bundle without rebuilding it.

    On the local backend that is a question about disk. On S3 it is a question
    about the bucket, and a hit fills the disk cache on the way past, so the
    read that follows is an ordinary file read.
    """
    return blobs.exists(bundle_key(session_key), bundle_path(session_key))


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
    if not path.exists():
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
    client, and what gets published so other clients need not rebuild it.
    """
    path = bundle_path(session_key)
    if not blobs.materialize(bundle_key(session_key), path):
        return None
    return path.read_bytes()


def write_bundle_bytes(session_key: int, data: bytes) -> Path:
    """Seed the local file cache from a bundle built elsewhere."""
    settings.REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    path = bundle_path(session_key)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    blobs.publish(path, bundle_key(session_key))
    forget_bundle(session_key)
    return path


def _store_bundle(session_key: int, bundle: dict[str, Any]) -> Path:
    settings.REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    path = bundle_path(session_key)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        json.dump(bundle, handle, separators=(",", ":"))
    tmp.replace(path)
    blobs.publish(path, bundle_key(session_key))
    forget_bundle(session_key)
    return path
