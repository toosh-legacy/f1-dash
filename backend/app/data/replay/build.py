"""Building a bundle: fetch the session, then assemble it.

This is the only module that talks to OpenF1 during a build, and the only one
that knows the order the pieces go together in -- geometry first, because
everything else is expressed in terms of it, then frames, then the narrative
read back out of the laps.

Expensive by nature: one request per car over tens of thousands of samples. It
runs as a background job and reports progress by stage, so a caller can watch
it rather than wait blindly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from app.config import settings
from app.data.openf1_client import OpenF1Client
from app.data.replay.constants import BUNDLE_VERSION, _TEAM_FALLBACK_COLOUR
from app.data.replay.frames import _annotate_frames, _build_frames, _project
from app.data.replay.geometry import _build_track
from app.data.replay.narrative import (
    _control_log,
    _final_order,
    _flag_timeline,
    _green_laps,
    _lap_state,
    _lap_timeline,
    _overtakes,
    _pit_windows,
    _stint_index,
)
from app.data.replay.shared import Progress, ReplayUnavailable, _parse_date, log
from app.data.replay.store import _store_bundle, load_bundle


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
