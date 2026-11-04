"""Reconstructing the circuit from a race's worth of position fixes.

The feed samples each car roughly every 2.7 seconds -- about thirty points per
lap, which on its own draws a polygon with the corners cut off. Two ideas
recover the shape. The sampling clock and the lap clock are independent, so
folding every green lap onto one lap catches each corner from many different
points; and re-placing every fix along the loop that pass produced, twice,
pulls the line back onto the geometry the cars actually drove.

The pit lane is traced separately, from the fixes around timed stops: distance
from the racing line cannot find it alone, because a car running wide at a fast
corner sits further off line than a car in the pits.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from app.data.replay.constants import (
    CLEAN_LAP_RATIO,
    PIT_APPROACH_S,
    PIT_LANE_MAX_DISTANCE,
    PIT_TRACE_MIN_DISTANCE,
    REFINE_PASSES,
    TRACK_POINT_SPACING,
    TRACK_POINTS,
)
from app.data.replay.shared import ReplayUnavailable, _distance, _parse_date


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
