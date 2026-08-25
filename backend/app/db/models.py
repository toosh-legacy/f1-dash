"""SQLAlchemy models -- the field-level specification from guide section 8.

Two rules are enforced structurally here rather than by convention:
  * every ``Session`` carries a ``regs_regime`` (rule 6);
  * every ``PredictionModel`` records the regime it was trained under, so a
    model can never silently serve predictions across a regulation reset.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.db.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CircuitType(str, enum.Enum):
    STREET = "street"
    PERMANENT = "permanent"
    HYBRID = "hybrid"


class SessionType(str, enum.Enum):
    FP1 = "FP1"
    FP2 = "FP2"
    FP3 = "FP3"
    SQ = "SQ"
    SPRINT = "Sprint"
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    QUALIFYING = "Qualifying"
    RACE = "Race"

    @property
    def is_qualifying(self) -> bool:
        return self in {
            SessionType.Q1,
            SessionType.Q2,
            SessionType.Q3,
            SessionType.QUALIFYING,
            SessionType.SQ,
        }

    @property
    def is_race(self) -> bool:
        return self in {SessionType.RACE, SessionType.SPRINT}

    @property
    def is_practice(self) -> bool:
        return self in {SessionType.FP1, SessionType.FP2, SessionType.FP3}


class SessionStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    COMPLETED = "completed"


class ModelType(str, enum.Enum):
    QUALIFYING_TIME = "qualifying_time"
    QUALIFYING_ADVANCEMENT = "qualifying_advancement"
    RACE_STRATEGY = "race_strategy"
    RACE_FINISH_POSITION = "race_finish_position"


class RaceControlEventType(str, enum.Enum):
    GREEN = "green"
    YELLOW = "yellow"
    SAFETY_CAR = "safety_car"
    VSC = "vsc"
    RED_FLAG = "red_flag"

    @property
    def suppresses_predictions(self) -> bool:
        """Non-green states gate live race predictions (rule 4)."""
        return self is not RaceControlEventType.GREEN


class Circuit(Base):
    __tablename__ = "circuits"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    altitude_m: Mapped[float | None] = mapped_column(Float)
    avg_pit_loss_s: Mapped[float | None] = mapped_column(Float)
    historical_sc_rate: Mapped[float | None] = mapped_column(Float)
    historical_overtaking_difficulty: Mapped[float | None] = mapped_column(Float)

    sessions: Mapped[list["Session"]] = relationship(back_populates="circuit")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 2026 power units: no MGU-H, ~350kW MGU-K across the grid.
    power_unit: Mapped[str | None] = mapped_column(String(64))

    drivers: Mapped[list["Driver"]] = relationship(back_populates="team")


class Driver(Base):
    __tablename__ = "drivers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    driver_number: Mapped[int | None] = mapped_column(Integer, index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"))
    is_rookie: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    joined_team_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    team: Mapped[Team | None] = relationship(back_populates="drivers")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    circuit_id: Mapped[str] = mapped_column(ForeignKey("circuits.id"), nullable=False)
    session_type: Mapped[str] = mapped_column(String(16), nullable=False)
    regs_regime: Mapped[str] = mapped_column(
        String(16), nullable=False, default=settings.CURRENT_REGS_REGIME, index=True
    )
    is_sprint_weekend: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default=SessionStatus.SCHEDULED.value, nullable=False)
    weather_snapshot: Mapped[dict | None] = mapped_column(JSON)
    # OpenF1's own key for this session, when known -- the join between our
    # historical (FastF1) view and the live (OpenF1) view.
    openf1_session_key: Mapped[int | None] = mapped_column(Integer, index=True)

    circuit: Mapped[Circuit] = relationship(back_populates="sessions")

    __table_args__ = (
        UniqueConstraint("year", "circuit_id", "session_type", name="uq_session_identity"),
        Index("ix_sessions_year_type", "year", "session_type"),
    )


class FeatureSnapshot(Base):
    """The engineered vector actually used for one prediction.

    Stored for two reasons: predictions stay auditable, and once results are
    known the snapshot becomes a labelled training row.
    """

    __tablename__ = "feature_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("drivers.id"), nullable=False, index=True)
    context: Mapped[str] = mapped_column(String(32), nullable=False)  # "Q2", "lap_23", "pre_session"
    features: Mapped[dict] = mapped_column(JSON, nullable=False)
    regs_regime: Mapped[str] = mapped_column(
        String(16), nullable=False, default=settings.CURRENT_REGS_REGIME
    )
    # Labels, filled in once the session completes (see training/labelling.py).
    label_qualifying_time_s: Mapped[float | None] = mapped_column(Float)
    label_advanced: Mapped[bool | None] = mapped_column(Boolean)
    label_finish_position: Mapped[float | None] = mapped_column(Float)
    label_strategy: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "driver_id", "context", name="uq_snapshot_identity"),
    )


class PredictionModel(Base):
    """The model registry (guide section 8 / rule 2)."""

    __tablename__ = "prediction_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_path: Mapped[str] = mapped_column(String(512), nullable=False)
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    validation_score: Mapped[float | None] = mapped_column(Float)
    validation_metric: Mapped[str | None] = mapped_column(String(32))
    validation_detail: Mapped[dict | None] = mapped_column(JSON)
    training_rows: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    regs_regime: Mapped[str] = mapped_column(
        String(16), nullable=False, default=settings.CURRENT_REGS_REGIME
    )

    __table_args__ = (
        UniqueConstraint("model_type", "version", name="uq_model_version"),
        Index("ix_active_model", "model_type", "is_active"),
    )


class QualifyingPrediction(Base):
    __tablename__ = "qualifying_predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("drivers.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(8), nullable=False)  # Q1/Q2/Q3
    predicted_time_s: Mapped[float | None] = mapped_column(Float)
    advancement_probability: Mapped[float | None] = mapped_column(Float)  # null for Q3
    model_version: Mapped[int | None] = mapped_column(ForeignKey("prediction_models.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    model: Mapped[PredictionModel | None] = relationship()

    __table_args__ = (Index("ix_quali_pred_lookup", "session_id", "period", "created_at"),)


class RacePrediction(Base):
    __tablename__ = "race_predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("drivers.id"), nullable=False, index=True)
    lap_number: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_finish_position: Mapped[float | None] = mapped_column(Float)
    strategy_probabilities: Mapped[dict | None] = mapped_column(JSON)
    # True when the prediction was produced during a non-green-flag state (rule 4).
    is_gated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    gate_reason: Mapped[str | None] = mapped_column(String(32))
    model_version: Mapped[int | None] = mapped_column(ForeignKey("prediction_models.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    model: Mapped[PredictionModel | None] = relationship()

    __table_args__ = (Index("ix_race_pred_lookup", "session_id", "lap_number", "created_at"),)


class RaceControlEvent(Base):
    __tablename__ = "race_control_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    lap_number: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (Index("ix_rc_lookup", "session_id", "created_at"),)
