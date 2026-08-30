"""Pydantic response/request schemas for the REST API (guide section 9)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())


class CircuitOut(ORMModel):
    id: str
    name: str
    type: str
    altitude_m: float | None = None
    avg_pit_loss_s: float | None = None
    historical_sc_rate: float | None = None
    historical_overtaking_difficulty: float | None = None


class DriverOut(ORMModel):
    id: str
    name: str
    driver_number: int | None = None
    team_id: str | None = None
    is_rookie: bool = False


class SessionOut(ORMModel):
    id: int
    year: int
    circuit_id: str
    session_type: str
    regs_regime: str
    is_sprint_weekend: bool
    start_time: datetime | None = None
    status: str
    weather_snapshot: dict[str, Any] | None = None
    openf1_session_key: int | None = None


class QualifyingPredictionOut(ORMModel):
    driver_id: str
    period: str
    predicted_time_s: float | None = None
    advancement_probability: float | None = None
    model_version: int | None = None
    created_at: datetime


class RacePredictionOut(ORMModel):
    driver_id: str
    lap_number: int
    predicted_finish_position: float | None = None
    strategy_probabilities: dict[str, float] | None = None
    is_gated: bool
    gate_reason: str | None = None
    model_version: int | None = None
    created_at: datetime


class PredictionModelOut(ORMModel):
    id: int
    model_type: str
    version: int
    trained_at: datetime
    validation_score: float | None = None
    validation_metric: str | None = None
    validation_detail: dict[str, Any] | None = None
    training_rows: int | None = None
    is_active: bool
    regs_regime: str


class RaceControlEventOut(ORMModel):
    event_type: str
    lap_number: int | None = None
    message: str | None = None
    created_at: datetime


class PracticeDriverForm(BaseModel):
    """Current-form summary -- explicitly *not* a forward prediction."""

    driver_id: str
    driver_name: str
    team: str | None = None
    best_lap_s: float | None = None
    gap_to_best_s: float | None = None
    laps_completed: int = 0
    compound: str | None = None


class PracticeSummaryOut(BaseModel):
    session_id: int
    session_type: str
    note: str = Field(
        default="Current-form summary from completed laps. Not a forward prediction.",
    )
    drivers: list[PracticeDriverForm] = []


class JobOut(BaseModel):
    job_id: str
    kind: str
    session_id: int | None = None
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    stages: list[str] = []


class PromoteResponse(BaseModel):
    id: int
    model_type: str
    version: int
    is_active: bool
    previous_active_version: int | None = None


class LiveLoopOut(BaseModel):
    session_id: int
    session_key: int | None = None
    kind: str
    running: bool
    polls: int = 0
    updates_published: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    last_context: str | None = None
    uptime_s: float | None = None


class SeedRequest(BaseModel):
    year: int | None = None
    limit_events: int | None = Field(default=None, description="Seed only the first N events")


class StartLoopRequest(BaseModel):
    openf1_session_key: int | None = Field(
        default=None,
        description=(
            "OpenF1 session key. Omit to reuse the one stored on the session. "
            "A completed session's key replays that session, which is how the "
            "loops are exercised out of season."
        ),
    )
    total_laps: int | None = None
    poll_interval_s: float | None = None


class HealthOut(BaseModel):
    status: str
    regs_regime: str
    season: int
    database: str
    active_models: dict[str, int]
    live_loops: int
    pending_jobs: int

    # Operational detail. The counts above say what is configured; these say
    # whether it is working -- how long since each live loop last heard
    # anything, how long a live inference tick takes, and how deep the
    # background queue that training runs on is.
    live: list[dict[str, Any]] = Field(default_factory=list)
    jobs: dict[str, Any] = Field(default_factory=dict)
    caches: dict[str, Any] = Field(default_factory=dict)
    last_trained_at: str | None = None


class ReplayRoundOut(BaseModel):
    """One race in the replay catalogue."""

    session_key: int
    meeting_key: int | None = None
    name: str
    session_type: str
    circuit: str | None = None
    country: str | None = None
    location: str | None = None
    date_start: str | None = None
    year: int | None = None
    cached: bool = Field(description="Whether the replay bundle has been built")
    size_bytes: int | None = None


class ProjectionRunOut(BaseModel):
    """One evaluation pass over a replay, under one model version."""

    model_version: int | None = Field(
        default=None, description="Null when the pass ran with no active model"
    )
    laps: int
    first_lap: int | None = None
    last_lap: int | None = None
    computed_at: str | None = None


class ProjectionCoverageOut(BaseModel):
    """What has already been evaluated for a replay."""

    session_key: int
    built: bool
    active_model_version: int | None = None
    runs: list[ProjectionRunOut] = []


class ClearedOut(BaseModel):
    session_key: int
    cleared: int
