"""Feature computation (guide section 7), obeying the transfer table (section 2).

Design notes:

* Feature functions are **pure**: they take an already-assembled
  :class:`FeatureContext` of raw inputs and return numbers. Network and database
  access happen in the aggregators above them, which keeps the whole catalog
  unit-testable without FastF1 or OpenF1.
* Every core feature has a declared fallback. M2 requires a complete vector with
  no missing values, so a gap becomes a documented neutral value plus an entry
  in ``FeatureVector.fallbacks_used`` -- never a silent ``None`` reaching XGBoost.
* Car/team/tyre inputs are sourced from current-regime aggregates only; driver
  and track-geometry inputs may use the full historical record. The split is
  enforced downstream by :mod:`app.features.transfer`, and mirrored here in how
  the aggregates are built.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.features.transfer import assert_policy_covered

log = logging.getLogger(__name__)

# Compounds, ordered soft -> hard, plus wets. Ordinal encoding is deliberate:
# the ordering carries real information about grip and life.
COMPOUND_ORDER = {"SOFT": 1.0, "MEDIUM": 2.0, "HARD": 3.0, "INTERMEDIATE": 4.0, "WET": 5.0}

SESSION_TYPE_CODE = {
    "FP1": 1.0, "FP2": 2.0, "FP3": 3.0, "SQ": 4.0,
    "Q1": 5.0, "Q2": 6.0, "Q3": 7.0, "Qualifying": 6.0,
    "Sprint": 8.0, "Race": 9.0,
}

#: Neutral value used when an input is genuinely unavailable. Percentiles sit at
#: mid-field, gaps at zero, rates at a grid-typical average.
FALLBACKS: dict[str, float] = {
    "team_quali_pace_percentile": 0.5,
    "team_race_pace_percentile": 0.5,
    "team_recent_form_delta": 0.0,
    "team_power_unit_code": 0.0,
    "team_dnf_rate": 0.08,
    "team_manual_override_gain_kph": 0.0,
    "team_fuel_corrected_pace_s": 0.0,
    "team_active_aero_x_mode_share": 0.5,
    "driver_historical_baseline": 0.5,
    "driver_teammate_quali_gap_s": 0.0,
    "driver_teammate_race_gap_s": 0.0,
    "driver_track_history_score": 0.5,
    "driver_wet_rating": 0.5,
    "driver_consistency": 0.5,
    "driver_is_rookie": 0.0,
    "driver_is_new_to_team": 0.0,
    "driver_grid_to_finish_delta": 0.0,
    "circuit_type_street": 0.0,
    "circuit_type_permanent": 1.0,
    "circuit_type_hybrid": 0.0,
    "circuit_overtaking_difficulty": 0.5,
    "circuit_sc_rate": 0.4,
    "circuit_altitude_m": 100.0,
    "circuit_pit_loss_s": 22.0,
    "circuit_tyre_deg_rate": 0.05,
    "weather_air_temp_c": 25.0,
    "weather_track_temp_c": 35.0,
    "weather_humidity": 50.0,
    "weather_rainfall": 0.0,
    "weather_wind_speed": 2.0,
    "session_is_night": 0.0,
    "tyre_compound_code": 2.0,
    "tyre_age_laps": 0.0,
    "tyre_deg_rate_compound": 0.05,
    "tyre_age_x_track_temp": 0.0,
    "rc_safety_car_active": 0.0,
    "rc_vsc_active": 0.0,
    "rc_red_flag_active": 0.0,
    "rc_manual_override_enabled": 0.0,
    "session_type_code": 9.0,
    "session_is_sprint_weekend": 0.0,
    "lap_number": 0.0,
    "laps_remaining": 0.0,
    "gap_ahead_s": 0.0,
    "gap_behind_s": 0.0,
    "current_position": 10.0,
}

# --- feature sets per model (core features only; rule 5) --------------------

DRIVER_TRACK_FEATURES = [
    "driver_historical_baseline",
    "driver_track_history_score",
    "driver_wet_rating",
    "driver_is_rookie",
    "driver_is_new_to_team",
    "circuit_type_street",
    "circuit_type_permanent",
    "circuit_type_hybrid",
    "circuit_overtaking_difficulty",
    "circuit_sc_rate",
    "weather_air_temp_c",
    "weather_track_temp_c",
    "weather_humidity",
    "weather_rainfall",
    "session_is_night",
    "session_is_sprint_weekend",
]

TEAM_FEATURES = [
    "team_quali_pace_percentile",
    "team_race_pace_percentile",
    "team_recent_form_delta",
    "team_power_unit_code",
    "team_dnf_rate",
    "team_manual_override_gain_kph",
]

QUALIFYING_FEATURES = [
    *TEAM_FEATURES,
    *DRIVER_TRACK_FEATURES,
    "driver_teammate_quali_gap_s",
    "session_type_code",
]

RACE_FEATURES = [
    *TEAM_FEATURES,
    *DRIVER_TRACK_FEATURES,
    "driver_teammate_race_gap_s",
    "circuit_pit_loss_s",
    "circuit_tyre_deg_rate",
    "tyre_compound_code",
    "tyre_age_laps",
    "tyre_deg_rate_compound",
    "rc_safety_car_active",
    "rc_vsc_active",
    "rc_manual_override_enabled",
    "lap_number",
    "laps_remaining",
    "gap_ahead_s",
    "gap_behind_s",
    "current_position",
]

FEATURE_SETS: dict[str, list[str]] = {
    "qualifying_time": QUALIFYING_FEATURES,
    "qualifying_advancement": QUALIFYING_FEATURES,
    "race_finish_position": RACE_FEATURES,
    "race_strategy": RACE_FEATURES,
}

for _name, _fs in FEATURE_SETS.items():
    assert_policy_covered(_fs)  # fail at import if a policy is missing (rule 3)


# --- inputs ----------------------------------------------------------------


@dataclass
class TeamForm:
    """Current-regime team aggregates. 2026 data only, per the transfer table."""

    quali_pace_percentile: float | None = None
    race_pace_percentile: float | None = None
    recent_form_delta: float | None = None
    power_unit_code: float | None = None
    dnf_rate: float | None = None
    manual_override_gain_kph: float | None = None
    fuel_corrected_pace_s: float | None = None
    sessions_observed: int = 0


@dataclass
class DriverProfile:
    """Driver aggregates. Full historical record is fair game (human skill)."""

    baseline: float | None = None
    track_history_score: float | None = None
    wet_rating: float | None = None
    consistency: float | None = None
    grid_to_finish_delta: float | None = None
    is_rookie: bool = False
    is_new_to_team: bool = False
    teammate_quali_gap_s: float | None = None
    teammate_race_gap_s: float | None = None


@dataclass
class CircuitProfile:
    """Track geometry. Transfers, assuming the layout is unchanged."""

    circuit_type: str = "permanent"
    altitude_m: float | None = None
    pit_loss_s: float | None = None
    sc_rate: float | None = None
    overtaking_difficulty: float | None = None
    tyre_deg_rate: float | None = None  # 2026-derived, not historical


@dataclass
class LiveState:
    """Per-car live inputs, from OpenF1 during a session."""

    lap_number: int | None = None
    total_laps: int | None = None
    position: int | None = None
    gap_ahead_s: float | None = None
    gap_behind_s: float | None = None
    compound: str | None = None
    tyre_age_laps: int | None = None
    compound_deg_rate: float | None = None
    manual_override_enabled: bool | None = None
    safety_car_active: bool = False
    vsc_active: bool = False
    red_flag_active: bool = False


@dataclass
class FeatureContext:
    """Everything one feature vector is computed from."""

    session_type: str
    regs_regime: str
    is_sprint_weekend: bool = False
    is_night: bool = False
    weather: dict[str, Any] = field(default_factory=dict)
    team: TeamForm = field(default_factory=TeamForm)
    driver: DriverProfile = field(default_factory=DriverProfile)
    circuit: CircuitProfile = field(default_factory=CircuitProfile)
    live: LiveState = field(default_factory=LiveState)


@dataclass
class FeatureVector:
    values: dict[str, float]
    fallbacks_used: list[str]
    context_label: str

    def as_row(self, feature_names: list[str]) -> list[float]:
        return [self.values[name] for name in feature_names]

    @property
    def is_complete(self) -> bool:
        return not self.fallbacks_used


# --- computation -----------------------------------------------------------


class _Collector:
    """Applies the declared fallback whenever an input is missing, and records it."""

    def __init__(self) -> None:
        self.values: dict[str, float] = {}
        self.fallbacks: list[str] = []

    def set(self, name: str, value: Any) -> None:
        coerced = _num(value)
        if coerced is None:
            if name not in FALLBACKS:
                raise KeyError(f"no fallback declared for feature {name!r}")
            coerced = FALLBACKS[name]
            self.fallbacks.append(name)
        self.values[name] = coerced


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def build_feature_vector(ctx: FeatureContext, context_label: str) -> FeatureVector:
    """Compute the full core feature catalog for one driver in one context."""
    c = _Collector()

    # --- car & team (2026 only) -------------------------------------------
    c.set("team_quali_pace_percentile", ctx.team.quali_pace_percentile)
    c.set("team_race_pace_percentile", ctx.team.race_pace_percentile)
    c.set("team_recent_form_delta", ctx.team.recent_form_delta)
    c.set("team_power_unit_code", ctx.team.power_unit_code)
    c.set("team_dnf_rate", ctx.team.dnf_rate)
    # New for 2026: energy deployment efficiency, proxied by speed-trap gain in
    # Manual Override zones. Neutral (0.0) until enough telemetry exists.
    c.set("team_manual_override_gain_kph", ctx.team.manual_override_gain_kph)

    # --- driver (full history) --------------------------------------------
    c.set("driver_historical_baseline", ctx.driver.baseline)
    c.set("driver_track_history_score", ctx.driver.track_history_score)
    c.set("driver_wet_rating", ctx.driver.wet_rating)
    c.set("driver_is_rookie", ctx.driver.is_rookie)
    c.set("driver_is_new_to_team", ctx.driver.is_new_to_team)
    c.set("driver_teammate_quali_gap_s", ctx.driver.teammate_quali_gap_s)
    c.set("driver_teammate_race_gap_s", ctx.driver.teammate_race_gap_s)

    # --- circuit (geometry, transfers) ------------------------------------
    ctype = (ctx.circuit.circuit_type or "permanent").lower()
    c.set("circuit_type_street", ctype == "street")
    c.set("circuit_type_permanent", ctype == "permanent")
    c.set("circuit_type_hybrid", ctype == "hybrid")
    c.set("circuit_overtaking_difficulty", ctx.circuit.overtaking_difficulty)
    c.set("circuit_sc_rate", ctx.circuit.sc_rate)
    c.set("circuit_pit_loss_s", ctx.circuit.pit_loss_s)
    c.set("circuit_tyre_deg_rate", ctx.circuit.tyre_deg_rate)  # 2026-derived

    # --- weather (transfers) ----------------------------------------------
    w = ctx.weather or {}
    c.set("weather_air_temp_c", w.get("air_temp"))
    c.set("weather_track_temp_c", w.get("track_temp"))
    c.set("weather_humidity", w.get("humidity"))
    c.set("weather_rainfall", bool(w.get("rainfall")) if w.get("rainfall") is not None else None)
    c.set("session_is_night", ctx.is_night)

    # --- tyres (2026 only) -------------------------------------------------
    compound = (ctx.live.compound or "").upper()
    c.set("tyre_compound_code", COMPOUND_ORDER.get(compound))
    c.set("tyre_age_laps", ctx.live.tyre_age_laps)
    c.set("tyre_deg_rate_compound", ctx.live.compound_deg_rate)

    # --- race control ------------------------------------------------------
    c.set("rc_safety_car_active", ctx.live.safety_car_active)
    c.set("rc_vsc_active", ctx.live.vsc_active)
    c.set("rc_red_flag_active", ctx.live.red_flag_active)
    c.set("rc_manual_override_enabled", ctx.live.manual_override_enabled)

    # --- session / relative position ---------------------------------------
    c.set("session_type_code", SESSION_TYPE_CODE.get(ctx.session_type))
    c.set("session_is_sprint_weekend", ctx.is_sprint_weekend)
    c.set("lap_number", ctx.live.lap_number)
    remaining = (
        ctx.live.total_laps - ctx.live.lap_number
        if ctx.live.total_laps is not None and ctx.live.lap_number is not None
        else None
    )
    c.set("laps_remaining", remaining)
    c.set("gap_ahead_s", ctx.live.gap_ahead_s)
    c.set("gap_behind_s", ctx.live.gap_behind_s)
    c.set("current_position", ctx.live.position)

    return FeatureVector(values=c.values, fallbacks_used=c.fallbacks, context_label=context_label)


# --- aggregate derivation ---------------------------------------------------


def percentile_rank(value: float, population: list[float], *, lower_is_better: bool = True) -> float:
    """Rank of ``value`` within ``population``, mapped to 0..1 (1 = best)."""
    clean = [v for v in population if v is not None]
    if not clean:
        return 0.5
    better = sum(1 for v in clean if (v < value if lower_is_better else v > value))
    rank = 1.0 - better / len(clean)
    return max(0.0, min(1.0, rank))


def team_form_from_sessions(
    lap_times_by_team: dict[str, list[float]],
    *,
    dnfs_by_team: dict[str, tuple[int, int]] | None = None,
    recent_deltas_by_team: dict[str, float] | None = None,
    override_gain_by_team: dict[str, float] | None = None,
    power_unit_codes: dict[str, float] | None = None,
) -> dict[str, TeamForm]:
    """Build per-team form from **current-regime sessions only**.

    Callers are responsible for passing 2026 sessions here; passing pre-reset
    data would violate the transfer table (guide section 2), which is why this
    function takes already-filtered inputs rather than querying seasons itself.
    """
    best_by_team = {
        team: min(times) for team, times in lap_times_by_team.items() if times
    }
    population = list(best_by_team.values())
    forms: dict[str, TeamForm] = {}
    for team, best in best_by_team.items():
        finished, started = (dnfs_by_team or {}).get(team, (0, 0))
        pace = percentile_rank(best, population)
        forms[team] = TeamForm(
            quali_pace_percentile=pace,
            race_pace_percentile=pace,
            recent_form_delta=(recent_deltas_by_team or {}).get(team),
            power_unit_code=(power_unit_codes or {}).get(team),
            dnf_rate=((started - finished) / started) if started else None,
            manual_override_gain_kph=(override_gain_by_team or {}).get(team),
            sessions_observed=len(lap_times_by_team.get(team, [])),
        )
    return forms


def manual_override_gain(
    speed_traps_with_override: list[float], speed_traps_without: list[float]
) -> float | None:
    """Energy-deployment efficiency proxy: speed-trap gain while overriding.

    2026's Manual Override replaces DRS as the overtaking aid, so the equivalent
    of "DRS speed gain" is the delta between trap speeds with and without the
    burst deployed (guide section 7, core feature).
    """
    if not speed_traps_with_override or not speed_traps_without:
        return None
    with_avg = sum(speed_traps_with_override) / len(speed_traps_with_override)
    without_avg = sum(speed_traps_without) / len(speed_traps_without)
    return with_avg - without_avg


def fuel_corrected_lap_time(
    lap_time_s: float, lap_number: int, total_laps: int, fuel_load_kg: float = 100.0
) -> float:
    """Correct a lap time to a nominal full-fuel equivalent (extended feature).

    Uses the ~0.03-0.05 s/kg/lap band from the guide, taking the midpoint.
    """
    if total_laps <= 0:
        return lap_time_s
    burned_fraction = max(0.0, min(1.0, lap_number / total_laps))
    remaining_kg = fuel_load_kg * (1.0 - burned_fraction)
    return lap_time_s - 0.04 * remaining_kg


def driver_track_history_score(finishes_at_circuit: list[float], field_size: int = 20) -> float | None:
    """Normalised track-specific history: 1.0 = always wins, 0.0 = always last."""
    clean = [p for p in finishes_at_circuit if p]
    if not clean:
        return None
    avg = sum(clean) / len(clean)
    return max(0.0, min(1.0, 1.0 - (avg - 1) / max(1, field_size - 1)))


def driver_wet_rating(wet_finishes: list[float], dry_finishes: list[float]) -> float | None:
    """How much better (or worse) a driver finishes in the wet, mapped to 0..1."""
    if not wet_finishes or not dry_finishes:
        return None
    wet_avg = sum(wet_finishes) / len(wet_finishes)
    dry_avg = sum(dry_finishes) / len(dry_finishes)
    # A 5-position swing saturates the scale.
    return max(0.0, min(1.0, 0.5 + (dry_avg - wet_avg) / 10.0))
