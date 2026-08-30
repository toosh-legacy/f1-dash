"""The transfer table (guide section 2 / rule 3) and the feature catalog."""
from __future__ import annotations

import pytest

from app.features import transfer
from app.features.engineering import (
    FALLBACKS,
    FEATURE_SETS,
    CircuitProfile,
    DriverProfile,
    FeatureContext,
    LiveState,
    TeamForm,
    build_feature_vector,
    driver_wet_rating,
    fuel_corrected_lap_time,
    manual_override_gain,
    percentile_rank,
)


class TestTransferTable:
    def test_car_and_tyre_features_are_regime_only(self):
        for name in (
            "team_quali_pace_percentile",
            "team_race_pace_percentile",
            "circuit_tyre_deg_rate",
            "tyre_compound_code",
            "team_manual_override_gain_kph",
        ):
            assert transfer.is_regime_only(name), f"{name} must not train on pre-2026 data"

    def test_driver_and_geometry_features_use_full_history(self):
        for name in (
            "driver_historical_baseline",
            "driver_track_history_score",
            "driver_wet_rating",
            "circuit_altitude_m",
            "weather_air_temp_c",
        ):
            assert transfer.policy_for(name).transfer is transfer.Transfer.FULL_HISTORY

    def test_power_unit_and_pit_loss_are_weak_transfer(self):
        assert transfer.policy_for("team_power_unit_code").transfer is transfer.Transfer.WEAK
        assert transfer.policy_for("circuit_pit_loss_s").transfer is transfer.Transfer.WEAK

    def test_undeclared_feature_raises(self):
        with pytest.raises(transfer.UndeclaredFeatureError):
            transfer.assert_policy_covered(["some_new_feature"])

    def test_pre_regime_row_dropped_when_matrix_has_regime_only_features(self):
        weight = transfer.row_sample_weight("2025", ["team_quali_pace_percentile", "driver_wet_rating"])
        assert weight == 0.0

    def test_pre_regime_row_downweighted_for_weak_only_matrix(self):
        weight = transfer.row_sample_weight("2025", ["circuit_pit_loss_s", "driver_wet_rating"])
        assert weight == transfer.WEAK_TRANSFER_WEIGHT

    def test_current_regime_row_is_full_weight(self):
        assert transfer.row_sample_weight("2026", ["team_quali_pace_percentile"]) == 1.0

    def test_full_history_only_matrix_keeps_old_rows(self):
        assert transfer.row_sample_weight("2023", ["driver_wet_rating", "circuit_sc_rate"]) == 1.0


class TestFeatureVector:
    def test_every_feature_in_every_model_set_has_a_fallback(self):
        for feature_set in FEATURE_SETS.values():
            for name in feature_set:
                assert name in FALLBACKS, f"{name} has no declared fallback"

    def test_empty_context_produces_a_complete_vector(self):
        """M2: no missing values -- gaps become documented fallbacks."""
        vector = build_feature_vector(FeatureContext(session_type="Race", regs_regime="2026"), "lap_1")
        for feature_set in FEATURE_SETS.values():
            for name in feature_set:
                assert name in vector.values
                assert isinstance(vector.values[name], float)
        assert vector.fallbacks_used, "an empty context should report its fallbacks"

    def test_populated_context_uses_real_values(self):
        ctx = FeatureContext(
            session_type="Race",
            regs_regime="2026",
            weather={"air_temp": 31.0, "track_temp": 48.0, "humidity": 20.0, "rainfall": False},
            team=TeamForm(quali_pace_percentile=0.9, race_pace_percentile=0.85, dnf_rate=0.1),
            driver=DriverProfile(baseline=0.8, wet_rating=0.7, is_rookie=True),
            circuit=CircuitProfile(circuit_type="street", pit_loss_s=19.0, tyre_deg_rate=0.08),
            live=LiveState(lap_number=23, total_laps=57, position=4, compound="MEDIUM", tyre_age_laps=12),
        )
        vector = build_feature_vector(ctx, "lap_23")
        assert vector.values["weather_track_temp_c"] == 48.0
        assert vector.values["circuit_type_street"] == 1.0
        assert vector.values["circuit_type_permanent"] == 0.0
        assert vector.values["laps_remaining"] == 34.0
        assert vector.values["tyre_compound_code"] == 2.0
        assert vector.values["driver_is_rookie"] == 1.0

    def test_gating_flags_reach_the_vector(self):
        ctx = FeatureContext(
            session_type="Race", regs_regime="2026", live=LiveState(safety_car_active=True)
        )
        vector = build_feature_vector(ctx, "lap_10")
        assert vector.values["rc_safety_car_active"] == 1.0
        assert vector.values["rc_vsc_active"] == 0.0


class TestDerivedFeatures:
    def test_percentile_rank_puts_fastest_on_top(self):
        population = [80.0, 81.0, 82.0, 83.0]
        assert percentile_rank(80.0, population) == 1.0
        assert percentile_rank(83.0, population) == 0.25

    def test_manual_override_gain_is_a_speed_delta(self):
        assert manual_override_gain([320.0, 322.0], [310.0, 312.0]) == pytest.approx(10.0)

    def test_manual_override_gain_without_data_is_none(self):
        assert manual_override_gain([], [300.0]) is None

    def test_fuel_correction_moves_early_laps_more(self):
        early = fuel_corrected_lap_time(90.0, lap_number=1, total_laps=50)
        late = fuel_corrected_lap_time(90.0, lap_number=49, total_laps=50)
        assert early < late, "a heavier car should correct to a larger delta"

    def test_wet_rating_rewards_better_wet_finishes(self):
        assert driver_wet_rating([2.0], [8.0]) > 0.5
        assert driver_wet_rating([12.0], [4.0]) < 0.5
