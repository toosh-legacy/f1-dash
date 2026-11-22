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

A kept window is a file under ``REPLAY_DIR`` *and* a row in the durable file
store (:mod:`app.storage.files`), under the same relative path as its key. On a
function the file is gone with the instance; the row is what saves asking
OpenF1 again -- which, while any session is live, a free caller cannot do.
"""
from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.data.openf1_client import OpenF1Client
from app.data.replay.shared import _parse_date
from app.storage import files

log = logging.getLogger(__name__)

#: How much replay time one fetch covers. Long enough that a car being followed
#: at 60x does not ask more than about once every two seconds; short enough that
#: the first answer arrives quickly.
WINDOW_S = 120

#: Windows kept in memory. One car across a whole race is about sixty of them.
CACHE_SIZE = 400

#: A window that ended less than this long ago may still be filling in --
#: a live session, or one that has only just finished -- so it is served but
#: not kept. OpenF1 publishes car data a few seconds behind the track.
SETTLE_S = 120.0

#: Bumped when the row format changes, so windows written by an older build
#: are fetched again rather than served wrong.
CACHE_VERSION = "v2"

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
    ``t`` on the replay clock; a pedal with no reading is ``None``. Empty when
    the car was not running.
    """
    key = (session_key, number, index)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    # Kept on disk too: a window of a finished session never changes, and
    # fetching it again after a restart -- or while OpenF1 is closed to
    # unauthenticated callers during a live session -- is what left the probe
    # reading dashes.
    stored = _read(key)
    if stored is not None:
        _remember(key, stored)
        return stored

    start = t0 + timedelta(seconds=index * WINDOW_S)
    end = start + timedelta(seconds=WINDOW_S)
    rows = _openf1().car_data(
        session_key,
        driver_number=number,
        **{"date>": start.isoformat(), "date<": end.isoformat()},
    )
    out = _compact(rows, t0)

    if time.time() - end.timestamp() > SETTLE_S:
        _remember(key, out)
        _write(key, out)
    return out


def _remember(key: tuple[int, int, int], rows: list[list[float]]) -> None:
    with _lock:
        _cache[key] = rows
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


def _store_key(key: tuple[int, int, int]) -> str:
    """The window's key in the file store: its path below ``REPLAY_DIR``."""
    session_key, number, index = key
    return f"telemetry/{CACHE_VERSION}/{session_key}/{number}/{index}.json.gz"


def _path(key: tuple[int, int, int]):
    return settings.REPLAY_DIR / _store_key(key)


def _kept(key: tuple[int, int, int]) -> bool:
    """Whether a window is kept anywhere, without downloading it."""
    return files.exists(_store_key(key), _path(key))


def _read(key: tuple[int, int, int]) -> list[list[float]] | None:
    path = _path(key)
    # The listing check first: most windows asked for on a fresh instance were
    # never stored, and a cached listing answers that without a query apiece.
    if not path.exists() and _kept(key):
        files.materialize(_store_key(key), path)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("discarding unreadable telemetry window %s: %s", path, exc)
        return None


def _encode(rows: list[list[float]]) -> bytes:
    return gzip.compress(json.dumps(rows, separators=(",", ":")).encode("utf-8"), mtime=0)


def _write(
    key: tuple[int, int, int], rows: list[list[float]], *, publish: bool = True
) -> bytes | None:
    """Keep a window on disk and, unless ``publish`` is off, in the file store.

    Returns the encoded bytes, so a bulk caller can publish many in one go.
    Failing to keep a window is only ever slower, never wrong, so neither a
    disk nor a database error is raised.
    """
    path = _path(key)
    data = _encode(rows)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    except OSError as exc:  # a cache that cannot be written is only slower
        log.warning("could not keep telemetry window %s: %s", path, exc)
    if publish:
        try:
            files.publish_bytes(_store_key(key), data)
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning("could not store telemetry window %s: %s", _store_key(key), exc)
    return data


def _compact(rows: list[dict[str, Any]], t0: datetime) -> list[list[float | None]]:
    dated = []
    for row in rows:
        moment = _parse_date(row.get("date"))
        if moment is not None:
            dated.append((moment, row))
    dated.sort(key=lambda pair: pair[0])

    out: list[list[float | None]] = []
    # The feed writes 104 for "no reading" on throttle and brake. A parked car
    # reads 104 on both for minutes at a time: that is genuinely no reading.
    # A moving car drops the odd single sample to 104 too -- a glitch, not the
    # driver lifting -- so there the last real reading is held, rather than
    # the gauge blinking to a dash at 340 km/h.
    last = {"throttle": None, "brake": None}
    for moment, row in dated:
        speed = _number(row.get("speed"))
        pedals = []
        for name in ("throttle", "brake"):
            reading = _number(row.get(name))
            if reading > 100.0:
                reading = last[name] if speed > 0 else None
            else:
                last[name] = reading
            pedals.append(reading)
        out.append([
            round((moment - t0).total_seconds(), 2),
            speed,
            _number(row.get("n_gear")),
            pedals[0],
            pedals[1],
            _number(row.get("rpm")),
        ])
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


def prefetch_session(
    session_key: int,
    bundle: dict[str, Any],
    *,
    client: OpenF1Client | None = None,
    report: Any = None,
) -> dict[str, int]:
    """Fetch every car's telemetry for a whole finished session and keep it.

    One request per car rather than one per car per two minutes, done when the
    replay is built. After that the session's telemetry never depends on
    OpenF1 again -- which matters because a free caller is locked out of
    every session, past ones included, while any session is live, and a
    sponsor's minute budget is better spent on the live one.

    Every window of the session is written, empty ones too, so a car that was
    in the garage reads as "not on track" instead of asking OpenF1 again.
    Returns windows written per car; skips cars already complete.
    """
    report = report or (lambda _stage, _detail=None: None)
    t0 = session_t0(bundle)
    duration = float(bundle.get("duration_s") or 0)
    if t0 is None or duration <= 0:
        return {}
    end = t0 + timedelta(seconds=duration)
    if time.time() - end.timestamp() < SETTLE_S:
        return {}  # still filling in; the on-demand path handles it
    last = window_index(duration)
    client = client or _openf1()
    written: dict[int, int] = {}
    drivers = bundle.get("drivers") or []
    for done, driver in enumerate(drivers, start=1):
        number = int(driver["number"])
        if all(_kept((session_key, number, i)) for i in range(last + 1)):
            continue
        try:
            rows = client.car_data(
                session_key,
                driver_number=number,
                **{"date>": t0.isoformat(), "date<": end.isoformat()},
            )
        except Exception as exc:  # noqa: BLE001 - one car must not lose the rest
            log.warning("telemetry prefetch for car %s of %s failed: %s", number, session_key, exc)
            continue
        by_window: dict[int, list[list[float]]] = {i: [] for i in range(last + 1)}
        for row in _compact(rows, t0):
            index = window_index(row[0])
            if index in by_window:
                by_window[index].append(row)
        # One transaction per car rather than one per window: sixty-odd round
        # trips to a remote database would cost more than the fetch did.
        batch = [
            (_store_key((session_key, number, index)),
             _write((session_key, number, index), chunk, publish=False))
            for index, chunk in by_window.items()
        ]
        try:
            files.publish_many(batch)
        except Exception as exc:  # noqa: BLE001 - the disk copy still stands
            log.warning("could not store telemetry for car %s of %s: %s", number, session_key, exc)
        written[number] = len(by_window)
        report("telemetry", {"car": number, "done": done, "of": len(drivers)})
    return written
