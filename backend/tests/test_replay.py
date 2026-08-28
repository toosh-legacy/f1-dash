"""Replay reconstruction and the projection built on top of it.

The geometry is tested against a synthetic circuit rather than a recorded one,
because the interesting property is not "does Hungary come out right" but "does
a sparse, jittery position feed come back as the shape it was sampled from".
The fixtures below sample a known oval the way OpenF1 samples a real circuit:
rarely, unevenly, and with the same fix repeated until the next update.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.data import replay as R


TRACK_RADIUS = 4000.0
LAP_DURATION = 80.0
FIX_INTERVAL = 2.7  # seconds between position updates, as the real feed gives
START = datetime(2026, 7, 26, 13, 0, tzinfo=timezone.utc)


def oval(phase: float) -> tuple[float, float]:
    """A known closed shape: an ellipse, so reconstruction has something to miss."""
    angle = phase * 2 * math.pi
    return (TRACK_RADIUS * math.cos(angle), TRACK_RADIUS * 0.6 * math.sin(angle))


def synthetic_trace(number: int, laps: int, *, offset: float = 0.0):
    """Fixes for one car, sampled far more slowly than it moves.

    Each car starts its sampling clock at a different offset, which is what
    makes the fold work: no two cars catch the lap at the same places.
    """
    trace = []
    epoch = START.timestamp() + offset
    moment = epoch
    end = epoch + laps * LAP_DURATION
    while moment < end:
        phase = ((moment - epoch) % LAP_DURATION) / LAP_DURATION
        x, y = oval(phase)
        # The feed repeats a fix until the car's next update.
        for repeat in range(3):
            trace.append((moment + repeat * 0.3, x, y))
        moment += FIX_INTERVAL
    return trace


def synthetic_laps(number: int, laps: int, *, offset: float = 0.0):
    return [
        {
            "driver_number": number,
            "lap_number": index + 1,
            "lap_duration": LAP_DURATION,
            "date_start": (
                START + timedelta(seconds=offset + index * LAP_DURATION)
            ).isoformat(),
            "is_pit_out_lap": False,
        }
        for index in range(laps)
    ]


@pytest.fixture
def race():
    traces, laps = {}, []
    for index, number in enumerate((1, 4, 16, 44)):
        traces[number] = synthetic_trace(number, 20, offset=index * 0.7)
        laps.extend(synthetic_laps(number, 20, offset=index * 0.7))
    return traces, laps


class TestCircuitReconstruction:
    def test_a_sparse_feed_folds_back_into_the_shape_it_sampled(self, race):
        traces, laps = race
        line = R._racing_line(traces, laps)

        assert len(line) > 100, "the fold should be far denser than any single lap"
        for x, y in line:
            radius = math.hypot(x / TRACK_RADIUS, y / (TRACK_RADIUS * 0.6))
            assert 0.9 < radius < 1.1, "reconstruction drifted off the sampled shape"

    def test_the_loop_closes(self, race):
        traces, laps = race
        line = R._racing_line(traces, laps)
        span = max(R._distance(line[0], point) for point in line)
        assert R._distance(line[0], line[-1]) < span * 0.2

    def test_a_single_lap_is_too_sparse_to_draw(self, race):
        """The reason the fold exists: one lap is about thirty fixes."""
        traces, _laps = race
        one_lap = [p for p in traces[1] if p[0] < START.timestamp() + LAP_DURATION]
        distinct = {(x, y) for _t, x, y in one_lap}
        assert len(distinct) < 40

    def test_safety_car_laps_are_not_folded_in(self):
        """A slow lap is a different line, and would blur the circuit."""
        laps = synthetic_laps(1, 5)
        laps.append(
            {
                "driver_number": 1,
                "lap_number": 6,
                "lap_duration": LAP_DURATION * 1.8,
                "date_start": START.isoformat(),
                "is_pit_out_lap": False,
            }
        )
        kept = R._representative_laps(laps)
        assert all(lap["lap_duration"] == LAP_DURATION for lap in kept)


class TestPitLane:
    def test_traced_from_the_cars_that_stopped(self, race):
        traces, laps = race
        line = R._racing_line(traces, laps)

        # Car 4 peels off for one stop: the same stretch of track, 400 units to
        # the inside of it, which is what a pit lane looks like to the feed.
        stop_at = START.timestamp() + 5 * LAP_DURATION
        detour = []
        for step in range(40):
            phase = 0.55 + (step / 40.0) * 0.15  # the bottom of the oval
            x, y = oval(phase)
            detour.append((stop_at - 20 + step, x, y + 400.0))
        traces[4] = sorted(traces[4] + detour, key=lambda p: p[0])

        pits = [
            {
                "driver_number": 4,
                "date": datetime.fromtimestamp(stop_at, timezone.utc).isoformat(),
                "pit_duration": 2.5,
                "lap_number": 5,
            }
        ]
        lane = R._pit_lane(traces, line, pits)
        assert lane, "the detour around a timed stop is the pit lane"
        # The lane is inside the oval, where the car went, not on the racing line.
        assert all(y < 0 for _x, y in lane), "the lane sits on the stretch it was traced from"
        assert all(
            abs(math.hypot(x / TRACK_RADIUS, y / (TRACK_RADIUS * 0.6)) - 1.0) > 0.02
            for x, y in lane
        ), "the lane must be off the racing line, not on it"

    def test_no_stops_means_no_pit_lane(self, race):
        traces, laps = race
        assert R._pit_lane(traces, R._racing_line(traces, laps), []) == []

    def test_a_wrapped_lane_is_cut_at_its_widest_gap(self):
        """Pit entry sits before the line and exit after it, so the run wraps."""
        placed = [(0.02, 1.0, 1.0), (0.05, 2.0, 2.0), (0.94, 3.0, 3.0), (0.97, 4.0, 4.0)]
        rotated = R._rotate_to_gap(placed)
        assert [point[1] for point in rotated] == [3.0, 4.0, 1.0, 2.0]


class TestPlayback:
    def _projected(self, line):
        traces = {7: [(0.0, *line[0]), (10.0, *line[5])]}
        return R._project(traces, line, [])

    def test_progress_interpolates_between_sparse_fixes(self, race):
        traces, laps = race
        line = R._racing_line(traces, laps)
        projected = self._projected(line)
        frames = R._build_frames(projected, len(line), 0.0, 10.0)

        values = [frame["cars"].get("7") for frame in frames]
        assert all(isinstance(value, float) or isinstance(value, int) for value in values)
        assert values[0] < values[len(values) // 2] < values[-1], "a car must move between fixes"

    def test_a_car_that_stops_reporting_leaves_the_map(self, race):
        traces, laps = race
        line = R._racing_line(traces, laps)
        projected = R._project({7: [(0.0, *line[0])]}, line, [])
        frames = R._build_frames(projected, len(line), 0.0, R.SAMPLE_STALE_S + 10)

        assert "7" in frames[0]["cars"]
        assert "7" not in frames[-1]["cars"], "a retired car should not sit frozen on track"

    def test_crossing_the_line_does_not_run_the_car_backwards(self, race):
        """Unwrapping is what keeps interpolation from reversing at start/finish."""
        traces, laps = race
        line = R._racing_line(traces, laps)
        last, first = line[-2], line[2]
        projected = R._project({7: [(0.0, *last), (4.0, *first)]}, line, [])
        values = [value for _t, value, _pit in projected[7]]
        assert values[1] > values[0], "progress must keep climbing through the line"

    def test_the_session_window_ignores_garage_fixes_from_the_day_before(self):
        yesterday = START.timestamp() - 86_400
        traces = {1: [(yesterday, 0.0, 0.0), (START.timestamp() + 60, 1.0, 1.0)]}
        meta = {
            "date_start": START.isoformat(),
            "date_end": (START + timedelta(hours=2)).isoformat(),
        }
        first, last = R._session_window(traces, meta)
        assert first == START.timestamp()
        assert last - first <= 7200


class TestCatalogue:
    class _Client:
        def __init__(self, rows):
            self.rows = rows

        def sessions(self, year=None, **_filters):
            return self.rows

        def close(self):
            pass

    def test_only_races_that_have_started_are_listed(self):
        rows = [
            {"session_key": 1, "session_type": "Race", "session_name": "Race",
             "date_start": "2026-03-08T04:00:00+00:00", "circuit_short_name": "Melbourne"},
            {"session_key": 2, "session_type": "Race", "session_name": "Race",
             "date_start": "2026-12-06T13:00:00+00:00", "circuit_short_name": "Yas Marina"},
            {"session_key": 3, "session_type": "Qualifying", "session_name": "Qualifying",
             "date_start": "2026-03-07T05:00:00+00:00", "circuit_short_name": "Melbourne"},
        ]
        entries = R.catalogue(
            2026,
            client=self._Client(rows),
            now=datetime(2026, 8, 28, tzinfo=timezone.utc),
        )
        assert [entry.session_key for entry in entries] == [1]

    def test_sprints_can_be_excluded(self):
        rows = [
            {"session_key": 1, "session_type": "Race", "session_name": "Sprint",
             "date_start": "2026-03-14T03:00:00+00:00", "circuit_short_name": "Shanghai"},
            {"session_key": 2, "session_type": "Race", "session_name": "Race",
             "date_start": "2026-03-15T07:00:00+00:00", "circuit_short_name": "Shanghai"},
        ]
        entries = R.catalogue(
            2026,
            client=self._Client(rows),
            include_sprints=False,
            now=datetime(2026, 8, 28, tzinfo=timezone.utc),
        )
        assert [entry.session_key for entry in entries] == [2]
