"""Turning position fixes into playback frames.

Cars are carried as *progress along* the reconstructed paths rather than as raw
coordinates, so interpolating between two fixes moves a car through the corners
it actually drove, at the speed it drove them -- rather than cutting across
them in a straight line.
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.data.replay.constants import PIT_LANE_MIN_DISTANCE, SAMPLE_STALE_S
from app.data.replay.geometry import _SpatialIndex


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
