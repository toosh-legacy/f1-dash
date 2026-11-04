"""Replay telemetry: one car, one window of replay time, fetched once."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.data.replay import telemetry


T0 = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)


class _FakeOpenF1:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def car_data(self, session_key, **filters):
        self.calls.append((session_key, filters))
        return self.rows


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch, tmp_path):
    # Windows are kept on disk as well as in memory; give each test its own.
    monkeypatch.setattr(telemetry.settings, "REPLAY_DIR", tmp_path)
    telemetry._cache.clear()
    yield
    telemetry._cache.clear()


def test_t0_prefers_the_recorded_instant_and_falls_back_to_the_schedule():
    recorded = {"t0": T0.timestamp() + 30, "session": {"date_start": T0.isoformat()}}
    assert telemetry.session_t0(recorded).timestamp() == T0.timestamp() + 30
    older = {"session": {"date_start": T0.isoformat()}}
    assert telemetry.session_t0(older) == T0
    assert telemetry.session_t0({}) is None


def test_rows_are_put_on_the_replay_clock_and_no_reading_is_handled(monkeypatch):
    fake = _FakeOpenF1([
        {"date": "2026-07-05T14:02:01.500000+00:00", "speed": 287, "n_gear": 7,
         "throttle": 104, "brake": 0, "rpm": 11329},
        {"date": "2026-07-05T14:02:00.250000+00:00", "speed": 290, "n_gear": 7,
         "throttle": 100, "brake": 104, "rpm": 11400},
    ])
    monkeypatch.setattr(telemetry, "_openf1", lambda: fake)

    rows = telemetry.telemetry_window(11326, 16, 1, T0)

    # Sorted by time, seconds since t0. The feed's 104 ("no reading") on a
    # moving car holds the last real value; with none yet, it stays empty.
    assert rows == [
        [120.25, 290.0, 7.0, 100.0, None, 11400.0],
        [121.5, 287.0, 7.0, 100.0, 0.0, 11329.0],
    ]
    _, filters = fake.calls[0]
    assert filters["driver_number"] == 16
    assert filters["date>"].startswith("2026-07-05T14:02:00")
    assert filters["date<"].startswith("2026-07-05T14:04:00")


def test_a_window_is_fetched_once(monkeypatch):
    fake = _FakeOpenF1([])
    monkeypatch.setattr(telemetry, "_openf1", lambda: fake)
    telemetry.telemetry_window(11326, 16, 3, T0)
    telemetry.telemetry_window(11326, 16, 3, T0)
    telemetry.telemetry_window(11326, 44, 3, T0)
    assert len(fake.calls) == 2


def test_window_index_follows_the_window_length():
    assert telemetry.window_index(0) == 0
    assert telemetry.window_index(telemetry.WINDOW_S - 0.1) == 0
    assert telemetry.window_index(telemetry.WINDOW_S) == 1


def test_a_window_survives_a_restart(monkeypatch):
    fake = _FakeOpenF1([{"date": "2026-07-05T14:02:01+00:00", "speed": 300, "n_gear": 8,
                         "throttle": 100, "brake": 0, "rpm": 11000}])
    monkeypatch.setattr(telemetry, "_openf1", lambda: fake)
    first = telemetry.telemetry_window(11326, 16, 1, T0)
    telemetry._cache.clear()   # the process restarts; the disk does not
    assert telemetry.telemetry_window(11326, 16, 1, T0) == first
    assert len(fake.calls) == 1


def test_a_window_still_filling_in_is_not_kept(monkeypatch):
    now = datetime.now(timezone.utc)
    fake = _FakeOpenF1([])
    monkeypatch.setattr(telemetry, "_openf1", lambda: fake)
    # The window ending now: a live session, still being written.
    telemetry.telemetry_window(9001, 1, 0, now - telemetry.timedelta(seconds=telemetry.WINDOW_S))
    telemetry.telemetry_window(9001, 1, 0, now - telemetry.timedelta(seconds=telemetry.WINDOW_S))
    assert len(fake.calls) == 2


def test_a_parked_car_has_no_pedal_reading_and_a_moving_glitch_is_held(monkeypatch):
    fake = _FakeOpenF1([
        {"date": "2026-07-05T14:02:00.000000+00:00", "speed": 0, "n_gear": 0,
         "throttle": 104, "brake": 104, "rpm": 0},
        {"date": "2026-07-05T14:02:01.000000+00:00", "speed": 300, "n_gear": 8,
         "throttle": 99, "brake": 0, "rpm": 12000},
        {"date": "2026-07-05T14:02:01.250000+00:00", "speed": 302, "n_gear": 8,
         "throttle": 104, "brake": 104, "rpm": 12100},
    ])
    monkeypatch.setattr(telemetry, "_openf1", lambda: fake)

    rows = telemetry.telemetry_window(11326, 16, 1, T0)

    assert [r[3:5] for r in rows] == [[None, None], [99.0, 0.0], [99.0, 0.0]]
