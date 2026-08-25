"""Live qualifying inference: one update per period boundary (Q1 -> Q2 -> Q3).

Cadence is the period, not the lap: qualifying predictions only become newly
informative when a period ends and the field's real times for it are known.

The loop re-runs the *already trained* active models. It never trains.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.data.openf1_client import OpenF1Client
from app.db import models as m
from app.db.database import session_scope
from app.features.builder import FeatureBuilder, live_state_from_openf1
from app.live.base import LiveLoop
from app.live.broadcast import qualifying_update
from app.models import registry
from app.models.qualifying_model import PERIOD_CUTOFF

log = logging.getLogger(__name__)

PERIODS = ("Q1", "Q2", "Q3")


def detect_period(openf1_drivers_running: int, elapsed_fraction: float | None = None) -> str:
    """Infer the current period from how many cars are still on track.

    OpenF1 does not expose the period directly, but the field size does: 20 cars
    in Q1, 15 in Q2, 10 in Q3 under the standard format.
    """
    if openf1_drivers_running <= PERIOD_CUTOFF["Q2"]:
        return "Q3"
    if openf1_drivers_running <= PERIOD_CUTOFF["Q1"]:
        return "Q2"
    return "Q1"


class QualifyingLoop(LiveLoop):
    kind = "qualifying"

    def __init__(
        self,
        session_id: int,
        session_key: int,
        *,
        client: OpenF1Client | None = None,
        poll_interval_s: float | None = None,
    ) -> None:
        super().__init__(
            session_id,
            session_key,
            poll_interval_s=poll_interval_s or settings.QUALIFYING_POLL_INTERVAL_S,
            client=client,
        )
        self._last_period: str | None = None

    def poll_once(self) -> None:
        states = self.client.live_state(self.session_key)
        if not states:
            return

        active_cars = sum(1 for s in states.values() if s.last_lap_time_s is not None)
        period = detect_period(active_cars or len(states))
        self.status.last_context = period

        # One broadcast per period boundary -- not per poll.
        if period == self._last_period:
            return
        log.info("session %s entered %s (%s cars running)", self.session_id, period, active_cars)
        self._last_period = period
        predictions = self.predict_period(period, states)
        if predictions:
            self.publish(qualifying_update(period, predictions))

    def predict_period(self, period: str, states: dict[int, Any]) -> list[dict[str, Any]]:
        """Run the active qualifying models for every car in this period."""
        with session_scope() as db:
            session = db.get(m.Session, self.session_id)
            if session is None:
                return []
            session.status = m.SessionStatus.LIVE.value

            time_model = registry.load_active(
                db, m.ModelType.QUALIFYING_TIME.value, regs_regime=session.regs_regime
            )
            adv_model = registry.load_active(
                db, m.ModelType.QUALIFYING_ADVANCEMENT.value, regs_regime=session.regs_regime
            )
            if time_model is None and adv_model is None:
                log.warning("no active qualifying model; skipping live inference")
                return []

            builder = FeatureBuilder(db, season=session.year, as_of=session.start_time)
            drivers_by_number = {
                d.driver_number: d for d in db.scalars(select(m.Driver)).all() if d.driver_number
            }

            rows: list[dict[str, float]] = []
            drivers: list[m.Driver] = []
            for number, state in states.items():
                driver = drivers_by_number.get(number)
                if driver is None:
                    continue
                vector = builder.build_and_store(
                    session,
                    driver,
                    period,
                    live=live_state_from_openf1(state),
                )
                rows.append(vector.values)
                drivers.append(driver)

            if not rows:
                return []

            times = time_model[0].predict(rows).tolist() if time_model else [None] * len(rows)
            # Q3 has nothing to advance to, so the probability stays null there.
            if adv_model and period != "Q3":
                probabilities = adv_model[0].advancement_probability(rows)
            else:
                probabilities = [None] * len(rows)

            model_id = (time_model or adv_model)[1].id
            predictions: list[dict[str, Any]] = []
            for driver, predicted_time, probability in zip(drivers, times, probabilities):
                db.add(
                    m.QualifyingPrediction(
                        session_id=session.id,
                        driver_id=driver.id,
                        period=period,
                        predicted_time_s=predicted_time,
                        advancement_probability=probability,
                        model_version=model_id,
                    )
                )
                predictions.append(
                    {
                        "driver_id": driver.id,
                        "driver_name": driver.name,
                        "team": driver.team.name if driver.team else None,
                        "predicted_time_s": round(float(predicted_time), 3) if predicted_time is not None else None,
                        "advancement_probability": round(float(probability), 4) if probability is not None else None,
                    }
                )
            predictions.sort(key=lambda p: (p["predicted_time_s"] is None, p["predicted_time_s"]))
            return predictions


def start(session_id: int, session_key: int, **kwargs: Any) -> QualifyingLoop:
    from app.live.base import loops

    return loops.start(QualifyingLoop(session_id, session_key, **kwargs))  # type: ignore[return-value]
