"""FastF1 access -- historical and completed-session data.

Everything that reads a *finished* session goes through here. Live state comes
from :mod:`app.data.openf1_client` instead; the two never swap roles.

FastF1 is a heavy, blocking, network- and disk-backed library. Nothing in this
module may be called from the API event loop -- call it from the background
threads in ``app/training`` and ``app/live`` only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)


class FastF1Unavailable(RuntimeError):
    """FastF1 is not installed, or the requested session has no data yet."""


@lru_cache(maxsize=1)
def _fastf1():
    try:
        import fastf1  # noqa: PLC0415 - optional heavy import, deliberately lazy
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise FastF1Unavailable(
            "fastf1 is not installed; run `pip install -r requirements.txt`"
        ) from exc
    fastf1.Cache.enable_cache(str(settings.FASTF1_CACHE_DIR))
    return fastf1


@dataclass
class SessionResult:
    """One driver's outcome in a completed session."""

    driver_id: str
    driver_number: int | None
    driver_name: str
    team_name: str | None
    position: float | None
    grid_position: float | None
    best_lap_s: float | None
    q1_s: float | None = None
    q2_s: float | None = None
    q3_s: float | None = None
    status: str | None = None
    points: float | None = None

    @property
    def is_dnf(self) -> bool:
        # An unknown status is not evidence of a retirement: treat it as a
        # finish, or a season whose classification data is missing would label
        # the entire field as DNF.
        if not self.status or self.status.lower() in {"nan", "none"}:
            return False
        s = self.status.lower()
        return not (s.startswith("finished") or s.startswith("+"))


@dataclass
class Stint:
    driver_id: str
    stint_number: int
    compound: str | None
    lap_start: int | None
    lap_end: int | None
    tyre_life_start: float | None
    degradation_s_per_lap: float | None = None


@dataclass
class LoadedSession:
    year: int
    event_name: str
    circuit_key: str
    session_type: str
    start_time: datetime | None
    is_sprint_weekend: bool
    results: list[SessionResult] = field(default_factory=list)
    stints: list[Stint] = field(default_factory=list)
    weather: dict[str, Any] = field(default_factory=dict)
    race_control: list[dict[str, Any]] = field(default_factory=list)
    laps: Any = None  # the raw FastF1 Laps frame, for derived features


def _f(value: Any) -> float | None:
    """Coerce a possibly-NaN / possibly-Timedelta FastF1 value to float seconds."""
    if value is None:
        return None
    try:
        if hasattr(value, "total_seconds"):
            seconds = value.total_seconds()
            return None if seconds != seconds else float(seconds)  # NaT -> NaN -> None
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN check without importing numpy


def circuit_id_from_event(event_name: str, location: str | None = None) -> str:
    """Stable, human-readable circuit id used as the primary key."""
    source = (location or event_name or "").strip().lower()
    return "".join(ch if ch.isalnum() else "_" for ch in source).strip("_") or "unknown"


class FastF1Client:
    """Thin, typed wrapper over the parts of FastF1 this project uses."""

    def __init__(self, season: int | None = None) -> None:
        self.season = season or settings.CURRENT_SEASON

    # -- schedule ---------------------------------------------------------
    def event_schedule(self, year: int | None = None) -> list[dict[str, Any]]:
        ff1 = _fastf1()
        year = year or self.season
        schedule = ff1.get_event_schedule(year, include_testing=False)
        events: list[dict[str, Any]] = []
        for _, row in schedule.iterrows():
            fmt = str(row.get("EventFormat", "conventional"))
            events.append(
                {
                    "round": int(row["RoundNumber"]),
                    "event_name": str(row["EventName"]),
                    "location": str(row.get("Location", "")),
                    "country": str(row.get("Country", "")),
                    "circuit_id": circuit_id_from_event(str(row["EventName"]), row.get("Location")),
                    "is_sprint_weekend": "sprint" in fmt.lower(),
                    "session_dates": {
                        str(row.get(f"Session{i}", "")): row.get(f"Session{i}Date")
                        for i in range(1, 6)
                        if row.get(f"Session{i}")
                    },
                }
            )
        return events

    # -- one session ------------------------------------------------------
    def load_session(
        self,
        year: int,
        event: str | int,
        session_type: str,
        *,
        telemetry: bool = False,
    ) -> LoadedSession:
        """Load a completed session. Blocking -- background threads only."""
        ff1 = _fastf1()
        try:
            ses = ff1.get_session(year, event, session_type)
            ses.load(telemetry=telemetry, weather=True, messages=True)
        except Exception as exc:  # fastf1 raises a wide range of errors
            raise FastF1Unavailable(f"cannot load {year} {event} {session_type}: {exc}") from exc

        loaded = LoadedSession(
            year=year,
            event_name=str(getattr(ses.event, "EventName", event)),
            circuit_key=circuit_id_from_event(
                str(getattr(ses.event, "EventName", event)), getattr(ses.event, "Location", None)
            ),
            session_type=session_type,
            start_time=getattr(ses, "date", None),
            is_sprint_weekend="sprint" in str(getattr(ses.event, "EventFormat", "")).lower(),
            laps=getattr(ses, "laps", None),
        )
        loaded.results = self._extract_results(ses)
        # Classification data (position, grid, status) reaches FastF1 through
        # Ergast, which does not cover the current season. When it is missing,
        # the finishing order is reconstructed from timing data instead --
        # without it every driver looks like a DNF and the race labels collapse
        # to a single value.
        self._augment_results_from_laps(ses, loaded.results)
        loaded.stints = self._extract_stints(ses)
        loaded.weather = self._extract_weather(ses)
        loaded.race_control = self._extract_race_control(ses)
        return loaded

    # -- extraction helpers ------------------------------------------------
    @staticmethod
    def _extract_results(ses: Any) -> list[SessionResult]:
        results: list[SessionResult] = []
        frame = getattr(ses, "results", None)
        if frame is None or len(frame) == 0:
            return results
        for _, row in frame.iterrows():
            abbrev = str(row.get("Abbreviation") or row.get("DriverNumber") or "").strip()
            if not abbrev:
                continue
            number = row.get("DriverNumber")
            results.append(
                SessionResult(
                    driver_id=abbrev,
                    driver_number=int(number) if str(number).isdigit() else None,
                    driver_name=str(row.get("FullName") or abbrev),
                    team_name=str(row.get("TeamName")) if row.get("TeamName") is not None else None,
                    position=_f(row.get("Position")),
                    grid_position=_f(row.get("GridPosition")),
                    best_lap_s=_f(row.get("Q3")) or _f(row.get("Q2")) or _f(row.get("Q1")),
                    q1_s=_f(row.get("Q1")),
                    q2_s=_f(row.get("Q2")),
                    q3_s=_f(row.get("Q3")),
                    status=str(row.get("Status")) if row.get("Status") is not None else None,
                    points=_f(row.get("Points")),
                )
            )
        return results

    @staticmethod
    def _augment_results_from_laps(ses: Any, results: list[SessionResult]) -> None:
        """Fill in position/grid/status from lap timing when classification is absent."""
        if not results or all(r.position is not None for r in results):
            return
        laps = getattr(ses, "laps", None)
        if laps is None or len(laps) == 0 or "Driver" not in laps.columns:
            return

        by_driver: dict[str, dict[str, Any]] = {}
        for driver, group in laps.groupby("Driver"):
            group = group.sort_values("LapNumber")
            last = group.iloc[-1]
            first = group.iloc[0]
            by_driver[str(driver)] = {
                "laps_completed": int(last["LapNumber"]) if "LapNumber" in group.columns else 0,
                "final_position": _f(last.get("Position")) if "Position" in group.columns else None,
                "first_lap_position": _f(first.get("Position")) if "Position" in group.columns else None,
                "elapsed_s": _f(last.get("Time")) if "Time" in group.columns else None,
            }
        if not by_driver:
            return

        leader_laps = max(entry["laps_completed"] for entry in by_driver.values())
        # Order by distance covered, then by elapsed time at the final lap.
        ranking = sorted(
            by_driver.items(),
            key=lambda kv: (
                -kv[1]["laps_completed"],
                kv[1]["elapsed_s"] if kv[1]["elapsed_s"] is not None else float("inf"),
            ),
        )
        derived_order = {driver: index + 1 for index, (driver, _) in enumerate(ranking)}

        for result in results:
            entry = by_driver.get(result.driver_id)
            if entry is None:
                continue
            if result.position is None:
                result.position = entry["final_position"] or float(derived_order[result.driver_id])
            if result.grid_position is None:
                # Position at the end of lap 1 is the closest available proxy for
                # the starting grid slot when the grid itself is not published.
                result.grid_position = entry["first_lap_position"]
            if not result.status:
                # More than one lap short of the leader is a retirement, not a
                # lapped finisher -- a heuristic, and flagged as such.
                behind = leader_laps - entry["laps_completed"]
                result.status = "Finished" if behind <= 1 else "Retired (derived)"

    @staticmethod
    def _extract_stints(ses: Any) -> list[Stint]:
        stints: list[Stint] = []
        laps = getattr(ses, "laps", None)
        if laps is None or len(laps) == 0:
            return stints
        needed = {"Driver", "Stint", "Compound", "LapNumber", "TyreLife", "LapTime"}
        if not needed.issubset(set(laps.columns)):
            return stints
        for (driver, stint_no), group in laps.groupby(["Driver", "Stint"], dropna=True):
            group = group.sort_values("LapNumber")
            lap_times = [_f(v) for v in group["LapTime"]]
            ages = [_f(v) for v in group["TyreLife"]]
            pairs = [(a, t) for a, t in zip(ages, lap_times) if a is not None and t is not None]
            deg = _linear_slope(pairs) if len(pairs) >= 4 else None
            stints.append(
                Stint(
                    driver_id=str(driver),
                    stint_number=int(stint_no),
                    compound=str(group["Compound"].iloc[0]) if len(group) else None,
                    lap_start=int(group["LapNumber"].iloc[0]),
                    lap_end=int(group["LapNumber"].iloc[-1]),
                    tyre_life_start=_f(group["TyreLife"].iloc[0]),
                    degradation_s_per_lap=deg,
                )
            )
        return stints

    @staticmethod
    def _extract_weather(ses: Any) -> dict[str, Any]:
        data = getattr(ses, "weather_data", None)
        if data is None or len(data) == 0:
            return {}
        mean_of = lambda col: _f(data[col].mean()) if col in data.columns else None  # noqa: E731
        rainfall = bool(data["Rainfall"].any()) if "Rainfall" in data.columns else False
        return {
            "air_temp": mean_of("AirTemp"),
            "track_temp": mean_of("TrackTemp"),
            "humidity": mean_of("Humidity"),
            "wind_speed": mean_of("WindSpeed"),
            "wind_direction": mean_of("WindDirection"),
            "rainfall": rainfall,
        }

    @staticmethod
    def _extract_race_control(ses: Any) -> list[dict[str, Any]]:
        messages = getattr(ses, "race_control_messages", None)
        if messages is None or len(messages) == 0:
            return []
        events: list[dict[str, Any]] = []
        for _, row in messages.iterrows():
            events.append(
                {
                    "message": str(row.get("Message", "")),
                    "category": str(row.get("Category", "")),
                    "flag": str(row.get("Flag", "")),
                    "lap_number": int(row["Lap"]) if str(row.get("Lap", "")).isdigit() else None,
                    "time": row.get("Time"),
                }
            )
        return events


def _linear_slope(pairs: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of y over x; the tyre-degradation rate in s/lap."""
    n = len(pairs)
    if n < 2:
        return None
    sx = sum(x for x, _ in pairs)
    sy = sum(y for _, y in pairs)
    sxx = sum(x * x for x, _ in pairs)
    sxy = sum(x * y for x, y in pairs)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    return (n * sxy - sx * sy) / denom
