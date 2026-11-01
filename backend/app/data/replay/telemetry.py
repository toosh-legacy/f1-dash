"""Car telemetry for a replay, fetched on demand and a window at a time.

A bundle carries where every car is, not what it is doing. Speed, gear,
throttle and brake come from OpenF1's ``car_data`` feed at roughly four samples
a second -- for a whole race that is over half a million rows, which is far too
much to put in a bundle that every viewer downloads to watch every car.

Only one car is ever being looked at, though. So the dashboard asks for that car
and a short window around the playhead, and this module fetches that window
from OpenF1, trims it to the fields the dashboard shows, and keeps it. A window
of a finished session never changes, so a cached one is right forever.

Times are seconds on the replay's own clock. That clock starts at the
scheduled session start (see ``build._session_window``); a bundle built after
this module existed records the instant explicitly as ``t0``, and an older one
falls back to the schedule, which is the same instant.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.data.openf1_client import OpenF1Client
from app.data.replay.shared import _parse_date

#: How much replay time one fetch covers. Long enough that a car being followed
#: at 60x does not ask more than about once every two seconds; short enough that
#: the first answer arrives quickly.
WINDOW_S = 120

#: Windows kept in memory. One car across a whole race is about sixty of them.
CACHE_SIZE = 400

_cache: "OrderedDict[tuple[int, int, int], list[list[float]]]" = OrderedDict()
_lock = threading.Lock()
_client: OpenF1Client | None = None


def session_t0(bundle: dict[str, Any]) -> datetime | None:
    """The wall-clock instant the replay's t=0 stands for."""
    if bundle.get("t0") is not None:
        return datetime.fromtimestamp(float(bundle["t0"]), tz=timezone.utc)
    return _parse_date((bundle.get("session") or {}).get("date_start"))


def window_index(t: float) -> int:
    return int(t // WINDOW_S)


def telemetry_window(
    session_key: int, number: int, index: int, t0: datetime
) -> list[list[float]]:
    """One window of one car's telemetry as compact rows.

    Each row is ``[t, speed_kph, gear, throttle_pct, brake_pct, rpm]`` with
    ``t`` on the replay clock. Empty when the car was not running.
    """
    key = (session_key, number, index)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    start = t0 + timedelta(seconds=index * WINDOW_S)
    end = start + timedelta(seconds=WINDOW_S)
    rows = _openf1().car_data(
        session_key,
        driver_number=number,
        **{"date>": start.isoformat(), "date<": end.isoformat()},
    )
    out = _compact(rows, t0)

    with _lock:
        _cache[key] = out
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return out


def _compact(rows: list[dict[str, Any]], t0: datetime) -> list[list[float]]:
    out: list[list[float]] = []
    for row in rows:
        moment = _parse_date(row.get("date"))
        if moment is None:
            continue
        out.append([
            round((moment - t0).total_seconds(), 2),
            _number(row.get("speed")),
            _number(row.get("n_gear")),
            # The feed writes 104 for "no reading" on throttle and brake; a
            # pedal cannot be pressed past 100 percent, so anything above it is
            # clipped rather than shown as more than flat out.
            min(_number(row.get("throttle")), 100.0),
            min(_number(row.get("brake")), 100.0),
            _number(row.get("rpm")),
        ])
    out.sort(key=lambda r: r[0])
    return out


def _number(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _openf1() -> OpenF1Client:
    # One client for the process: it owns the throttle that keeps every caller
    # inside OpenF1's rate limit, and that only works if they share it.
    global _client
    with _lock:
        if _client is None:
            _client = OpenF1Client()
        return _client
