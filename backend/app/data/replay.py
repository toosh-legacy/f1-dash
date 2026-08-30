"""Replay bundles: a completed session reconstructed for playback on the map.

OpenF1 exposes a ``location`` feed of raw track X/Y per car, which is enough to
redraw a session from the outside: the circuit itself, every car moving around
it, and the moments they peel into the pit lane.

Building one is expensive (one request per car, tens of thousands of samples
each) and the result never changes once a session is over, so a bundle is built
once as a background job and cached on disk as gzipped JSON. The dashboard then
fetches a single file and plays it locally.

The feed is coarser than its sample rate suggests: a car's position updates
roughly every 2.7 seconds, which at racing speed is a jump of some 250 metres
and about thirty distinct points per lap. Nothing here is drawn from raw
coordinates, because doing that cuts every corner. Instead:

* **The circuit outline** is reconstructed by folding every green lap of the
  race onto a single lap -- the sampling clock and the lap clock are
  independent, so across a race the same corner is caught from many different
  points -- and then refined twice by re-placing each fix along the loop the
  previous pass produced.
* **The pit lane** is traced from the fixes around timed stops. Distance from
  the racing line cannot find it on its own: a car running wide at a fast
  corner is further off line than a car in the pits.
* **Cars** are carried as *progress along* those paths rather than as
  positions, so interpolating between two fixes moves a car through the corners
  it actually drove, at the speed it drove them.
* **Per-lap race state** -- position, rolling pace, tyre and stop count -- is
  what the prediction panel reasons over.

Coordinates stay in OpenF1's own units (tenths of a metre, origin arbitrary).
The dashboard normalises them against the bundle's bounds, so no assumption
about scale or orientation is baked in here.
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import statistics
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from app.config import settings
from app.data.openf1_client import OpenF1Client

log = logging.getLogger(__name__)

Progress = Callable[[str, dict[str, Any] | None], None]

#: Bundle format version. Bumped when the on-disk shape changes so stale caches
#: are rebuilt rather than misread.
BUNDLE_VERSION = 6

#: A sample further than this from the racing line is not on the racing line.
#: OpenF1 units are roughly decimetres, so this is ~15 m -- wide enough to keep
#: a car running off-line through a corner, tight enough to isolate pit lane.
PIT_LANE_MIN_DISTANCE = 150.0

#: Fixes arrive every ~2.7 s, so a car that has not reported for this long is
#: off track -- retired, or in the garage -- rather than frozen on the map at
#: its last known point.
SAMPLE_STALE_S = 20.0

#: A short distance in feed units (~2.5 m), used as the floor for spatial-index
#: cell sizes so a degenerate path cannot produce a zero-sized grid.
TRACK_POINT_SPACING = 25.0

#: Vertices in the reconstructed circuit outline. Around 8 m apart on a normal
#: lap: fine enough to draw the corners, coarse enough to stay small and to
#: keep a car's position along it meaningful.
TRACK_POINTS = 600

#: Fixes further than this from the racing line are not pit lane either; they
#: are garages, run-off excursions or bad fixes.
PIT_LANE_MAX_DISTANCE = 900.0

#: How much of the approach to, and escape from, a timed stop counts as being
#: in the pit lane. A stop is timestamped at the box, but the lane either side
#: of it is most of what there is to draw, so the window is generous.
PIT_APPROACH_S = 45.0

#: Within a stop window the car is known to be in the pit lane, so tracing can
#: accept fixes much closer to the racing line than :data:`PIT_LANE_MIN_DISTANCE`
#: -- the lane runs alongside the track, and at Zandvoort or Monaco it runs very
#: close indeed. The wider threshold is still what *classifies* a car as pitting
#: during playback, where there is no stop record to lean on.
PIT_TRACE_MIN_DISTANCE = 80.0

#: A lap slower than this multiple of the session's quickest is an in-lap, an
#: out-lap or a safety-car lap, and is not a picture of the racing line.
CLEAN_LAP_RATIO = 1.06

#: Refinement passes over the reconstructed outline. The first recovers the
#: corners the phase fold cut; the second is worth having; past that it only
#: chases sampling noise.
REFINE_PASSES = 2

#: Rolling window for the pace figure shown in the prediction panel.
PACE_WINDOW_LAPS = 5

#: Laps outside this band of the driver's own median are pit laps or traffic,
#: and are excluded from the pace figure.
PACE_OUTLIER_RATIO = 1.10

#: The share of the field whose pace is the benchmark. The quickest quarter is
#: what a car at the front is actually racing; the field median is not a
#: benchmark at all, since half the grid is slower than it by construction.
REFERENCE_PACE_QUANTILE = 0.25

_TEAM_FALLBACK_COLOUR = "9AA0A6"


class ReplayUnavailable(RuntimeError):
    """The session has no usable position feed."""


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------
@dataclass
class ReplayCatalogueEntry:
    session_key: int
    meeting_key: int | None
    name: str
    session_type: str
    circuit: str | None
    country: str | None
    location: str | None
    date_start: str | None
    year: int | None
    cached: bool = False
    size_bytes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "meeting_key": self.meeting_key,
            "name": self.name,
            "session_type": self.session_type,
            "circuit": self.circuit,
            "country": self.country,
            "location": self.location,
            "date_start": self.date_start,
            "year": self.year,
            "cached": self.cached,
            "size_bytes": self.size_bytes,
        }


def bundle_path(session_key: int) -> Path:
    return settings.REPLAY_DIR / f"session_{session_key}_v{BUNDLE_VERSION}.json.gz"


def is_cached(session_key: int) -> bool:
    return bundle_path(session_key).exists()


def catalogue(
    year: int | None = None,
    *,
    client: OpenF1Client | None = None,
    include_sprints: bool = True,
    now: datetime | None = None,
) -> list[ReplayCatalogueEntry]:
    """Every race that has already run this season, newest first.

    A session only appears once it has actually started; a scheduled round has
    nothing to replay.
    """
    year = year or settings.CURRENT_SEASON
    now = now or datetime.now(timezone.utc)
    owned = client is None
    client = client or OpenF1Client()
    try:
        rows = client.sessions(year=year)
    finally:
        if owned:
            client.close()

    entries: list[ReplayCatalogueEntry] = []
    for row in rows:
        # OpenF1 types both the grand prix and the sprint as "Race"; they are
        # told apart by name.
        if row.get("session_type") != "Race":
            continue
        name = str(row.get("session_name") or "Race")
        if not include_sprints and name.lower().startswith("sprint"):
            continue
        started = _parse_date(row.get("date_start"))
        if started is None or started > now:
            continue
        key = int(row["session_key"])
        path = bundle_path(key)
        entries.append(
            ReplayCatalogueEntry(
                session_key=key,
                meeting_key=row.get("meeting_key"),
                name=name,
                session_type=str(row.get("session_type") or ""),
                circuit=row.get("circuit_short_name"),
                country=row.get("country_name"),
                location=row.get("location"),
                date_start=row.get("date_start"),
                year=row.get("year") or year,
                cached=path.exists(),
                size_bytes=path.stat().st_size if path.exists() else None,
            )
        )
    entries.sort(key=lambda e: e.date_start or "", reverse=True)
    return entries


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
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


#: Bumped whenever a bundle on disk changes, so anything derived from a bundle
#: can tell its answer is stale without having to compare the bundle itself.
_bundle_generation = 0


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


#: The part of a bundle that is not playback data.
META_FIELDS = (
    "version", "built_at", "session", "duration_s", "frame_interval_s",
    "total_laps", "drivers", "track", "laps", "race_control", "events", "results",
)


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
    return path.read_bytes() if path.exists() else None


def write_bundle_bytes(session_key: int, data: bytes) -> Path:
    """Seed the local file cache from a bundle built elsewhere."""
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


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------
def build_bundle(
    session_key: int,
    *,
    client: OpenF1Client | None = None,
    progress: Progress | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Fetch, derive and cache a replay bundle. Safe to call from a job thread."""
    report: Progress = progress or (lambda _stage, _detail=None: None)

    if not force:
        cached = load_bundle(session_key)
        if cached is not None:
            report("cached", {"session_key": session_key})
            return cached

    owned = client is None
    client = client or OpenF1Client()
    try:
        return _build(session_key, client, report)
    finally:
        if owned:
            client.close()


def _build(session_key: int, client: OpenF1Client, report: Progress) -> dict[str, Any]:
    report("session", {"session_key": session_key})
    meta = _session_meta(client, session_key)

    report("drivers", None)
    drivers = _drivers(client, session_key)
    if not drivers:
        raise ReplayUnavailable(f"session {session_key} has no driver list")

    report("timing", {"drivers": len(drivers)})
    laps = client.laps(session_key)
    stints = client.stints(session_key)
    pits = client.pit(session_key)
    positions = client.position(session_key)
    intervals = client.intervals(session_key)
    control = client.race_control(session_key)

    report("locations", {"cars": len(drivers)})
    traces: dict[int, list[tuple[float, float, float]]] = {}
    for index, driver in enumerate(drivers, start=1):
        number = driver["number"]
        try:
            rows = client.location(session_key, number)
        except Exception as exc:  # one silent car must not lose the whole replay
            log.warning("no location feed for car %s: %s", number, exc)
            rows = []
        traces[number] = _clean_trace(rows)
        report("locations", {"car": number, "done": index, "of": len(drivers)})

    if not any(traces.values()):
        raise ReplayUnavailable(f"session {session_key} has no position feed")

    t0, t1 = _session_window(traces, meta)
    report("track", None)
    track = _build_track(traces, laps, pits)

    report("frames", None)
    path = [tuple(p) for p in track["path"]]
    pit_path = [tuple(p) for p in track["pit_path"]]
    frames = _build_frames(_project(traces, path, pit_path), len(path), t0, t1)
    lap_timeline = _lap_timeline(laps, t0)
    flag_timeline = _flag_timeline(control, t0)
    _annotate_frames(frames, lap_timeline, flag_timeline, _pit_windows(pits, laps, t0))

    report("race_state", {"frames": len(frames)})
    stint_index = _stint_index(stints)
    lap_state = _lap_state(
        laps, positions, intervals, stint_index, pits, _green_laps(laps, flag_timeline, t0), t0
    )

    bundle = {
        "version": BUNDLE_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "session": meta,
        "duration_s": round(t1 - t0, 1),
        "frame_interval_s": round(1.0 / settings.REPLAY_FRAME_HZ, 3),
        "total_laps": max((int(l["lap_number"]) for l in laps if l.get("lap_number")), default=0),
        "drivers": drivers,
        "track": track,
        "frames": frames,
        "laps": lap_state,
        "race_control": _control_log(control, t0),
        "events": _overtakes(frames, lap_state, len(path)),
        "results": _final_order(positions, laps, drivers),
    }
    path = _store_bundle(session_key, bundle)
    report("stored", {"path": path.name, "bytes": path.stat().st_size})
    return bundle


# --------------------------------------------------------------------------
# Pieces
# --------------------------------------------------------------------------
def _session_meta(client: OpenF1Client, session_key: int) -> dict[str, Any]:
    rows = client.sessions(session_key=session_key)
    row = rows[0] if rows else {}
    return {
        "session_key": session_key,
        "meeting_key": row.get("meeting_key"),
        "name": row.get("session_name"),
        "type": row.get("session_type"),
        "circuit": row.get("circuit_short_name"),
        "location": row.get("location"),
        "country": row.get("country_name"),
        "year": row.get("year"),
        "date_start": row.get("date_start"),
        "date_end": row.get("date_end"),
    }


def _drivers(client: OpenF1Client, session_key: int) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for row in client.drivers(session_key):
        number = row.get("driver_number")
        if number is None:
            continue
        colour = (row.get("team_colour") or "").lstrip("#") or _TEAM_FALLBACK_COLOUR
        seen[int(number)] = {
            "number": int(number),
            "code": row.get("name_acronym") or str(number),
            "name": row.get("full_name") or row.get("broadcast_name") or str(number),
            "team": row.get("team_name"),
            "colour": f"#{colour}",
            "headshot": row.get("headshot_url"),
        }
    return [seen[n] for n in sorted(seen)]


def _clean_trace(rows: Iterable[dict[str, Any]]) -> list[tuple[float, float, float]]:
    """``(epoch_seconds, x, y)`` samples, sorted, with the feed's (0,0) dropped.

    A zeroed coordinate means "no fix", not "on the start line".
    """
    out: list[tuple[float, float, float]] = []
    for row in rows:
        moment = _parse_date(row.get("date"))
        x, y = row.get("x"), row.get("y")
        if moment is None or x is None or y is None:
            continue
        if x == 0 and y == 0:
            continue
        out.append((moment.timestamp(), float(x), float(y)))
    out.sort(key=lambda s: s[0])
    return out


def _session_window(
    traces: dict[int, list[tuple[float, float, float]]], meta: dict[str, Any]
) -> tuple[float, float]:
    """The clock the replay runs on.

    Cars report from the garage hours -- sometimes a day -- before the lights go
    out, so the scheduled window is authoritative and the traces only narrow it.
    """
    starts = [t[0][0] for t in traces.values() if t]
    ends = [t[-1][0] for t in traces.values() if t]
    first, last = min(starts), max(ends)

    scheduled_start = _parse_date(meta.get("date_start"))
    scheduled_end = _parse_date(meta.get("date_end"))
    if scheduled_start:
        first = max(first, scheduled_start.timestamp())
    if scheduled_end:
        last = min(last, scheduled_end.timestamp())
    if last <= first:  # implausible metadata; trust the feed
        return min(starts), max(ends)
    return first, last


# --------------------------------------------------------------------------
# Geometry
#
# The location feed is coarse: a fix roughly every 2.7 s, which at racing speed
# is a 250 m jump. Drawing those raw would teleport cars across corners, so the
# geometry is inverted -- the circuit is reconstructed once as an ordered path,
# and each car is then carried as its *progress along that path*. Interpolating
# progress moves a car through the corners it actually drove, at the speed it
# actually drove them, however sparse the underlying fixes.
# --------------------------------------------------------------------------
def _build_track(
    traces: dict[int, list[tuple[float, float, float]]],
    laps: list[dict[str, Any]],
    pits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Racing line, pit lane and bounds, in the feed's own coordinate space."""
    line = _racing_line(traces, laps)
    if len(line) < 20:
        raise ReplayUnavailable("could not trace a racing line")

    pit = _pit_lane(traces, line, pits)
    xs = [p[0] for p in line] + [p[0] for p in pit]
    ys = [p[1] for p in line] + [p[1] for p in pit]
    return {
        "path": [[round(x, 1), round(y, 1)] for x, y in line],
        "pit_path": [[round(x, 1), round(y, 1)] for x, y in pit],
        "start_finish": [round(line[0][0], 1), round(line[0][1], 1)],
        "bounds": {
            "min_x": min(xs),
            "max_x": max(xs),
            "min_y": min(ys),
            "max_y": max(ys),
        },
    }


def _racing_line(
    traces: dict[int, list[tuple[float, float, float]]],
    laps: list[dict[str, Any]],
) -> list[tuple[float, float]]:
    """Fold a whole race down onto one lap.

    No single lap is sampled densely enough to draw a circuit -- the feed moves
    a car in steps of a couple of hundred metres. But the sampling clock and the
    lap clock are independent, and twenty cars sample independently of each
    other, so across a race the same corner is caught from many slightly
    different points. Stamping every fix with how far through its lap it
    arrived stacks the whole race into one dense cloud shaped like the circuit.

    Turning that cloud back into a line takes two stages, because lap phase is
    only a rough ordering -- two laps a second apart in pace are metres apart at
    the same phase. Phase bootstraps a coarse loop; each refinement pass then
    re-places every fix by projecting it onto the loop it just produced, so the
    ordering comes from geometry rather than timing, and the corners the coarse
    loop cut are recovered.

    Only representative green laps are folded in: a pit lap or a safety-car lap
    would fold the pit lane, or a different line, into the shape of the circuit.
    """
    folded = _fold_laps(traces, _representative_laps(laps))
    if len(folded) < TRACK_POINTS:
        return []

    folded.sort(key=lambda p: p[0])
    cloud = [(x, y) for _phase, x, y in folded]

    # Duplicate vertices carry no direction, and the feed repeats a fix until
    # the car's next update, so the bootstrap loop is deduplicated before it is
    # used as a projection target.
    line = _dedupe(_median_bins(folded, bins=TRACK_POINTS // 4))
    if len(line) < 8:
        return []
    for _pass in range(REFINE_PASSES):
        line = _refine_line(line, cloud, bins=TRACK_POINTS)
    return line


def _fold_laps(
    traces: dict[int, list[tuple[float, float, float]]],
    clean: list[dict[str, Any]],
) -> list[tuple[float, float, float]]:
    """``(lap_phase, x, y)`` for every fix taken during a representative lap."""
    by_driver: dict[int, list[dict[str, Any]]] = {}
    for lap in clean:
        by_driver.setdefault(int(lap["driver_number"]), []).append(lap)

    folded: list[tuple[float, float, float]] = []
    for number, driver_laps in by_driver.items():
        trace = traces.get(number) or []
        if not trace:
            continue
        for lap in driver_laps:
            start = _parse_date(lap["date_start"])
            if start is None:
                continue
            begin = start.timestamp()
            duration = float(lap["lap_duration"])
            for moment, x, y in trace:
                if moment < begin:
                    continue
                if moment > begin + duration:
                    break
                folded.append(((moment - begin) / duration, x, y))
    return folded


def _median_bins(
    points: list[tuple[float, float, float]], bins: int
) -> list[tuple[float, float]]:
    """Median point per equal slice of an ordered key -- jitter collapses out."""
    buckets: dict[int, list[tuple[float, float]]] = {}
    for key, x, y in points:
        buckets.setdefault(min(int(key * bins), bins - 1), []).append((x, y))
    return [
        (
            statistics.median(p[0] for p in buckets[index]),
            statistics.median(p[1] for p in buckets[index]),
        )
        for index in sorted(buckets)
    ]


def _dedupe(line: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return [
        point
        for index, point in enumerate(line)
        if index == 0 or _distance(line[index - 1], point) > 1.0
    ]


def _refine_line(
    line: list[tuple[float, float]], cloud: list[tuple[float, float]], bins: int
) -> list[tuple[float, float]]:
    """Re-bin the fix cloud along an existing loop, by distance not by time.

    Every fix is projected onto the loop *as a curve* -- onto its nearest
    segment, not its nearest vertex -- so a coarse loop of thirty vertices still
    spreads the cloud continuously around the lap instead of stacking it on
    thirty points. Taking the median of each slice then pulls the loop onto the
    racing line, sharpening whatever the previous pass cut.
    """
    if len(line) < 4:
        return line
    arc = _arc_lengths(line)
    total = arc[-1] + _distance(line[-1], line[0])
    if total <= 0:
        return line

    spacing = total / len(line)
    grid = _SpatialIndex(line, cell=max(spacing * 2, TRACK_POINT_SPACING * 2))
    placed = [
        (_arc_position(line, arc, grid, point) / total, point[0], point[1])
        for point in cloud
    ]
    placed.sort(key=lambda p: p[0])
    refined = _dedupe(_median_bins(placed, bins=bins))
    return _smooth(refined, window=3) if len(refined) >= 8 else refined


def _arc_position(
    line: list[tuple[float, float]],
    arc: list[float],
    grid: "_SpatialIndex",
    point: tuple[float, float],
) -> float:
    """Distance along the loop of the closest point on it -- a continuous value.

    The nearest vertex only narrows the search; the answer comes from the two
    segments meeting there, so the result moves smoothly as the fix moves.
    """
    vertex, _distance_to_vertex = grid.nearest(point)
    best_position, best_distance = arc[vertex], math.inf
    for index in (vertex - 1, vertex):
        if index < 0 or index + 1 >= len(line):
            continue
        start, end = line[index], line[index + 1]
        span = _distance(start, end)
        if span == 0:
            continue
        fraction = (
            (point[0] - start[0]) * (end[0] - start[0])
            + (point[1] - start[1]) * (end[1] - start[1])
        ) / (span * span)
        fraction = min(max(fraction, 0.0), 1.0)
        foot = (
            start[0] + (end[0] - start[0]) * fraction,
            start[1] + (end[1] - start[1]) * fraction,
        )
        offset = _distance(point, foot)
        if offset < best_distance:
            best_distance = offset
            best_position = arc[index] + span * fraction
    return best_position


def _arc_lengths(line: list[tuple[float, float]]) -> list[float]:
    """Cumulative distance to each vertex, from the first."""
    lengths = [0.0]
    for previous, point in zip(line, line[1:]):
        lengths.append(lengths[-1] + _distance(previous, point))
    return lengths


def _representative_laps(laps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Green-flag racing laps: no pit lap, no crawl behind a safety car."""
    timed = [
        lap
        for lap in laps
        if lap.get("lap_duration")
        and lap.get("date_start")
        and lap.get("driver_number") is not None
        and not lap.get("is_pit_out_lap")
    ]
    if not timed:
        return []
    quickest = min(float(lap["lap_duration"]) for lap in timed)
    return [lap for lap in timed if float(lap["lap_duration"]) <= quickest * CLEAN_LAP_RATIO]


def _smooth(points: list[tuple[float, float]], window: int) -> list[tuple[float, float]]:
    """Rolling mean over an ordered path -- the fold is dense but jittery."""
    if len(points) <= window:
        return points
    half = window // 2
    out: list[tuple[float, float]] = []
    for index in range(len(points)):
        chunk = points[max(0, index - half) : index + half + 1]
        out.append(
            (sum(p[0] for p in chunk) / len(chunk), sum(p[1] for p in chunk) / len(chunk))
        )
    return out


def _pit_lane(
    traces: dict[int, list[tuple[float, float, float]]],
    line: list[tuple[float, float]],
    pits: list[dict[str, Any]],
) -> list[tuple[float, float]]:
    """The pit lane, traced from the cars that actually used it.

    Distance from the racing line alone cannot find it: a car running wide at a
    fast corner is further off line than a car in the pits. What is decisive is
    *when* -- a stop is timestamped, so the fixes around it are known to be in
    the pit lane, and the ones still far from the racing line are the entry, the
    lane itself and the exit.

    Those fixes are then projected onto the racing line and median-binned, the
    same way the circuit is built, which orders them into a lane running the
    right way round.
    """
    if len(line) < 8:
        return []

    windows: dict[int, list[tuple[float, float]]] = {}
    for stop in pits:
        number = stop.get("driver_number")
        moment = _parse_date(stop.get("date"))
        if number is None or moment is None:
            continue
        duration = float(stop.get("pit_duration") or 3.0)
        at = moment.timestamp()
        windows.setdefault(int(number), []).append(
            (at - PIT_APPROACH_S, at + duration + PIT_APPROACH_S)
        )
    if not windows:
        return []

    arc = _arc_lengths(line)
    total = arc[-1] + _distance(line[-1], line[0])
    grid = _SpatialIndex(line, cell=max(total / len(line) * 2, TRACK_POINT_SPACING * 2))

    placed: list[tuple[float, float, float]] = []
    for number, spans in windows.items():
        for moment, x, y in traces.get(number) or []:
            if not any(start <= moment <= end for start, end in spans):
                continue
            _vertex, offset = grid.nearest((x, y))
            if not PIT_TRACE_MIN_DISTANCE <= offset <= PIT_LANE_MAX_DISTANCE:
                continue
            placed.append((_arc_position(line, arc, grid, (x, y)) / total, x, y))

    if len(placed) < 20:
        return []
    placed.sort(key=lambda p: p[0])
    placed = _rotate_to_gap(placed)

    # The pit lane covers a fraction of the lap, so the slices are spread over
    # the span it actually occupies rather than over the whole circuit.
    first, last = placed[0][0], placed[-1][0]
    span = last - first
    if span <= 0:
        return []
    bins = max(int(TRACK_POINTS * span), 12)
    lane = _dedupe(
        _median_bins([((key - first) / span, x, y) for key, x, y in placed], bins=bins)
    )
    return _smooth(lane, window=3) if len(lane) >= 8 else lane


def _rotate_to_gap(
    placed: list[tuple[float, float, float]]
) -> list[tuple[float, float, float]]:
    """Cut a wrapped run of lap positions at its widest gap.

    Pit entry sits just before the start/finish line and pit exit just after it,
    so ordering the lane by lap position splits it in two and joining the halves
    would draw a chord straight across the circuit. The empty stretch between
    the two halves is by far the widest gap in the sequence, so rotating the
    positions to begin after it puts the lane back in one piece.
    """
    keys = [p[0] for p in placed]
    gaps = [(second - first, index) for index, (first, second) in enumerate(zip(keys, keys[1:]))]
    wrap = (1.0 - keys[-1]) + keys[0]
    widest, cut = max(gaps, default=(0.0, -1))
    if wrap >= widest:
        return placed
    origin = keys[cut + 1]
    rotated = [((key - origin) % 1.0, x, y) for key, x, y in placed]
    rotated.sort(key=lambda p: p[0])
    return rotated


class _SpatialIndex:
    """Uniform grid over a path, for nearest-point lookups."""

    def __init__(self, points: list[tuple[float, float]], cell: float) -> None:
        self.points = points
        self.cell = cell
        self.buckets: dict[tuple[int, int], list[int]] = {}
        for index, (x, y) in enumerate(points):
            self.buckets.setdefault((int(x // cell), int(y // cell)), []).append(index)

    def nearest(self, point: tuple[float, float]) -> tuple[int, float]:
        cx, cy = int(point[0] // self.cell), int(point[1] // self.cell)
        best_index, best = 0, math.inf
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for index in self.buckets.get((cx + dx, cy + dy), ()):
                    distance = _distance(point, self.points[index])
                    if distance < best:
                        best_index, best = index, distance
        if best is math.inf:  # nothing in the neighbouring cells -- scan
            for index, candidate in enumerate(self.points):
                distance = _distance(point, candidate)
                if distance < best:
                    best_index, best = index, distance
        return best_index, best


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------
def _project(
    traces: dict[int, list[tuple[float, float, float]]],
    line: list[tuple[float, float]],
    pit_path: list[tuple[float, float]],
) -> dict[int, list[tuple[float, float, bool]]]:
    """Turn raw fixes into ``(time, progress, in_pit)`` along the drawn paths.

    Track progress is unwrapped -- it keeps climbing past the end of the lap --
    so interpolating between two fixes never runs a car backwards through the
    start/finish line.
    """
    track_grid = _SpatialIndex(line, cell=PIT_LANE_MIN_DISTANCE * 2)
    pit_grid = _SpatialIndex(pit_path, cell=PIT_LANE_MIN_DISTANCE * 2) if pit_path else None
    loop = float(len(line))

    projected: dict[int, list[tuple[float, float, bool]]] = {}
    for number, trace in traces.items():
        samples: list[tuple[float, float, bool]] = []
        unwrapped = 0.0
        previous: float | None = None
        for moment, x, y in trace:
            index, distance = track_grid.nearest((x, y))
            if pit_grid is not None and distance > PIT_LANE_MIN_DISTANCE:
                pit_index, pit_distance = pit_grid.nearest((x, y))
                # Closer to the pit lane than to the track is not enough on its
                # own: the drawn lane is a sparse path, and a car stopped in a
                # gravel trap can be nearer one of its vertices than to the
                # racing line without being in the pits at all. It has to be
                # genuinely on the lane.
                if pit_distance < distance and pit_distance <= PIT_LANE_MIN_DISTANCE:
                    samples.append((moment, float(pit_index), True))
                    previous = None  # rejoining the track resets wrap tracking
                    continue
            position = float(index)
            if previous is not None:
                delta = position - previous
                if delta < -loop / 2:  # crossed the line
                    delta += loop
                elif delta > loop / 2:  # a backwards glitch
                    delta -= loop
                unwrapped += delta
            else:
                unwrapped = position
            previous = position
            samples.append((moment, unwrapped, False))
        projected[number] = samples
    return projected


def _build_frames(
    projected: dict[int, list[tuple[float, float, bool]]],
    loop: int,
    t0: float,
    t1: float,
) -> list[dict[str, Any]]:
    """Resample every car onto one shared clock.

    A frame carries ``{number: progress}`` for cars on track and
    ``{number: ["p", progress]}`` for cars in the pit lane, each a position
    along the matching path in ``track``. A car with no recent fix is simply
    absent from the frame, which is what the map should show when it retires.
    """
    step = 1.0 / settings.REPLAY_FRAME_HZ
    cursors = {number: 0 for number in projected}
    frames: list[dict[str, Any]] = []

    moment = t0
    while moment <= t1:
        cars: dict[str, Any] = {}
        for number, samples in projected.items():
            if not samples:
                continue
            index = cursors[number]
            while index + 1 < len(samples) and samples[index + 1][0] <= moment:
                index += 1
            cursors[number] = index

            current = samples[index]
            following = samples[index + 1] if index + 1 < len(samples) else None
            if current[0] > moment or moment - current[0] > SAMPLE_STALE_S:
                continue

            value, in_pit = current[1], current[2]
            if (
                following is not None
                and not in_pit
                and not following[2]
                and following[0] > current[0]
            ):
                fraction = min((moment - current[0]) / (following[0] - current[0]), 1.0)
                value = current[1] + (following[1] - current[1]) * fraction

            cars[str(number)] = (
                ["p", round(value, 1)] if in_pit else round(value % loop, 2)
            )
        frames.append({"t": round(moment - t0, 1), "cars": cars})
        moment += step
    return frames


# --------------------------------------------------------------------------
# Overtakes
# --------------------------------------------------------------------------
#: A car is not racing for this long either side of a visit to the pit lane.
#: Places handed over on the way in and taken back on the way out are the pit
#: cycle; counting those is how a quiet race produces a hundred overtakes.
PIT_SETTLE_S = 25.0

#: Once a car is ahead it has to stay ahead for this long for the move to have
#: happened. Fixes arrive every ~2.7 s and the frames interpolate between them,
#: so cars running a tenth apart swap on the map without passing each other.
OVERTAKE_SETTLE_S = 6.0

#: Being passed by this many cars on one lap is not a run of lost duels. It is
#: a car in trouble, and it is reported as that instead.
INCIDENT_MIN_PASSES = 4


def _is_ahead(a: float, b: float, loop: int) -> bool:
    """Whether progress ``a`` is in front of progress ``b`` on a closed loop.

    Positions wrap at the line, so "greater" is not "ahead". Two cars within
    half a lap of each other -- which any pair racing each other is -- are
    ordered by which way round the shorter gap runs.
    """
    return ((a - b) % loop) < loop / 2


def _overtakes(
    frames: list[dict[str, Any]],
    lap_state: dict[str, list[dict[str, Any]]],
    loop: int,
) -> list[dict[str, Any]]:
    """Every change of hands in the race, and the second it happened.

    Two sources, each used for what it is good for.

    The **per-lap order** decides *what* happened. It is timing data rather than
    reconstruction, so it is the authority on who finished a lap ahead of whom,
    and it carries a stop count -- which is how the pit cycle is told apart from
    a move on track. A car that gained places because the car ahead pitted has
    not overtaken anybody, and neither has a car whose own stop dropped it.

    The **frames** decide *when*. A lap boundary is a poor timestamp for a move
    made halfway round, and it cannot describe a move at all when it happened
    between two boundaries. Comparing the two cars' progress through the lap
    finds the moment one went by, to the second.
    """
    if not frames or loop <= 0 or not lap_state:
        return []

    # When each lap was being run, and where cars were during it.
    windows: dict[int, list[dict[str, Any]]] = {}
    for frame in frames:
        lap = frame.get("lap")
        if lap:
            windows.setdefault(int(lap), []).append(frame)

    # When each car was in the pit lane, so a stop can be kept out of the way.
    pit_moments: dict[str, list[float]] = {}
    for frame in frames:
        for number, value in (frame.get("cars") or {}).items():
            if isinstance(value, list):
                pit_moments.setdefault(number, []).append(float(frame.get("t") or 0.0))

    def near_pit(number: int, moment: float) -> bool:
        return any(abs(moment - stamp) <= PIT_SETTLE_S
                   for stamp in pit_moments.get(str(number), ()))

    numbers = sorted(int(lap) for lap in lap_state if str(lap).isdigit())
    events: list[dict[str, Any]] = []

    for lap in numbers:
        before = lap_state.get(str(lap - 1))
        after = lap_state.get(str(lap))
        if not before or not after:
            continue

        was = {row["number"]: row["position"] for row in before if row.get("position")}
        stops_was = {row["number"]: row.get("stops") or 0 for row in before}
        pitted = {
            row["number"]
            for row in after
            if (row.get("stops") or 0) > stops_was.get(row["number"], 0)
        }

        found: list[dict[str, Any]] = []
        for row in after:
            car, position = row.get("number"), row.get("position")
            if car is None or position is None or car in pitted:
                continue
            started = was.get(car)
            if started is None or started <= position:
                continue

            # Whom it got by: everyone it started behind and finished ahead of,
            # minus anyone who spent the lap in the pit lane.
            for other in after:
                passed, at = other.get("number"), other.get("position")
                if passed is None or at is None or passed in pitted:
                    continue
                if was.get(passed) is None or was[passed] >= started or at <= position:
                    continue
                moment = _moment_of_pass(windows.get(lap, ()), car, passed, loop)
                if moment is None or near_pit(car, moment) or near_pit(passed, moment):
                    continue
                found.append(
                    {
                        "t": round(moment, 1),
                        "lap": lap,
                        "kind": "pass",
                        "car": int(car),
                        "over": int(passed),
                        "position": int(position),
                    }
                )

        events.extend(_fold_incidents(found, was, after, lap))

    events.sort(key=lambda e: (e["t"], e["car"]))
    return events


def _fold_incidents(
    found: list[dict[str, Any]],
    was: dict[int, int],
    after: list[dict[str, Any]],
    lap: int,
) -> list[dict[str, Any]]:
    """Collapse "the whole field went past one car" into the one thing it was.

    When a car is passed by most of the grid on a single lap it did not lose a
    string of duels: it broke, or spun, or picked up a puncture. Reporting that
    as eleven overtakes is both wrong and useless -- the car in trouble is the
    story, and it is one event, not eleven.
    """
    by_victim: dict[int, list[dict[str, Any]]] = {}
    for event in found:
        by_victim.setdefault(event["over"], []).append(event)

    ends = {row["number"]: row.get("position") for row in after}
    kept: list[dict[str, Any]] = []
    for victim, passes in by_victim.items():
        if len(passes) < INCIDENT_MIN_PASSES:
            kept.extend(passes)
            continue
        kept.append(
            {
                "t": round(min(p["t"] for p in passes), 1),
                "lap": lap,
                "kind": "drop",
                "car": int(victim),
                "over": None,
                "from_position": was.get(victim),
                "position": ends.get(victim),
                "passed_by": len(passes),
            }
        )
    return kept


def _moment_of_pass(
    window: Iterable[dict[str, Any]],
    car: int,
    passed: int,
    loop: int,
) -> float | None:
    """When ``car`` got in front of ``passed`` and stayed there, in the lap.

    The last time the order was the old way round is the moment before the
    move, so the first frame after it is the move. A pair that never appears
    the old way round changed places before this lap's frames begin, and the
    start of the window is the closest honest answer.
    """
    mine, theirs = str(car), str(passed)
    first: float | None = None
    last_behind: float | None = None
    settled_at: float | None = None

    for frame in window:
        moment = float(frame.get("t") or 0.0)
        if first is None:
            first = moment
        cars = frame.get("cars") or {}
        here, there = cars.get(mine), cars.get(theirs)
        if here is None or there is None:
            continue
        if isinstance(here, list) or isinstance(there, list):
            continue                    # one of them is in the pit lane
        if _is_ahead(float(here), float(there), loop):
            if settled_at is None:
                settled_at = moment
        else:
            last_behind = moment
            settled_at = None

    if settled_at is not None and (last_behind is None or settled_at > last_behind):
        return settled_at
    return first


def _lap_timeline(laps: list[dict[str, Any]], t0: float) -> list[tuple[float, int]]:
    """``(offset_seconds, leader_lap)`` -- when each lap of the race began."""
    starts: dict[int, float] = {}
    for lap in laps:
        number = lap.get("lap_number")
        started = _parse_date(lap.get("date_start"))
        if number is None or started is None:
            continue
        offset = started.timestamp() - t0
        starts[int(number)] = min(starts.get(int(number), math.inf), offset)
    return sorted((offset, lap) for lap, offset in starts.items())


def _flag_timeline(control: list[dict[str, Any]], t0: float) -> list[tuple[float, str]]:
    """``(offset_seconds, state)`` from race-control messages."""
    from app.live import race_control as rc

    timeline: list[tuple[float, str]] = []
    for message in sorted(control, key=lambda m: str(m.get("date") or "")):
        moment = _parse_date(message.get("date"))
        if moment is None:
            continue
        state = rc.classify_openf1_message(message)
        if state is None:
            continue
        offset = moment.timestamp() - t0
        # Messages from before the session window belong to the build-up -- a
        # safety-car board on the grid, a pit-exit light -- and are not this
        # session's flag state. Carrying one in starts the race under yellow.
        if offset < 0:
            continue
        if timeline and timeline[-1][1] == state.value:
            continue
        timeline.append((offset, state.value))
    return timeline


def _pit_windows(
    pits: list[dict[str, Any]], laps: list[dict[str, Any]], t0: float
) -> dict[int, list[tuple[float, float]]]:
    """Per car, the ``(from, to)`` offsets during which it was in the pit lane.

    OpenF1 timestamps the stop itself, so the window is padded either side to
    cover the entry and exit runs -- which is the part worth watching.
    """
    windows: dict[int, list[tuple[float, float]]] = {}
    for stop in pits:
        number = stop.get("driver_number")
        moment = _parse_date(stop.get("date"))
        if number is None or moment is None:
            continue
        duration = float(stop.get("pit_duration") or 3.0)
        offset = moment.timestamp() - t0
        windows.setdefault(int(number), []).append((offset - 22.0, offset + duration + 22.0))
    return windows


def _annotate_frames(
    frames: list[dict[str, Any]],
    lap_timeline: list[tuple[float, int]],
    flag_timeline: list[tuple[float, str]],
    pit_windows: dict[int, list[tuple[float, float]]],
) -> None:
    """Stamp each frame with the lap, the flag state, and who is in the pits."""
    lap_cursor = flag_cursor = 0
    lap, flag = 0, "green"
    for frame in frames:
        moment = frame["t"]
        while lap_cursor < len(lap_timeline) and lap_timeline[lap_cursor][0] <= moment:
            lap = lap_timeline[lap_cursor][1]
            lap_cursor += 1
        while flag_cursor < len(flag_timeline) and flag_timeline[flag_cursor][0] <= moment:
            flag = flag_timeline[flag_cursor][1]
            flag_cursor += 1
        frame["lap"] = lap
        frame["flag"] = flag
        in_pit = [
            number
            for number, windows in pit_windows.items()
            if any(start <= moment <= end for start, end in windows)
        ]
        if in_pit:
            frame["pit"] = sorted(in_pit)


def _stint_index(stints: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    index: dict[int, list[dict[str, Any]]] = {}
    for stint in stints:
        number = stint.get("driver_number")
        if number is None:
            continue
        index.setdefault(int(number), []).append(stint)
    for rows in index.values():
        rows.sort(key=lambda s: s.get("lap_start") or 0)
    return index


def _lap_state(
    laps: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    intervals: list[dict[str, Any]],
    stints: dict[int, list[dict[str, Any]]],
    pits: list[dict[str, Any]],
    green_laps: set[int],
    t0: float,
) -> dict[str, list[dict[str, Any]]]:
    """Per lap, per car: the race state the prediction panel reasons over.

    Everything here is what a strategist would read off the timing screen --
    position, how quick the car is going, what it is running and how many stops
    it has taken -- plus the comparative figure that matters most: how the car's
    pace compares with the pace at the front.

    Two things make that comparison honest. Only green laps count towards a
    car's pace, because behind a safety car everyone is slow and none of it
    says anything about how quick the car is. And the yardstick is the pace of
    the leading quarter of the field rather than the field median: the median
    is dragged around by cars pitting, cars in traffic and cars nursing a
    problem, which is how every car on the grid can end up reading as quicker
    than "average".
    """
    by_driver: dict[int, dict[int, dict[str, Any]]] = {}
    for lap in laps:
        number, lap_number = lap.get("driver_number"), lap.get("lap_number")
        if number is None or lap_number is None:
            continue
        by_driver.setdefault(int(number), {})[int(lap_number)] = lap

    position_by_lap = _positions_by_lap(positions, laps, t0)
    gaps = _gaps_by_lap(intervals, laps, t0)
    stops = _stops_by_lap(pits, laps, t0)

    out: dict[str, list[dict[str, Any]]] = {}
    all_laps = sorted({int(l["lap_number"]) for l in laps if l.get("lap_number")})
    for lap_number in all_laps:
        rows: list[dict[str, Any]] = []
        for number, driver_laps in by_driver.items():
            if lap_number not in driver_laps:
                continue
            pace = _rolling_pace(driver_laps, lap_number, green_laps)
            compound, age = _tyre_at(stints.get(number, []), lap_number)
            rows.append(
                {
                    "number": number,
                    "position": position_by_lap.get(lap_number, {}).get(number),
                    "lap_time_s": _round(driver_laps[lap_number].get("lap_duration")),
                    "pace_s": _round(pace),
                    "compound": compound,
                    "tyre_age": age,
                    "stops": stops.get(lap_number, {}).get(number, 0),
                    "gap_to_leader_s": _round(gaps.get(lap_number, {}).get(number)),
                }
            )
        reference = _reference_pace([r["pace_s"] for r in rows if r["pace_s"]])
        for row in rows:
            # Comparative advantage: seconds a lap quicker than the front of
            # the field. Negative for most of the grid, which is the point --
            # only a handful of cars are genuinely quicker than the leaders.
            row["reference_pace_s"] = reference
            row["pace_delta_s"] = (
                _round(reference - row["pace_s"]) if reference and row["pace_s"] else None
            )
        rows.sort(key=lambda r: (r["position"] is None, r["position"] or 0))
        out[str(lap_number)] = rows
    return out


def _reference_pace(paces: list[float]) -> float | None:
    """The pace at the front: the median of the quickest quarter of the field.

    A single fastest lap is too noisy to measure a race against and the field
    median is not a benchmark at all, since half the field is slower than it by
    construction. The quickest quarter is what a car is actually racing.
    """
    if not paces:
        return None
    ordered = sorted(paces)
    quickest = ordered[: max(1, round(len(ordered) * REFERENCE_PACE_QUANTILE))]
    return round(statistics.median(quickest), 3)


def _rolling_pace(
    driver_laps: dict[int, dict[str, Any]],
    lap_number: int,
    green_laps: set[int],
) -> float | None:
    """Median of the recent green laps -- traffic, stops and neutralisations out.

    Falls back to the unfiltered window when a car has no recent green lap at
    all, which happens under a long safety car: a stale pace figure is more
    useful than none, and the caller has the flag state to judge it by.
    """
    window_start = max(1, lap_number - PACE_WINDOW_LAPS + 1)
    candidates = [
        (n, driver_laps[n])
        for n in range(window_start, lap_number + 1)
        if n in driver_laps
        and driver_laps[n].get("lap_duration")
        and not driver_laps[n].get("is_pit_out_lap")
    ]
    green = [float(lap["lap_duration"]) for n, lap in candidates if n in green_laps]
    window = green or [float(lap["lap_duration"]) for _n, lap in candidates]
    if not window:
        return None
    reference = statistics.median(window)
    clean = [t for t in window if t <= reference * PACE_OUTLIER_RATIO]
    return statistics.median(clean or window)


def _green_laps(
    laps: list[dict[str, Any]], flag_timeline: list[tuple[float, str]], t0: float
) -> set[int]:
    """Laps that ran green from start to finish.

    A lap is only green if nothing interrupted it: a safety car deployed
    mid-lap makes that lap useless as a measure of pace even though it began
    under green.
    """
    boundaries = _lap_timeline(laps, t0)
    if not boundaries:
        return set()

    green: set[int] = set()
    state = "green"
    cursor = 0
    for index, (start, lap_number) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else math.inf
        # Advance the flag state to the start of this lap.
        while cursor < len(flag_timeline) and flag_timeline[cursor][0] <= start:
            state = flag_timeline[cursor][1]
            cursor += 1
        interrupted = any(start < moment < end for moment, _s in flag_timeline[cursor:])
        if state == "green" and not interrupted:
            green.add(lap_number)
    return green


def _positions_by_lap(
    positions: list[dict[str, Any]], laps: list[dict[str, Any]], t0: float
) -> dict[int, dict[int, int]]:
    """Order at the end of each lap, from the running position feed."""
    timeline = _lap_timeline(laps, t0)
    ordered = sorted(
        (
            (_parse_date(p.get("date")), p.get("driver_number"), p.get("position"))
            for p in positions
        ),
        key=lambda p: (p[0] or datetime.min.replace(tzinfo=timezone.utc)),
    )
    out: dict[int, dict[int, int]] = {}
    current: dict[int, int] = {}
    cursor = 0
    for moment, number, position in ordered:
        if moment is None or number is None or position is None:
            continue
        offset = moment.timestamp() - t0
        while cursor < len(timeline) and timeline[cursor][0] <= offset:
            lap = timeline[cursor][1]
            if lap > 1:
                out[lap - 1] = dict(current)
            cursor += 1
        current[int(number)] = int(position)
    if timeline:
        out[timeline[-1][1]] = dict(current)
    return out


def _gaps_by_lap(
    intervals: list[dict[str, Any]], laps: list[dict[str, Any]], t0: float
) -> dict[int, dict[int, float]]:
    timeline = _lap_timeline(laps, t0)
    out: dict[int, dict[int, float]] = {}
    current: dict[int, float] = {}
    rows = sorted(intervals, key=lambda r: str(r.get("date") or ""))
    cursor = 0
    for row in rows:
        moment = _parse_date(row.get("date"))
        number, gap = row.get("driver_number"), row.get("gap_to_leader")
        if moment is None or number is None:
            continue
        offset = moment.timestamp() - t0
        while cursor < len(timeline) and timeline[cursor][0] <= offset:
            lap = timeline[cursor][1]
            if lap > 1:
                out[lap - 1] = dict(current)
            cursor += 1
        if isinstance(gap, (int, float)):
            current[int(number)] = float(gap)
    if timeline:
        out[timeline[-1][1]] = dict(current)
    return out


def _stops_by_lap(
    pits: list[dict[str, Any]], laps: list[dict[str, Any]], t0: float
) -> dict[int, dict[int, int]]:
    stop_laps: list[tuple[int, int]] = []
    for stop in pits:
        number, lap = stop.get("driver_number"), stop.get("lap_number")
        if number is None or lap is None:
            continue
        stop_laps.append((int(lap), int(number)))
    stop_laps.sort()

    out: dict[int, dict[int, int]] = {}
    running: dict[int, int] = {}
    all_laps = sorted({int(l["lap_number"]) for l in laps if l.get("lap_number")})
    cursor = 0
    for lap_number in all_laps:
        while cursor < len(stop_laps) and stop_laps[cursor][0] <= lap_number:
            number = stop_laps[cursor][1]
            running[number] = running.get(number, 0) + 1
            cursor += 1
        out[lap_number] = dict(running)
    return out


def _tyre_at(stints: list[dict[str, Any]], lap_number: int) -> tuple[str | None, int | None]:
    for stint in stints:
        start, end = stint.get("lap_start"), stint.get("lap_end")
        if start is None:
            continue
        if start <= lap_number and (end is None or lap_number <= end):
            age = (stint.get("tyre_age_at_start") or 0) + (lap_number - start)
            return stint.get("compound"), int(age)
    return None, None


def _control_log(control: list[dict[str, Any]], t0: float) -> list[dict[str, Any]]:
    from app.live import race_control as rc

    out: list[dict[str, Any]] = []
    for message in sorted(control, key=lambda m: str(m.get("date") or "")):
        moment = _parse_date(message.get("date"))
        if moment is None:
            continue
        state = rc.classify_openf1_message(message)
        text = str(message.get("message") or "")[:200]
        out.append(
            {
                "t": round(moment.timestamp() - t0, 1),
                "lap": message.get("lap_number"),
                "state": state.value if state else None,
                "severity": rc.severity_of(text, state),
                "message": text,
            }
        )
    return out


def _final_order(
    positions: list[dict[str, Any]],
    laps: list[dict[str, Any]],
    drivers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Classification as the position feed last reported it."""
    last: dict[int, tuple[str, int]] = {}
    for row in positions:
        number, position, date = row.get("driver_number"), row.get("position"), row.get("date")
        if number is None or position is None or date is None:
            continue
        number = int(number)
        if number not in last or str(date) > last[number][0]:
            last[number] = (str(date), int(position))

    laps_done: dict[int, int] = {}
    for lap in laps:
        number, lap_number = lap.get("driver_number"), lap.get("lap_number")
        if number is None or lap_number is None:
            continue
        laps_done[int(number)] = max(laps_done.get(int(number), 0), int(lap_number))

    by_number = {d["number"]: d for d in drivers}
    out = [
        {
            "number": number,
            "code": by_number.get(number, {}).get("code"),
            "name": by_number.get(number, {}).get("name"),
            "team": by_number.get(number, {}).get("team"),
            "colour": by_number.get(number, {}).get("colour"),
            "position": position,
            "laps": laps_done.get(number),
        }
        for number, (_date, position) in last.items()
    ]
    out.sort(key=lambda r: r["position"])
    return out


# --------------------------------------------------------------------------
def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _round(value: Any, places: int = 3) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, places) if math.isfinite(number) else None
