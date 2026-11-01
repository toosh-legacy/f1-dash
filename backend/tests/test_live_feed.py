"""The live map feed and the director that starts it, against a fake OpenF1."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.data import openf1_client
from app.db import models as m
from app.live import director as director_module
from app.live import feed as feed_module
from app.live.feed import PositionFeed

LOOP_POINTS = 200
RADIUS = 2000.0


def _circle(index: float) -> tuple[float, float]:
    angle = 2 * math.pi * (index / LOOP_POINTS)
    return RADIUS * math.cos(angle), RADIUS * math.sin(angle)


TRACK = {
    "path": [list(_circle(i)) for i in range(LOOP_POINTS)],
    "pit_path": [],
    "start_finish": list(_circle(0)),
    "bounds": {"min_x": -RADIUS, "max_x": RADIUS, "min_y": -RADIUS, "max_y": RADIUS},
}


class FakeOpenF1:
    """Two cars lapping a circle, one path point a second, reported at 4 Hz."""

    def __init__(self, start: datetime) -> None:
        self.start = start
        self.now = start + timedelta(seconds=120)
        self.location_calls: list[str] = []

    def sessions(self, **_filters):
        return [{
            "session_key": 9001, "meeting_key": 1, "session_name": "Race", "session_type": "Race",
            "circuit_short_name": "Testring", "location": "Testville", "country_name": "Testland",
            "year": 2026, "date_start": self.start.isoformat(),
            "date_end": (self.start + timedelta(hours=2)).isoformat(),
        }]

    def drivers(self, _key):
        return [
            {"driver_number": 1, "name_acronym": "ONE", "full_name": "Car One", "team_colour": "ff0000"},
            {"driver_number": 2, "name_acronym": "TWO", "full_name": "Car Two", "team_colour": "0000ff"},
        ]

    def laps(self, _key):
        return [
            {"driver_number": 1, "lap_number": 1, "date_start": (self.start + timedelta(seconds=5)).isoformat(),
             "lap_duration": 90.0, "is_pit_out_lap": False},
            {"driver_number": 1, "lap_number": 2, "date_start": (self.start + timedelta(seconds=95)).isoformat(),
             "lap_duration": None, "is_pit_out_lap": False},
        ]

    def race_control(self, _key):
        return [{"date": (self.start + timedelta(seconds=60)).isoformat(), "message": "SAFETY CAR DEPLOYED",
                 "category": "SafetyCar", "lap_number": 1}]

    def location_since(self, _key, since_iso):
        self.location_calls.append(since_iso)
        since = datetime.fromisoformat(since_iso)
        rows = []
        moment = since + timedelta(seconds=0.25)
        while moment <= self.now:
            elapsed = (moment - self.start).total_seconds()
            for number, offset in ((1, 0.0), (2, 20.0)):
                x, y = _circle(elapsed + offset)
                rows.append({"driver_number": number, "date": moment.isoformat(), "x": x, "y": y})
            moment += timedelta(seconds=0.25)
        return rows

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    start = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
    client = FakeOpenF1(start)
    monkeypatch.setattr(feed_module, "layout_for", lambda circuit: (TRACK, 1234))
    monkeypatch.setattr(feed_module, "datetime", _FrozenDatetime.at(client.now))
    return client


class _FrozenDatetime(datetime):
    frozen: datetime

    @classmethod
    def at(cls, moment: datetime):
        cls.frozen = moment
        return cls

    @classmethod
    def now(cls, tz=None):
        return cls.frozen


def test_feed_draws_cars_on_the_circuit_one_frame_a_second(fake):
    feed = PositionFeed(1, 9001, client=fake)
    feed.poll_once()
    state = feed.snapshot()

    assert [d["code"] for d in state["drivers"]] == ["ONE", "TWO"]
    assert state["track"] is TRACK and state["track_source"] == 1234
    frames = state["frames"]
    assert frames, "the first poll should already put cars on the map"
    times = [frame["t"] for frame in frames]
    assert times == sorted(times) and all(b - a == pytest.approx(1.0) for a, b in zip(times, times[1:]))
    # Car one goes round one path point a second: its progress is its time.
    last = frames[-1]
    assert last["cars"]["1"] == pytest.approx(last["t"] % LOOP_POINTS, abs=1.0)
    assert last["cars"]["2"] == pytest.approx((last["t"] + 20) % LOOP_POINTS, abs=1.0)
    # Timing came with it: the lap the leader is on, the flag, the lap events.
    assert last["lap"] == 2
    assert last["flag"] == "safety_car"
    assert state["lap_events"] == [[95.0, 1, 90.0]]


def test_feed_picks_up_where_it_left_off(fake):
    feed = PositionFeed(1, 9001, client=fake)
    feed.poll_once()
    first = feed.snapshot()["frames"][-1]["t"]

    fake.now += timedelta(seconds=8)
    feed._poll_positions()
    frames = feed.snapshot()["frames"]
    assert frames[-1]["t"] == pytest.approx(first + 8)
    times = [frame["t"] for frame in frames]
    assert len(times) == len(set(times)), "no second is framed twice"
    assert fake.location_calls[-1] > fake.location_calls[0], "the second poll asks only for what is new"


def test_live_window_and_session_names():
    start = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
    row = {"date_start": start.isoformat(), "date_end": (start + timedelta(hours=2)).isoformat()}
    assert director_module.live_window(row, start + timedelta(minutes=30))
    assert director_module.live_window(row, start - timedelta(minutes=5))
    assert not director_module.live_window(row, start - timedelta(hours=1))
    assert not director_module.live_window(row, start + timedelta(hours=3))
    assert director_module.session_type_of("Practice 2") is m.SessionType.FP2
    assert director_module.session_type_of("Sprint Qualifying") is m.SessionType.SQ
    assert director_module.session_type_of("Race") is m.SessionType.RACE


def test_a_live_session_off_the_calendar_is_added_to_it(db):
    row = FakeOpenF1(datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)).sessions()[0]
    session = director_module.ensure_session(db, row)
    assert session.openf1_session_key == 9001
    assert session.session_type == "Race"
    assert session.circuit_id == "testville"
    # And it is found again, not duplicated.
    assert director_module.ensure_session(db, row).id == session.id


def test_a_locked_openf1_is_reported_not_failed(monkeypatch):
    class Locked:
        def latest_session(self):
            raise openf1_client.OpenF1Locked("Live F1 session in progress")

    watcher = director_module.Director()
    monkeypatch.setattr(watcher, "client", lambda: Locked())
    watcher.check()
    status = watcher.status()
    assert status["state"] == "locked"
    assert "account" in status["message"]
