"""Seeding reference data (M1): circuits, teams, drivers, and the session calendar.

Sources, in order of preference:

1. **FastF1** for the real season calendar and per-session driver/team entries.
2. **OpenF1** as a fallback for driver/team entries when FastF1 has no data for a
   session yet (early in a season, FastF1 lags).
3. A small static table for circuit geometry that neither API exposes -- altitude
   and circuit classification. These transfer across the regulation reset
   (physical geometry does not change with car rules), so a static table is the
   correct home for them.

Pit-loss and safety-car rates start from the static priors below and are meant to
be revalidated against 2026 data as it accumulates (guide section 2).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.data.fastf1_client import FastF1Client, FastF1Unavailable
from app.data.openf1_client import OpenF1Client, OpenF1Error
from app.db import models as m

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CircuitSeed:
    name_contains: str
    type: str
    altitude_m: float
    avg_pit_loss_s: float
    sc_rate: float
    overtaking_difficulty: float  # 0 = easy to pass, 1 = near impossible


#: Static circuit geometry. Matched by substring against the event location so a
#: renamed grand prix does not orphan its circuit row.
CIRCUIT_REFERENCE: tuple[CircuitSeed, ...] = (
    CircuitSeed("melbourne", "hybrid", 10, 19.0, 0.55, 0.60),
    CircuitSeed("shanghai", "permanent", 5, 21.0, 0.30, 0.35),
    CircuitSeed("suzuka", "permanent", 45, 22.0, 0.40, 0.65),
    CircuitSeed("sakhir", "permanent", 7, 22.5, 0.35, 0.30),
    CircuitSeed("jeddah", "street", 12, 20.0, 0.75, 0.45),
    CircuitSeed("miami", "street", 2, 19.5, 0.45, 0.40),
    CircuitSeed("imola", "permanent", 37, 26.0, 0.40, 0.80),
    CircuitSeed("monaco", "street", 7, 19.0, 0.65, 0.95),
    CircuitSeed("barcelona", "permanent", 130, 21.0, 0.25, 0.70),
    CircuitSeed("montreal", "hybrid", 13, 17.5, 0.70, 0.35),
    CircuitSeed("spielberg", "permanent", 678, 19.0, 0.35, 0.25),
    CircuitSeed("silverstone", "permanent", 153, 20.5, 0.40, 0.35),
    CircuitSeed("spa", "permanent", 401, 19.5, 0.35, 0.25),
    CircuitSeed("hungaroring", "permanent", 249, 20.0, 0.35, 0.85),
    CircuitSeed("zandvoort", "permanent", 5, 21.0, 0.45, 0.80),
    CircuitSeed("monza", "permanent", 162, 22.0, 0.40, 0.20),
    CircuitSeed("baku", "street", -22, 19.0, 0.80, 0.30),
    CircuitSeed("singapore", "street", 5, 26.0, 0.80, 0.85),
    CircuitSeed("austin", "permanent", 168, 21.0, 0.35, 0.30),
    CircuitSeed("mexico", "permanent", 2238, 22.0, 0.45, 0.45),
    CircuitSeed("interlagos", "permanent", 785, 20.0, 0.50, 0.30),
    CircuitSeed("las vegas", "street", 620, 20.5, 0.60, 0.30),
    CircuitSeed("lusail", "permanent", 15, 23.0, 0.35, 0.45),
    CircuitSeed("yas", "permanent", 5, 21.5, 0.35, 0.55),
    CircuitSeed("madrid", "hybrid", 600, 21.0, 0.50, 0.55),
)

DEFAULT_CIRCUIT = CircuitSeed("", "permanent", 100, 22.0, 0.40, 0.50)


def circuit_reference_for(*names: str) -> CircuitSeed:
    haystack = " ".join(n.lower() for n in names if n)
    for seed in CIRCUIT_REFERENCE:
        if seed.name_contains in haystack:
            return seed
    return DEFAULT_CIRCUIT


def seed_calendar(db: DBSession, year: int | None = None, limit_events: int | None = None) -> dict[str, int]:
    """Seed circuits and the session calendar for a season from FastF1."""
    year = year or settings.CURRENT_SEASON
    client = FastF1Client(year)
    try:
        events = client.event_schedule(year)
    except FastF1Unavailable as exc:
        raise RuntimeError(f"cannot seed calendar: {exc}") from exc

    if limit_events:
        events = events[:limit_events]

    circuits = sessions = 0
    for event in events:
        reference = circuit_reference_for(event["location"], event["event_name"], event["country"])
        circuit = db.get(m.Circuit, event["circuit_id"])
        if circuit is None:
            circuit = m.Circuit(id=event["circuit_id"], name=event["event_name"], type=reference.type)
            db.add(circuit)
            circuits += 1
        circuit.altitude_m = reference.altitude_m
        circuit.avg_pit_loss_s = reference.avg_pit_loss_s
        circuit.historical_sc_rate = reference.sc_rate
        circuit.historical_overtaking_difficulty = reference.overtaking_difficulty

        wanted = ["FP1", "FP2", "FP3", "Qualifying", "Race"]
        if event["is_sprint_weekend"]:
            wanted = ["FP1", "SQ", "Sprint", "Qualifying", "Race"]
        for session_type in wanted:
            if _upsert_session(db, year, event, session_type):
                sessions += 1

    db.flush()
    log.info("seeded %s circuits and %s sessions for %s", circuits, sessions, year)
    return {"circuits": circuits, "sessions": sessions, "events": len(events)}


def _upsert_session(db: DBSession, year: int, event: dict[str, Any], session_type: str) -> bool:
    existing = db.scalar(
        select(m.Session).where(
            m.Session.year == year,
            m.Session.circuit_id == event["circuit_id"],
            m.Session.session_type == session_type,
        )
    )
    start = _session_start(event, session_type)
    if existing is not None:
        existing.start_time = existing.start_time or start
        return False
    db.add(
        m.Session(
            year=year,
            circuit_id=event["circuit_id"],
            session_type=session_type,
            # Field, not a hardcoded assumption (rule 6).
            regs_regime=settings.CURRENT_REGS_REGIME if year >= 2026 else str(year),
            is_sprint_weekend=event["is_sprint_weekend"],
            start_time=start,
            status=m.SessionStatus.SCHEDULED.value,
        )
    )
    return True


def _session_start(event: dict[str, Any], session_type: str):
    for name, date in (event.get("session_dates") or {}).items():
        if name.replace(" ", "").lower().startswith(session_type.replace(" ", "").lower()):
            return _to_datetime(date)
    return None


def _to_datetime(value: Any):
    if value is None:
        return None
    to_pydatetime = getattr(value, "to_pydatetime", None)
    try:
        return to_pydatetime() if to_pydatetime else value
    except Exception:  # pragma: no cover - pandas NaT and friends
        return None


def seed_entries_from_fastf1(db: DBSession, year: int | None = None) -> dict[str, int]:
    """Seed teams and drivers from a real session's entry list."""
    year = year or settings.CURRENT_SEASON
    client = FastF1Client(year)
    events = client.event_schedule(year)
    for event in events:
        for session_type in ("Race", "Qualifying", "FP1"):
            try:
                loaded = client.load_session(year, event["event_name"], session_type)
            except FastF1Unavailable:
                continue
            if loaded.results:
                return _store_entries(
                    db,
                    [
                        {
                            "driver_id": r.driver_id,
                            "name": r.driver_name,
                            "number": r.driver_number,
                            "team": r.team_name,
                        }
                        for r in loaded.results
                    ],
                )
    raise RuntimeError(f"no {year} session with an entry list is available from FastF1 yet")


def seed_entries_from_openf1(db: DBSession, session_key: int | None = None) -> dict[str, int]:
    """Fallback entry list from OpenF1, for when FastF1 has no data yet."""
    with OpenF1Client() as client:
        try:
            if session_key is None:
                latest = client.latest_session()
                if latest is None:
                    raise RuntimeError("OpenF1 returned no sessions")
                session_key = int(latest["session_key"])
            rows = client.drivers(session_key)
        except OpenF1Error as exc:
            raise RuntimeError(f"cannot seed entries from OpenF1: {exc}") from exc

    return _store_entries(
        db,
        [
            {
                "driver_id": row.get("name_acronym") or str(row.get("driver_number")),
                "name": row.get("full_name") or row.get("broadcast_name") or "unknown",
                "number": row.get("driver_number"),
                "team": row.get("team_name"),
            }
            for row in rows
        ],
    )


def _store_entries(db: DBSession, entries: list[dict[str, Any]]) -> dict[str, int]:
    teams = drivers = 0
    for entry in entries:
        driver_id = str(entry["driver_id"] or "").strip()
        if not driver_id:
            continue
        team_name = entry.get("team") or "unknown"
        team_id = _slug(team_name)
        team = db.get(m.Team, team_id)
        if team is None:
            team = m.Team(id=team_id, name=team_name, power_unit=_power_unit_for(team_name))
            db.add(team)
            # Flush immediately: teammates share a team, and db.get() would not
            # see a pending row (autoflush is off), producing a duplicate insert.
            db.flush()
            teams += 1

        driver = db.get(m.Driver, driver_id)
        if driver is None:
            driver = m.Driver(id=driver_id, name=entry["name"])
            db.add(driver)
            db.flush()
            drivers += 1
        driver.name = entry["name"] or driver.name
        driver.driver_number = int(entry["number"]) if entry.get("number") else driver.driver_number
        driver.team_id = team_id
    db.flush()
    log.info("seeded %s teams and %s drivers", teams, drivers)
    return {"teams": teams, "drivers": drivers}


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value.lower()).strip("_") or "unknown"


def _power_unit_for(team_name: str) -> str | None:
    """Best-effort power-unit attribution from the team name.

    All 2026 units are new designs (no MGU-H, ~350kW MGU-K); this records the
    supplier only, which is why the transfer table treats it as weak-transfer.
    """
    lowered = team_name.lower()
    for needle, unit in (
        ("mercedes", "Mercedes"),
        ("ferrari", "Ferrari"),
        ("red bull", "Red Bull Ford Powertrains"),
        ("racing bulls", "Red Bull Ford Powertrains"),
        ("aston", "Honda"),
        ("sauber", "Audi"),
        ("audi", "Audi"),
        ("alpine", "Renault"),
        ("cadillac", "Cadillac"),
        ("williams", "Mercedes"),
        ("mclaren", "Mercedes"),
        ("haas", "Ferrari"),
    ):
        if needle in lowered:
            return unit
    return None


def seed_all(db: DBSession, year: int | None = None, limit_events: int | None = None) -> dict[str, Any]:
    """Full M1 seed: calendar first, then entries with an OpenF1 fallback."""
    year = year or settings.CURRENT_SEASON
    result: dict[str, Any] = {"year": year}
    result["calendar"] = seed_calendar(db, year, limit_events)
    try:
        result["entries"] = seed_entries_from_fastf1(db, year)
        result["entries_source"] = "fastf1"
    except (RuntimeError, FastF1Unavailable) as exc:
        log.warning("FastF1 entry list unavailable (%s); falling back to OpenF1", exc)
        result["entries"] = seed_entries_from_openf1(db)
        result["entries_source"] = "openf1"
    return result
