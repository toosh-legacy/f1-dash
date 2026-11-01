"""Timing for sessions that are not races: when each part ran, and every lap.

A race is read lap by lap, because the order at the end of each lap is the
race. Practice and qualifying are not: they are read by the clock. What
matters is each car's best lap *so far*, and in qualifying, so far *in this
part* -- Q1, Q2 and Q3 each start from nothing, with the slowest cars going
home at the end of the first two.

So a bundle for one of these sessions carries two extra things, both on the
replay's own clock:

* ``segments`` -- each green-light-to-chequered-flag run of the session, read
  from race control, named Q1/Q2/Q3 (SQ1/SQ2/SQ3 for a sprint shootout) or
  just the session's own name for practice.
* ``lap_events`` -- ``[t_completed, car, lap_time_s]`` for every timed lap
  that was not an out-lap, sorted by when it was completed.
"""
from __future__ import annotations

from typing import Any

from app.data.replay.shared import _parse_date

#: Race control's words for a part of a session starting and ending.
_START = "SESSION STARTED"
_END = ("SESSION FINISHED", "CHEQUERED FLAG")


def segments(
    control: list[dict[str, Any]], t0: float, session_name: str
) -> list[dict[str, Any]]:
    """Each run of the session, from its start to its chequered flag."""
    marks: list[tuple[float, str]] = []
    for message in control:
        text = str(message.get("message") or "").upper()
        moment = _parse_date(message.get("date"))
        if moment is None:
            continue
        if _START in text:
            marks.append((moment.timestamp() - t0, "start"))
        elif any(word in text for word in _END):
            marks.append((moment.timestamp() - t0, "end"))
    marks.sort()

    runs: list[list[float | None]] = []
    for moment, kind in marks:
        if kind == "start":
            runs.append([moment, None])
        elif runs and runs[-1][1] is None:
            runs[-1][1] = moment

    name = session_name.lower()
    if "qualifying" in name or "shootout" in name:
        prefix = "SQ" if ("sprint" in name or "shootout" in name) else "Q"
        names = [f"{prefix}{index}" for index in range(1, len(runs) + 1)]
    else:
        names = [session_name] * len(runs)
    return [
        {"name": label, "start": round(start, 1), "end": round(end, 1) if end is not None else None}
        for label, (start, end) in zip(names, runs)
    ]


def lap_events(laps: list[dict[str, Any]], t0: float) -> list[list[float]]:
    """Every timed flying lap, as ``[t_completed, car, lap_time_s]``."""
    out: list[list[float]] = []
    for lap in laps:
        started = _parse_date(lap.get("date_start"))
        duration = lap.get("lap_duration")
        if started is None or duration is None or lap.get("is_pit_out_lap"):
            continue
        try:
            seconds = float(duration)
            number = int(lap["driver_number"])
        except (TypeError, ValueError, KeyError):
            continue
        out.append([round(started.timestamp() - t0 + seconds, 2), number, round(seconds, 3)])
    out.sort(key=lambda event: event[0])
    return out


def session_end(control: list[dict[str, Any]]) -> float | None:
    """The last chequered flag, as an epoch -- when the session really ended.

    Qualifying overruns its scheduled hour whenever a part is delayed, so the
    schedule cannot be trusted to say where the replay should stop.
    """
    last: float | None = None
    for message in control:
        text = str(message.get("message") or "").upper()
        if any(word in text for word in _END):
            moment = _parse_date(message.get("date"))
            if moment is not None:
                last = max(last or 0.0, moment.timestamp())
    return last
