"""Live race inference: one update per lap, plus immediate race-control pushes.

Two things happen on every tick:

1. **Race control is checked first, on its own faster cadence.** A safety car is
   pushed to clients the moment it is seen -- waiting for the next lap update to
   report it would be exactly the failure rule 4 exists to prevent.
2. **A new lap triggers inference.** Predictions during a non-green state are
   still produced but flagged ``is_gated`` so the dashboard can present them as
   low-confidence rather than as a confident number that assumes green-flag
   racing.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.data.openf1_client import LiveDriverState, OpenF1Client
from app.db import models as m
from app.db.database import session_scope
from app.features.builder import FeatureBuilder, live_state_from_openf1
from app.live import race_control as rc
from app.live.base import LiveLoop
from app.live.broadcast import race_control as race_control_message
from app.live.broadcast import race_update
from app.models import registry

log = logging.getLogger(__name__)


class RaceLoop(LiveLoop):
    kind = "race"

    def __init__(
        self,
        session_id: int,
        session_key: int,
        *,
        client: OpenF1Client | None = None,
        poll_interval_s: float | None = None,
        total_laps: int | None = None,
    ) -> None:
        super().__init__(
            session_id,
            session_key,
            poll_interval_s=poll_interval_s or settings.RACE_POLL_INTERVAL_S,
            client=client,
        )
        self.total_laps = total_laps
        self._last_lap: int | None = None
        self._last_rc_state: rc.RC | None = None
        self._seen_rc_messages: set[str] = set()

    def poll_once(self) -> None:
        # 1. Race control first, and pushed immediately on change.
        state = self.check_race_control()

        # 2. Then per-lap inference.
        states = self.client.live_state(self.session_key)
        if not states:
            return
        lap = max((s.lap_number or 0) for s in states.values()) or None
        if lap is None or lap == self._last_lap:
            return
        self._last_lap = lap
        self.status.last_context = f"lap_{lap}"
        predictions = self.predict_lap(lap, states, state)
        if predictions:
            self.publish(race_update(lap, predictions, is_gated=state.is_gated))

    # -- race control ------------------------------------------------------
    def check_race_control(self) -> rc.GateState:
        """Poll race-control messages; record and push any state change (M6)."""
        messages = self.client.race_control(self.session_key)
        state = rc.GateState() if self._last_rc_state is None else rc.GateState(self._last_rc_state, self._last_lap)

        with session_scope() as db:
            if self._last_rc_state is None:
                state = rc.current_state(db, self.session_id)
                self._last_rc_state = state.event_type

            for message in messages:
                key = f"{message.get('date')}|{message.get('message')}"
                if key in self._seen_rc_messages:
                    continue
                self._seen_rc_messages.add(key)

                event_type = rc.classify_openf1_message(message)
                if event_type is None or event_type == self._last_rc_state:
                    continue

                lap = message.get("lap_number") or self._last_lap
                rc.record_event(db, self.session_id, event_type, lap, str(message.get("message", "")))
                self._last_rc_state = event_type
                state = rc.GateState(event_type=event_type, lap_number=lap)
                log.info("session %s race control -> %s (lap %s)", self.session_id, event_type.value, lap)
                # Immediate push; do not wait for the next lap update.
                self.publish(race_control_message(event_type.value, lap))

        return state

    # -- inference ---------------------------------------------------------
    def predict_lap(
        self, lap: int, states: dict[int, LiveDriverState], gate: rc.GateState
    ) -> list[dict[str, Any]]:
        with session_scope() as db:
            session = db.get(m.Session, self.session_id)
            if session is None:
                return []
            session.status = m.SessionStatus.LIVE.value

            finish_model = registry.load_active(
                db, m.ModelType.RACE_FINISH_POSITION.value, regs_regime=session.regs_regime
            )
            strategy_model = registry.load_active(
                db, m.ModelType.RACE_STRATEGY.value, regs_regime=session.regs_regime
            )
            if finish_model is None and strategy_model is None:
                log.warning("no active race model; skipping live inference")
                return []

            builder = FeatureBuilder(db, season=session.year)
            drivers_by_number = {
                d.driver_number: d for d in db.scalars(select(m.Driver)).all() if d.driver_number
            }
            gaps_behind = _gaps_behind(states)

            rows: list[dict[str, float]] = []
            drivers: list[m.Driver] = []
            for number, state in states.items():
                driver = drivers_by_number.get(number)
                if driver is None:
                    continue
                live = live_state_from_openf1(
                    state,
                    total_laps=self.total_laps,
                    gap_behind_s=gaps_behind.get(number),
                    safety_car=gate.event_type is rc.RC.SAFETY_CAR,
                    vsc=gate.event_type is rc.RC.VSC,
                    red_flag=gate.event_type is rc.RC.RED_FLAG,
                )
                vector = builder.build_and_store(session, driver, f"lap_{lap}", live=live)
                rows.append(vector.values)
                drivers.append(driver)

            if not rows:
                return []

            positions = finish_model[0].predict(rows).tolist() if finish_model else [None] * len(rows)
            strategies = (
                strategy_model[0].strategy_probabilities(rows)
                if strategy_model
                else [None] * len(rows)
            )
            model_id = (finish_model or strategy_model)[1].id

            predictions: list[dict[str, Any]] = []
            for driver, position, strategy in zip(drivers, positions, strategies):
                db.add(
                    m.RacePrediction(
                        session_id=session.id,
                        driver_id=driver.id,
                        lap_number=lap,
                        predicted_finish_position=position,
                        strategy_probabilities=strategy,
                        is_gated=gate.is_gated,
                        gate_reason=gate.reason,
                        model_version=model_id,
                    )
                )
                predictions.append(
                    {
                        "driver_id": driver.id,
                        "driver_name": driver.name,
                        "team": driver.team.name if driver.team else None,
                        "predicted_finish_position": round(float(position), 2) if position is not None else None,
                        "strategy_probabilities": strategy,
                    }
                )
            predictions.sort(
                key=lambda p: (
                    p["predicted_finish_position"] is None,
                    p["predicted_finish_position"],
                )
            )
            return predictions


def _gaps_behind(states: dict[int, LiveDriverState]) -> dict[int, float]:
    """Interval to the car *behind*, derived from the field's interval-ahead values."""
    ordered = sorted(
        (s for s in states.values() if s.position is not None), key=lambda s: s.position or 0
    )
    gaps: dict[int, float] = {}
    for ahead, behind in zip(ordered, ordered[1:]):
        if behind.interval_s is not None:
            gaps[ahead.driver_number] = behind.interval_s
    return gaps


def start(session_id: int, session_key: int, **kwargs: Any) -> RaceLoop:
    from app.live.base import loops

    return loops.start(RaceLoop(session_id, session_key, **kwargs))  # type: ignore[return-value]
