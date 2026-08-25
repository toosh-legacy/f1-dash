"""Assembles :class:`FeatureContext` objects from the database and data clients.

This is the impure layer that sits above :mod:`app.features.engineering`: it
does the FastF1/OpenF1/database work, then hands plain values to the pure
feature functions.

The transfer table is applied *at source* here: team/tyre aggregates are built
from current-regime sessions only, while driver and circuit aggregates draw on
the full historical record.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.data.fastf1_client import FastF1Client, FastF1Unavailable, LoadedSession
from app.data.openf1_client import LiveDriverState
from app.db import models as m
from app.features.engineering import (
    CircuitProfile,
    DriverProfile,
    FeatureContext,
    FeatureVector,
    LiveState,
    TeamForm,
    build_feature_vector,
    driver_track_history_score,
    driver_wet_rating,
    percentile_rank,
    team_form_from_sessions,
)

log = logging.getLogger(__name__)

#: Aggregates are expensive (many FastF1 loads) and change only when a session
#: completes, so they are cached in-process for this long.
AGGREGATE_TTL_S = 900.0

# 2026 power units, encoded ordinally for the tree model. All units are new
# designs this year (no MGU-H, ~350kW MGU-K), so this identifies the supplier
# only -- the transfer table marks it WEAK for exactly that reason.
POWER_UNIT_CODES = {
    "Mercedes": 1.0,
    "Ferrari": 2.0,
    "Red Bull Ford Powertrains": 3.0,
    "Honda": 4.0,
    "Audi": 5.0,
    "Renault": 6.0,
    "Alpine": 6.0,
    "Cadillac": 7.0,
}


@dataclass
class _CacheEntry:
    value: Any
    stored_at: float

    def is_fresh(self, ttl: float = AGGREGATE_TTL_S) -> bool:
        return (time.monotonic() - self.stored_at) < ttl


class AggregateCache:
    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        return entry.value if entry and entry.is_fresh() else None

    def put(self, key: str, value: Any) -> None:
        self._store[key] = _CacheEntry(value, time.monotonic())

    def clear(self) -> None:
        self._store.clear()


_cache = AggregateCache()


def invalidate_aggregates() -> None:
    """Call after ingesting a newly completed session."""
    _cache.clear()


class FeatureBuilder:
    """Builds and persists feature vectors for a session."""

    def __init__(
        self,
        db: DBSession,
        *,
        fastf1: FastF1Client | None = None,
        season: int | None = None,
        as_of: datetime | None = None,
    ) -> None:
        self.db = db
        self.season = season or settings.CURRENT_SEASON
        self.fastf1 = fastf1 or FastF1Client(self.season)
        # Aggregates are computed strictly from sessions that started *before*
        # this instant. Without it, a feature vector for a session would be
        # built from aggregates that already contain that session's own results
        # -- target leakage, and with a season this short the aggregates would
        # effectively encode the finishing order being predicted.
        self.as_of = as_of

    # -- aggregates --------------------------------------------------------
    def team_forms(self) -> dict[str, TeamForm]:
        """Per-team form from the current regime's completed sessions only."""
        key = f"team_forms:{self.season}:{self._as_of_key}"
        cached = _cache.get(key)
        if cached is not None:
            return cached

        lap_times: dict[str, list[float]] = {}
        finishes: dict[str, tuple[int, int]] = {}
        power_units: dict[str, float] = {}
        recent: dict[str, list[float]] = {}

        for loaded in self._completed_regime_sessions():
            for result in loaded.results:
                team = result.team_name or "unknown"
                if result.best_lap_s:
                    lap_times.setdefault(team, []).append(result.best_lap_s)
                    recent.setdefault(team, []).append(result.best_lap_s)
                started, finished = finishes.get(team, (0, 0))
                finishes[team] = (started + 1, finished + (0 if result.is_dnf else 1))
                power_units.setdefault(team, POWER_UNIT_CODES.get(team.split()[0], 0.0))

        # Recent form: last 3-5 results vs. the team's own season baseline.
        deltas: dict[str, float] = {}
        for team, times in recent.items():
            if len(times) >= 4:
                window = times[-5:]
                deltas[team] = sum(window) / len(window) - sum(times) / len(times)

        forms = team_form_from_sessions(
            lap_times,
            dnfs_by_team={t: (f, s) for t, (s, f) in finishes.items()},
            recent_deltas_by_team=deltas,
            power_unit_codes=power_units,
        )
        _cache.put(key, forms)
        return forms

    def driver_profiles(self, circuit_id: str | None = None) -> dict[str, DriverProfile]:
        """Driver aggregates over the full historical record (they transfer)."""
        key = f"driver_profiles:{self.season}:{circuit_id}:{self._as_of_key}"
        cached = _cache.get(key)
        if cached is not None:
            return cached

        finishes: dict[str, list[float]] = {}
        at_circuit: dict[str, list[float]] = {}
        wet: dict[str, list[float]] = {}
        dry: dict[str, list[float]] = {}
        quali_times: dict[str, list[float]] = {}
        teams: dict[str, str] = {}

        history_years = range(self.season - 4, self.season + 1)  # human skill transfers
        for year in history_years:
            for loaded in self._completed_sessions(year):
                is_wet = bool(loaded.weather.get("rainfall"))
                for result in loaded.results:
                    if result.position:
                        finishes.setdefault(result.driver_id, []).append(result.position)
                        (wet if is_wet else dry).setdefault(result.driver_id, []).append(result.position)
                        if circuit_id and loaded.circuit_key == circuit_id:
                            at_circuit.setdefault(result.driver_id, []).append(result.position)
                    if result.best_lap_s and year == self.season:
                        quali_times.setdefault(result.driver_id, []).append(result.best_lap_s)
                    if result.team_name:
                        teams[result.driver_id] = result.team_name

        teammate_gaps = self._teammate_gaps(quali_times, teams)
        db_drivers = {d.id: d for d in self.db.scalars(select(m.Driver)).all()}

        profiles: dict[str, DriverProfile] = {}
        all_avg = [sum(v) / len(v) for v in finishes.values() if v]
        for driver_id in set(finishes) | set(db_drivers):
            positions = finishes.get(driver_id, [])
            avg = sum(positions) / len(positions) if positions else None
            row = db_drivers.get(driver_id)
            profiles[driver_id] = DriverProfile(
                baseline=percentile_rank(avg, all_avg) if avg is not None else None,
                track_history_score=driver_track_history_score(at_circuit.get(driver_id, [])),
                wet_rating=driver_wet_rating(wet.get(driver_id, []), dry.get(driver_id, [])),
                is_rookie=bool(row.is_rookie) if row else False,
                is_new_to_team=self._is_new_to_team(row),
                teammate_quali_gap_s=teammate_gaps.get(driver_id),
                teammate_race_gap_s=teammate_gaps.get(driver_id),
            )
        _cache.put(key, profiles)
        return profiles

    @staticmethod
    def _is_new_to_team(driver: m.Driver | None) -> bool:
        if driver is None or driver.joined_team_date is None:
            return False
        return driver.joined_team_date.year >= settings.CURRENT_SEASON

    @staticmethod
    def _teammate_gaps(
        quali_times: dict[str, list[float]], teams: dict[str, str]
    ) -> dict[str, float]:
        """Best-lap gap to the driver's own teammate -- current regime only.

        This compares current cars, so it is a regime-only feature even though
        it describes a driver.
        """
        by_team: dict[str, list[tuple[str, float]]] = {}
        for driver_id, times in quali_times.items():
            if not times:
                continue
            by_team.setdefault(teams.get(driver_id, "unknown"), []).append(
                (driver_id, min(times))
            )
        gaps: dict[str, float] = {}
        for pair in by_team.values():
            if len(pair) != 2:
                continue
            (a_id, a_time), (b_id, b_time) = pair
            gaps[a_id] = a_time - b_time
            gaps[b_id] = b_time - a_time
        return gaps

    def _completed_regime_sessions(self) -> list[LoadedSession]:
        return self._completed_sessions(self.season)

    @property
    def _as_of_key(self) -> str:
        return self.as_of.isoformat() if self.as_of else "all"

    def _session_rows(self, year: int) -> list[m.Session]:
        """Completed sessions for a year that started before the ``as_of`` cutoff."""
        stmt = select(m.Session).where(
            m.Session.year == year, m.Session.status == m.SessionStatus.COMPLETED.value
        )
        if self.as_of is not None:
            stmt = stmt.where(m.Session.start_time < self.as_of)
        return list(self.db.scalars(stmt).all())

    def _completed_sessions(self, year: int) -> list[LoadedSession]:
        """Load completed sessions for a year, up to the ``as_of`` cutoff. Blocking."""
        key = f"sessions:{year}:{self._as_of_key}"
        cached = _cache.get(key)
        if cached is not None:
            return cached

        rows = self._session_rows(year)
        loaded: list[LoadedSession] = []
        for row in rows:
            try:
                loaded.append(self.fastf1.load_session(row.year, row.circuit.name, row.session_type))
            except FastF1Unavailable as exc:
                log.warning("skipping %s %s: %s", row.year, row.session_type, exc)
        _cache.put(key, loaded)
        return loaded

    # -- circuit -----------------------------------------------------------
    def circuit_profile(self, circuit: m.Circuit, tyre_deg_rate: float | None = None) -> CircuitProfile:
        return CircuitProfile(
            circuit_type=circuit.type,
            altitude_m=circuit.altitude_m,
            pit_loss_s=circuit.avg_pit_loss_s,
            sc_rate=circuit.historical_sc_rate,
            overtaking_difficulty=circuit.historical_overtaking_difficulty,
            tyre_deg_rate=tyre_deg_rate,  # 2026-derived, supplied by the caller
        )

    # -- context + persistence ---------------------------------------------
    def context_for(
        self,
        session: m.Session,
        driver: m.Driver,
        *,
        live: LiveState | None = None,
        tyre_deg_rate: float | None = None,
    ) -> FeatureContext:
        team_name = driver.team.name if driver.team else "unknown"
        forms = self.team_forms()
        profiles = self.driver_profiles(session.circuit_id)
        return FeatureContext(
            session_type=session.session_type,
            regs_regime=session.regs_regime,
            is_sprint_weekend=session.is_sprint_weekend,
            is_night=_is_night(session),
            weather=session.weather_snapshot or {},
            team=forms.get(team_name, TeamForm()),
            driver=profiles.get(driver.id, DriverProfile()),
            circuit=self.circuit_profile(session.circuit, tyre_deg_rate),
            live=live or LiveState(),
        )

    def build_and_store(
        self,
        session: m.Session,
        driver: m.Driver,
        context_label: str,
        *,
        live: LiveState | None = None,
        tyre_deg_rate: float | None = None,
    ) -> FeatureVector:
        """Compute a vector and upsert it as a :class:`FeatureSnapshot`."""
        ctx = self.context_for(session, driver, live=live, tyre_deg_rate=tyre_deg_rate)
        vector = build_feature_vector(ctx, context_label)
        if vector.fallbacks_used:
            log.debug(
                "session %s driver %s context %s used fallbacks: %s",
                session.id, driver.id, context_label, ", ".join(vector.fallbacks_used),
            )

        existing = self.db.scalar(
            select(m.FeatureSnapshot).where(
                m.FeatureSnapshot.session_id == session.id,
                m.FeatureSnapshot.driver_id == driver.id,
                m.FeatureSnapshot.context == context_label,
            )
        )
        if existing is None:
            existing = m.FeatureSnapshot(
                session_id=session.id,
                driver_id=driver.id,
                context=context_label,
                regs_regime=session.regs_regime,
                features={},
            )
            self.db.add(existing)
        existing.features = dict(vector.values)
        self.db.flush()
        return vector


def live_state_from_openf1(
    state: LiveDriverState,
    *,
    total_laps: int | None = None,
    gap_behind_s: float | None = None,
    compound_deg_rate: float | None = None,
    safety_car: bool = False,
    vsc: bool = False,
    red_flag: bool = False,
) -> LiveState:
    """Adapt an OpenF1 car state into the pure-feature :class:`LiveState`."""
    return LiveState(
        lap_number=state.lap_number,
        total_laps=total_laps,
        position=state.position,
        gap_ahead_s=state.interval_s,
        gap_behind_s=gap_behind_s,
        compound=state.compound,
        tyre_age_laps=state.tyre_age_laps,
        compound_deg_rate=compound_deg_rate,
        manual_override_enabled=state.manual_override_active,
        safety_car_active=safety_car,
        vsc_active=vsc,
        red_flag_active=red_flag,
    )


def _is_night(session: m.Session) -> bool:
    if session.start_time is None:
        return False
    return session.start_time.hour >= 18 or session.start_time.hour < 6
