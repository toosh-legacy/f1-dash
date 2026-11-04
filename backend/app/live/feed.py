"""The live map: every car's position, and the timing, while a session runs.

The prediction loops (``race_loop``, ``qualifying_loop``) say who finishes
where. This says where everyone *is*, so the dashboard can draw a live session
exactly the way it plays back a finished one -- the same frames, the same
lap events, the same flags -- and the only difference is where the next
frame comes from.

Two cadences, both inside one OpenF1 request budget:

* **positions**, every few seconds -- the whole field's ``location`` samples
  since the last poll, carried as progress along the circuit's drawn path
  (see :mod:`app.data.replay.frames`) and resampled to one frame a second;
* **timing**, every twenty seconds or so -- laps and race control, turned into
  the lap events, session parts, flag and race-control log a replay bundle
  carries.

The circuit's drawn path comes from any replay already built at the same
track. A circuit does not change between visits, and the feed's coordinate
frame is the circuit's own, so last year's race lays out this year's session.
Until one exists the feed still sends the timing; the cars wait for a map.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.data.openf1_client import OpenF1Client
from app.data.replay import timing
from app.data.replay.build import _clean_trace, _drivers, _session_meta
from app.data.replay.frames import _build_frames, _project
from app.data.replay.narrative import _control_log, _flag_timeline, _lap_timeline
from app.data.replay.shared import _parse_date
from app.live.base import LiveLoop, LoopManager

log = logging.getLogger(__name__)

#: How much of each car's recent trace is re-projected every poll. Long enough
#: to interpolate across a gap in the feed, short enough to stay cheap.
TRACE_KEEP_S = 30.0

#: Frames kept for a viewer who joins mid-session: ten minutes at 1 Hz.
FRAME_HISTORY = 600

#: The first poll looks back this far, so a viewer gets cars straight away.
FIRST_LOOKBACK_S = 15.0

#: How often to look for a built layout at this circuit while there is none.
LAYOUT_RETRY_S = 60.0


class PositionFeed(LiveLoop):
    kind = "feed"

    def __init__(
        self,
        session_id: int,
        session_key: int,
        *,
        client: OpenF1Client | None = None,
        poll_interval_s: float | None = None,
    ) -> None:
        super().__init__(
            session_id,
            session_key,
            poll_interval_s=poll_interval_s or settings.LIVE_POSITION_INTERVAL_S,
            client=client,
        )
        self._lock = threading.Lock()
        self.meta: dict[str, Any] | None = None
        self.t0: float | None = None
        self.drivers: list[dict[str, Any]] = []
        self.track: dict[str, Any] | None = None
        self.track_source: int | None = None
        self.frames: deque[dict[str, Any]] = deque(maxlen=FRAME_HISTORY)
        self.lap_events: list[list[float]] = []
        self.segments: list[dict[str, Any]] = []
        self.control: list[dict[str, Any]] = []
        self.flag = "green"
        self.lap = 0
        self.total_laps = 0

        self._traces: dict[int, list[tuple[float, float, float]]] = {}
        self._cursor: datetime | None = None
        self._emitted_until: float | None = None   # replay-clock second last framed
        self._lap_timeline: list[tuple[float, int]] = []
        self._flag_timeline: list[tuple[float, str]] = []
        self._last_timing = 0.0
        self._last_layout_try = 0.0

    # -- one tick ------------------------------------------------------------
    def poll_once(self) -> None:
        if self.meta is None:
            self._load_session()
        if self.track is None and time.monotonic() - self._last_layout_try > LAYOUT_RETRY_S:
            self._load_layout()
        if time.monotonic() - self._last_timing >= settings.LIVE_TIMING_INTERVAL_S:
            self._poll_timing()
        self._poll_positions()

    def _load_session(self) -> None:
        meta = _session_meta(self.client, self.session_key)
        start = _parse_date(meta.get("date_start"))
        if start is None:
            raise RuntimeError(f"session {self.session_key} has no start time yet")
        drivers = _drivers(self.client, self.session_key)
        with self._lock:
            self.meta, self.t0, self.drivers = meta, start.timestamp(), drivers
        self.status.last_context = f"{meta.get('circuit')} {meta.get('name')}"
        self.publish({"type": "live_session", **self._session_part()})

    def _load_layout(self) -> None:
        self._last_layout_try = time.monotonic()
        circuit = (self.meta or {}).get("circuit") or ""
        found = layout_for(circuit)
        if found is None:
            return
        track, source = found
        with self._lock:
            self.track, self.track_source = track, source
        log.info("live feed %s: laid out on replay %s", self.session_key, source)
        self.publish({"type": "live_track", "track": track, "source_session_key": source})

    # -- timing ----------------------------------------------------------------
    def _poll_timing(self) -> None:
        self._last_timing = time.monotonic()
        laps = self.client.laps(self.session_key)
        control = self.client.race_control(self.session_key)
        t0 = self.t0 or 0.0
        name = str((self.meta or {}).get("name") or "")
        lap_timeline = _lap_timeline(laps, t0)
        flag_timeline = _flag_timeline(control, t0)
        with self._lock:
            self._lap_timeline = lap_timeline
            self._flag_timeline = flag_timeline
            self.lap = lap_timeline[-1][1] if lap_timeline else 0
            self.total_laps = max(
                (int(l["lap_number"]) for l in laps if l.get("lap_number")), default=0
            )
            self.flag = flag_timeline[-1][1] if flag_timeline else "green"
            self.lap_events = timing.lap_events(laps, t0)
            self.segments = timing.segments(control, t0, name)
            self.control = _control_log(control, t0)
        self.publish({"type": "live_timing", **self._timing_part()})

    # -- positions -------------------------------------------------------------
    def _poll_positions(self) -> None:
        if self.t0 is None:
            return
        since = self._cursor or (datetime.now(timezone.utc) - timedelta(seconds=FIRST_LOOKBACK_S))
        rows = self.client.location_since(self.session_key, since.isoformat())
        if not rows:
            return

        newest = self._cursor
        by_car: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            moment = _parse_date(row.get("date"))
            if moment is not None and (newest is None or moment > newest):
                newest = moment
            try:
                by_car.setdefault(int(row["driver_number"]), []).append(row)
            except (KeyError, TypeError, ValueError):
                continue
        self._cursor = newest

        horizon = newest.timestamp() - TRACE_KEEP_S if newest else 0.0
        for number, car_rows in by_car.items():
            trace = self._traces.get(number, []) + _clean_trace(car_rows)
            trace.sort(key=lambda sample: sample[0])
            self._traces[number] = [s for s in trace if s[0] >= horizon]

        if self.track is None or newest is None:
            return
        frames = self._frames_until(newest.timestamp())
        if frames:
            with self._lock:
                self.frames.extend(frames)
            self.publish({"type": "live_frames", "frames": frames})

    def _frames_until(self, newest_epoch: float) -> list[dict[str, Any]]:
        """Frames for every whole second not yet sent, up to a second short of
        the newest fix -- so each frame has a fix after it to interpolate to."""
        t0 = self.t0 or 0.0
        last = float(int(newest_epoch - t0 - 1.0))
        first = (self._emitted_until + 1.0) if self._emitted_until is not None else last - FIRST_LOOKBACK_S + 2
        if last < first:
            return []
        path = [tuple(p) for p in self.track["path"]]
        pit_path = [tuple(p) for p in self.track.get("pit_path") or []]
        relative = {
            number: [(moment - t0, x, y) for moment, x, y in trace]
            for number, trace in self._traces.items() if trace
        }
        frames = _build_frames(_project(relative, path, pit_path), len(path), first, last)
        for frame in frames:
            frame["t"] = round(frame["t"] + first, 1)
            frame["lap"] = _at(self._lap_timeline, frame["t"], 0)
            frame["flag"] = _at(self._flag_timeline, frame["t"], "green")
        self._emitted_until = last
        return frames

    # -- what a viewer joining now needs -----------------------------------------
    def _session_part(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_key": self.session_key,
            "session": self.meta,
            "t0": self.t0,
            "drivers": self.drivers,
        }

    def _timing_part(self) -> dict[str, Any]:
        return {
            "lap": self.lap,
            "total_laps": self.total_laps,
            "flag": self.flag,
            "lap_events": self.lap_events,
            "segments": self.segments,
            "race_control": self.control,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._session_part(),
                **self._timing_part(),
                "track": self.track,
                "track_source": self.track_source,
                "frames": list(self.frames),
                "frame_interval_s": round(1.0 / settings.REPLAY_FRAME_HZ, 3),
            }


def _at(timeline: list[tuple[float, Any]], moment: float, default: Any) -> Any:
    value = default
    for offset, state in timeline:
        if offset > moment:
            break
        value = state
    return value


def layout_for(circuit: str) -> tuple[dict[str, Any], int] | None:
    """The drawn path of ``circuit`` from any built replay there, if one is."""
    if not circuit:
        return None
    # The replay library lives behind the API's dependency helpers; imported
    # here, not at the top, so the live package does not load the web layer.
    from app.api.replays import _bundle_at
    from app.db.database import session_scope

    with session_scope() as db:
        bundle = _bundle_at(circuit, db)
    if bundle is None or not bundle.get("track"):
        return None
    return bundle["track"], int((bundle.get("session") or {}).get("session_key") or 0)


def feed_for_key(session_key: int) -> "PositionFeed | None":
    """The running feed following OpenF1 session ``session_key``, if any."""
    for status in feeds.statuses():
        if status.get("session_key") == session_key:
            found = feeds.get(int(status["session_id"]))
            if isinstance(found, PositionFeed) and found.t0 is not None:
                return found
    return None


#: Running feeds, one per session. Kept apart from the prediction loops,
#: which are also keyed by session: a race runs one of each.
feeds = LoopManager()
