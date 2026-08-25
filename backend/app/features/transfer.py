"""The regulation-transfer table from guide section 2, as executable policy.

The table is the single most important piece of judgment in this project, so it
lives here as data that pipelines *query*, not as a comment that pipelines are
trusted to have read. Every training pipeline calls
:func:`training_years_for_feature` (or :func:`split_feature_names`) before it
assembles a training matrix; :func:`assert_policy_covered` fails loudly when a
new feature is added without a declared policy.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from app.config import settings


class Transfer(str, enum.Enum):
    """Does a feature's pre-2026 history still describe the 2026 car?"""

    # Current-regime data only. Pre-reset rows are actively misleading.
    REGIME_ONLY = "regime_only"
    # Full historical record is fair game (human skill, track geometry, weather).
    FULL_HISTORY = "full_history"
    # Partial carry-over: usable, but down-weighted and never dominant.
    WEAK = "weak"


# Sample weight applied to pre-regime rows for WEAK features. Engineering
# culture may partially carry over; the hardware does not.
WEAK_TRANSFER_WEIGHT = 0.25


@dataclass(frozen=True)
class FeaturePolicy:
    name: str
    transfer: Transfer
    rationale: str


def _p(name: str, transfer: Transfer, rationale: str) -> tuple[str, FeaturePolicy]:
    return name, FeaturePolicy(name, transfer, rationale)


#: feature name -> policy. Keys must match the keys produced by
#: :mod:`app.features.engineering` exactly.
FEATURE_POLICY: dict[str, FeaturePolicy] = dict(
    [
        # --- Car & team performance ---------------------------------------
        _p("team_quali_pace_percentile", Transfer.REGIME_ONLY, "every car is a new design"),
        _p("team_race_pace_percentile", Transfer.REGIME_ONLY, "every car is a new design"),
        _p("team_recent_form_delta", Transfer.REGIME_ONLY, "current-season trend only"),
        _p("team_power_unit_code", Transfer.WEAK, "MGU-H removal makes the unit a new design"),
        _p("team_dnf_rate", Transfer.WEAK, "engineering culture partially carries over"),
        _p("team_manual_override_gain_kph", Transfer.REGIME_ONLY, "2026-only mechanic, replaces DRS"),
        _p("team_fuel_corrected_pace_s", Transfer.REGIME_ONLY, "derived from current-car pace"),
        _p("team_active_aero_x_mode_share", Transfer.REGIME_ONLY, "2026-only mechanic"),
        # --- Driver (human skill: transfers) -------------------------------
        _p("driver_historical_baseline", Transfer.FULL_HISTORY, "driver skill is a human attribute"),
        _p("driver_teammate_quali_gap_s", Transfer.REGIME_ONLY, "compares current cars"),
        _p("driver_teammate_race_gap_s", Transfer.REGIME_ONLY, "compares current cars"),
        _p("driver_track_history_score", Transfer.FULL_HISTORY, "circuit knowledge carries over"),
        _p("driver_wet_rating", Transfer.FULL_HISTORY, "human skill signal"),
        _p("driver_consistency", Transfer.FULL_HISTORY, "human skill signal"),
        _p("driver_is_rookie", Transfer.FULL_HISTORY, "driver metadata"),
        _p("driver_is_new_to_team", Transfer.FULL_HISTORY, "season metadata"),
        _p("driver_grid_to_finish_delta", Transfer.FULL_HISTORY, "human racecraft signal"),
        # --- Track / circuit (geometry: transfers) -------------------------
        _p("circuit_type_street", Transfer.FULL_HISTORY, "physical geometry"),
        _p("circuit_type_permanent", Transfer.FULL_HISTORY, "physical geometry"),
        _p("circuit_type_hybrid", Transfer.FULL_HISTORY, "physical geometry"),
        _p("circuit_overtaking_difficulty", Transfer.FULL_HISTORY, "layout-driven"),
        _p("circuit_sc_rate", Transfer.FULL_HISTORY, "layout-driven"),
        _p("circuit_altitude_m", Transfer.FULL_HISTORY, "geography"),
        _p("circuit_pit_loss_s", Transfer.WEAK, "geometry usually unchanged; revalidate on 2026 data"),
        _p("circuit_tyre_deg_rate", Transfer.REGIME_ONLY, "new tyre dimensions reset the curve"),
        # --- Weather (independent of car regs) -----------------------------
        _p("weather_air_temp_c", Transfer.FULL_HISTORY, "weather is regulation-independent"),
        _p("weather_track_temp_c", Transfer.FULL_HISTORY, "weather is regulation-independent"),
        _p("weather_humidity", Transfer.FULL_HISTORY, "weather is regulation-independent"),
        _p("weather_rainfall", Transfer.FULL_HISTORY, "weather is regulation-independent"),
        _p("weather_wind_speed", Transfer.FULL_HISTORY, "weather is regulation-independent"),
        _p("session_is_night", Transfer.FULL_HISTORY, "schedule metadata"),
        # --- Tyres (2026 only) ---------------------------------------------
        _p("tyre_compound_code", Transfer.REGIME_ONLY, "new construction"),
        _p("tyre_age_laps", Transfer.REGIME_ONLY, "new construction"),
        _p("tyre_deg_rate_compound", Transfer.REGIME_ONLY, "new construction"),
        _p("tyre_age_x_track_temp", Transfer.REGIME_ONLY, "new construction"),
        # --- Race control ----------------------------------------------------
        _p("rc_safety_car_active", Transfer.FULL_HISTORY, "procedural, not car-dependent"),
        _p("rc_vsc_active", Transfer.FULL_HISTORY, "procedural"),
        _p("rc_red_flag_active", Transfer.FULL_HISTORY, "procedural"),
        _p("rc_manual_override_enabled", Transfer.REGIME_ONLY, "2026-only field, replaces DRS"),
        # --- Session / relative position -------------------------------------
        _p("session_type_code", Transfer.FULL_HISTORY, "session metadata"),
        _p("session_is_sprint_weekend", Transfer.FULL_HISTORY, "calendar metadata"),
        _p("lap_number", Transfer.REGIME_ONLY, "paired with current-car pace"),
        _p("laps_remaining", Transfer.REGIME_ONLY, "paired with current-car pace"),
        _p("gap_ahead_s", Transfer.REGIME_ONLY, "current-car field spread"),
        _p("gap_behind_s", Transfer.REGIME_ONLY, "current-car field spread"),
        _p("current_position", Transfer.REGIME_ONLY, "current-car field order"),
    ]
)


class UndeclaredFeatureError(KeyError):
    """Raised when a feature reaches a training pipeline without a transfer policy."""


def policy_for(feature_name: str) -> FeaturePolicy:
    try:
        return FEATURE_POLICY[feature_name]
    except KeyError as exc:  # pragma: no cover - defensive
        raise UndeclaredFeatureError(
            f"feature {feature_name!r} has no transfer policy; add it to "
            "app/features/transfer.py before using it in training (guide rule 3)"
        ) from exc


def assert_policy_covered(feature_names: list[str]) -> None:
    """Fail loudly if any feature lacks a declared policy."""
    missing = [n for n in feature_names if n not in FEATURE_POLICY]
    if missing:
        raise UndeclaredFeatureError(
            "features without a transfer policy (guide rule 3): " + ", ".join(sorted(missing))
        )


def split_feature_names(feature_names: list[str]) -> dict[Transfer, list[str]]:
    """Group feature names by transfer policy."""
    assert_policy_covered(feature_names)
    grouped: dict[Transfer, list[str]] = {t: [] for t in Transfer}
    for name in feature_names:
        grouped[FEATURE_POLICY[name].transfer].append(name)
    return grouped


def is_regime_only(feature_name: str) -> bool:
    return policy_for(feature_name).transfer is Transfer.REGIME_ONLY


def row_sample_weight(row_regime: str, feature_names: list[str]) -> float:
    """Sample weight for one training row given the regimes its features span.

    A row from an older regime is only usable if none of the features in the
    matrix are regime-only; if any are, the row is dropped (weight 0). Rows that
    only touch WEAK features are down-weighted rather than dropped.
    """
    if row_regime == settings.CURRENT_REGS_REGIME:
        return 1.0
    grouped = split_feature_names(feature_names)
    if grouped[Transfer.REGIME_ONLY]:
        return 0.0
    if grouped[Transfer.WEAK]:
        return WEAK_TRANSFER_WEIGHT
    return 1.0


def training_years_for_feature(feature_name: str, current_season: int | None = None) -> str:
    """Human-readable description of the year range a feature may train on."""
    season = current_season or settings.CURRENT_SEASON
    transfer = policy_for(feature_name).transfer
    if transfer is Transfer.REGIME_ONLY:
        return f"{season} only"
    if transfer is Transfer.WEAK:
        return f"{season} primary, earlier seasons down-weighted to {WEAK_TRANSFER_WEIGHT}"
    return "full historical record"
