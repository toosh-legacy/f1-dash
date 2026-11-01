"""Practice and qualifying timing: the parts of a session, and every lap."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.data.replay import timing
from app.data.replay.build import _session_window

T0 = datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc)


def _rc(minutes, message):
    return {"date": (T0 + timedelta(minutes=minutes)).isoformat(), "message": message}


QUALI_CONTROL = [
    _rc(0, "GREEN LIGHT - PIT EXIT OPEN"), _rc(0, "SESSION STARTED"),
    _rc(18, "CHEQUERED FLAG"), _rc(18, "SESSION FINISHED"),
    _rc(23, "FIA STEWARDS: Q1 INCIDENT INVOLVING CAR 6 NOTED"),
    _rc(33, "SESSION STARTED"), _rc(48, "CHEQUERED FLAG"), _rc(48, "SESSION FINISHED"),
    _rc(55, "SESSION STARTED"), _rc(68, "CHEQUERED FLAG"), _rc(68, "SESSION FINISHED"),
]


def test_qualifying_is_cut_into_q1_q2_q3():
    parts = timing.segments(QUALI_CONTROL, T0.timestamp(), "Qualifying")
    assert [p["name"] for p in parts] == ["Q1", "Q2", "Q3"]
    assert parts[0] == {"name": "Q1", "start": 0.0, "end": 1080.0}
    assert parts[2]["start"] == 55 * 60 and parts[2]["end"] == 68 * 60


def test_a_sprint_shootout_is_sq1_to_sq3_and_practice_keeps_its_name():
    assert [p["name"] for p in timing.segments(QUALI_CONTROL, T0.timestamp(), "Sprint Qualifying")] \
        == ["SQ1", "SQ2", "SQ3"]
    practice = [_rc(0, "SESSION STARTED"), _rc(60, "CHEQUERED FLAG")]
    assert timing.segments(practice, T0.timestamp(), "Practice 1") == [
        {"name": "Practice 1", "start": 0.0, "end": 3600.0}
    ]


def test_lap_events_are_timed_flying_laps_by_completion():
    laps = [
        {"driver_number": 16, "date_start": (T0 + timedelta(seconds=200)).isoformat(),
         "lap_duration": 88.5, "is_pit_out_lap": False},
        {"driver_number": 44, "date_start": (T0 + timedelta(seconds=100)).isoformat(),
         "lap_duration": 120.0, "is_pit_out_lap": True},          # out-lap: not a time
        {"driver_number": 44, "date_start": (T0 + timedelta(seconds=150)).isoformat(),
         "lap_duration": 89.0, "is_pit_out_lap": False},
        {"driver_number": 1, "date_start": None, "lap_duration": 90.0},
        {"driver_number": 1, "date_start": T0.isoformat(), "lap_duration": None},
    ]
    assert timing.lap_events(laps, T0.timestamp()) == [[239.0, 44, 89.0], [288.5, 16, 88.5]]


def test_an_overrunning_session_ends_at_its_last_chequered_flag():
    # Scheduled for an hour; Q3 ended at 68 minutes. The cars kept reporting
    # well after.
    meta = {"date_start": T0.isoformat(), "date_end": (T0 + timedelta(hours=1)).isoformat()}
    traces = {1: [(T0.timestamp() - 600, 0, 0), (T0.timestamp() + 3 * 3600, 0, 0)]}
    finished = timing.session_end(QUALI_CONTROL)

    first, last = _session_window(traces, meta, finished)
    assert first == T0.timestamp()
    assert last == finished + 120

    # Without race control it is the schedule, as before.
    assert _session_window(traces, meta)[1] == (T0 + timedelta(hours=1)).timestamp()


def test_the_meta_half_of_a_bundle_carries_the_timing(monkeypatch):
    # The dashboard draws the standings from /meta alone, so whatever the
    # standings read has to be in it, not left behind with the frames.
    import gzip
    import json

    from app.data.replay import store

    bundle = {
        "version": 6, "session": {"type": "Qualifying"}, "t0": 1.0,
        "segments": [{"name": "Q1", "start": 0, "end": 10}],
        "lap_events": [[5.0, 1, 88.0]], "frames": [{"t": 0, "cars": {}}],
    }
    monkeypatch.setattr(store, "load_bundle", lambda key: bundle)
    store._PART_CACHE.clear()
    try:
        meta = json.loads(gzip.decompress(store.bundle_part(999_999, "meta")))
    finally:
        store._PART_CACHE.clear()
    assert meta["segments"] == bundle["segments"]
    assert meta["lap_events"] == bundle["lap_events"]
    assert meta["t0"] == 1.0
    assert "frames" not in meta
