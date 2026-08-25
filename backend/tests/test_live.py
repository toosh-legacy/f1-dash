"""Race-control gating (rule 4), broadcast fan-out, and the live loops."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.db import models as m
from app.db.models import RaceControlEventType as RC
from app.live import race_control as rc
from app.live.base import LiveLoop
from app.live.broadcast import Broadcaster, race_control, race_update
from app.live.qualifying_loop import detect_period


class TestRaceControlClassification:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("SAFETY CAR DEPLOYED", RC.SAFETY_CAR),
            ("VIRTUAL SAFETY CAR DEPLOYED", RC.VSC),
            ("RED FLAG", RC.RED_FLAG),
            ("YELLOW FLAG IN TRACK SECTOR 4", RC.YELLOW),
            ("GREEN LIGHT - PIT EXIT OPEN", RC.GREEN),
            ("TRACK CLEAR", RC.GREEN),
        ],
    )
    def test_messages_map_to_states(self, message, expected):
        assert rc.classify_message(message) is expected

    def test_vsc_is_not_misread_as_safety_car(self):
        assert rc.classify_message("VIRTUAL SAFETY CAR DEPLOYED") is RC.VSC

    def test_safety_car_ending_returns_to_green(self):
        assert rc.classify_message("SAFETY CAR IN THIS LAP") is RC.GREEN

    def test_unrelated_message_carries_no_state(self):
        assert rc.classify_message("CAR 44 TIME 1:32.100 DELETED - TRACK LIMITS") is None


class TestGating:
    def test_green_does_not_gate(self):
        assert not rc.GateState(RC.GREEN).is_gated

    @pytest.mark.parametrize("event", [RC.SAFETY_CAR, RC.VSC, RC.RED_FLAG, RC.YELLOW])
    def test_non_green_states_gate(self, event):
        state = rc.GateState(event)
        assert state.is_gated
        assert state.reason == event.value

    def test_state_defaults_to_green_with_no_events(self, db, seeded):
        assert rc.current_state(db, seeded["race"].id).event_type is RC.GREEN

    def test_latest_event_wins(self, db, seeded):
        session_id = seeded["race"].id
        rc.record_event(db, session_id, RC.SAFETY_CAR, 24)
        assert rc.current_state(db, session_id).is_gated
        rc.record_event(db, session_id, RC.GREEN, 28)
        assert not rc.current_state(db, session_id).is_gated

    def test_state_at_lap_replays_a_session(self, db, seeded):
        session_id = seeded["race"].id
        rc.record_event(db, session_id, RC.SAFETY_CAR, 24)
        rc.record_event(db, session_id, RC.GREEN, 28)
        assert rc.state_at_lap(db, session_id, 26).is_gated, "lap 26 sits inside the SC window"
        assert not rc.state_at_lap(db, session_id, 30).is_gated
        assert not rc.state_at_lap(db, session_id, 5).is_gated

    def test_gated_prediction_is_persisted_as_gated(self, db, seeded):
        prediction = m.RacePrediction(
            session_id=seeded["race"].id,
            driver_id="ABC",
            lap_number=24,
            predicted_finish_position=3.2,
            is_gated=True,
            gate_reason=RC.SAFETY_CAR.value,
        )
        db.add(prediction)
        db.flush()
        assert prediction.is_gated and prediction.gate_reason == "safety_car"


class TestMessageShapes:
    def test_race_update_matches_the_specification(self):
        message = race_update(23, [{"driver_id": "ABC"}], is_gated=False)
        assert message == {
            "type": "race_update",
            "lap_number": 23,
            "is_gated": False,
            "predictions": [{"driver_id": "ABC"}],
        }

    def test_race_control_message_shape(self):
        assert race_control("safety_car", 24) == {
            "type": "race_control",
            "event_type": "safety_car",
            "lap_number": 24,
        }


class TestBroadcast:
    @pytest.mark.asyncio
    async def test_subscriber_receives_published_message(self):
        broadcaster = Broadcaster()
        broadcaster.bind_loop()
        queue = await broadcaster.subscribe(1)
        await broadcaster.publish(1, {"type": "race_update"})
        assert (await asyncio.wait_for(queue.get(), 1))["type"] == "race_update"

    @pytest.mark.asyncio
    async def test_messages_are_scoped_per_session(self):
        broadcaster = Broadcaster()
        broadcaster.bind_loop()
        one = await broadcaster.subscribe(1)
        two = await broadcaster.subscribe(2)
        await broadcaster.publish(1, {"type": "race_update"})
        assert not two.qsize()
        assert one.qsize() == 1

    @pytest.mark.asyncio
    async def test_slow_subscriber_drops_oldest_instead_of_growing(self):
        from app.live.broadcast import QUEUE_MAXSIZE

        broadcaster = Broadcaster()
        broadcaster.bind_loop()
        queue = await broadcaster.subscribe(1)
        for i in range(QUEUE_MAXSIZE + 10):
            await broadcaster.publish(1, {"type": "race_update", "lap_number": i})
        assert queue.qsize() <= QUEUE_MAXSIZE
        assert queue.get_nowait()["lap_number"] > 0, "oldest messages should have been dropped"

    @pytest.mark.asyncio
    async def test_thread_can_publish_into_the_event_loop(self):
        """The reference pattern: background thread -> run_coroutine_threadsafe."""
        broadcaster = Broadcaster()
        broadcaster.bind_loop()
        queue = await broadcaster.subscribe(7)

        threading.Thread(
            target=lambda: broadcaster.publish_threadsafe(7, {"type": "race_control"}),
            daemon=True,
        ).start()

        message = await asyncio.wait_for(queue.get(), 2)
        assert message["type"] == "race_control"

    def test_publish_without_a_loop_is_a_noop(self):
        Broadcaster().publish_threadsafe(1, {"type": "race_update"})  # must not raise


class TestQualifyingPeriodDetection:
    def test_period_from_cars_running(self):
        assert detect_period(20) == "Q1"
        assert detect_period(15) == "Q2"
        assert detect_period(10) == "Q3"


class _CountingLoop(LiveLoop):
    kind = "test"

    def __init__(self):
        super().__init__(1, 99, poll_interval_s=0.01, client=_DummyClient())
        self.ticks = 0
        self.fail_next = False

    def poll_once(self):
        self.ticks += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("upstream hiccup")


class _DummyClient:
    def close(self):
        pass


class TestLoopResilience:
    def test_loop_runs_and_stops(self):
        loop = _CountingLoop().start()
        time.sleep(0.1)
        loop.stop()
        assert loop.ticks > 0
        assert not loop.is_running

    def test_a_failing_poll_does_not_kill_the_loop(self):
        loop = _CountingLoop().start()
        loop.fail_next = True
        time.sleep(0.2)
        ticks_after_failure = loop.ticks
        loop.stop()
        assert ticks_after_failure > 1, "loop should keep polling after an error"
        assert loop.status.last_error is not None
