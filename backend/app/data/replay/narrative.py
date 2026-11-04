"""What happened in the race, read back out of the laps.

Two kinds of thing live here. The first is the story a viewer watches for --
overtakes, flags, pit windows, the finishing order. The second is the per-lap
race state the prediction panel reasons over: position, rolling pace, tyre and
stop count.

Both are derived rather than reported. The feed says where cars were, not that
one passed another, so a pass is inferred from an order that changed and then
stayed changed -- which is also why the settle windows here matter.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from app.data.replay.constants import (
    INCIDENT_MIN_PASSES,
    PACE_OUTLIER_RATIO,
    PACE_WINDOW_LAPS,
    PIT_SETTLE_S,
    REFERENCE_PACE_QUANTILE,
)
from app.data.replay.shared import _parse_date, _round


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
