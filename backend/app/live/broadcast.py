"""WebSocket fan-out: one subscriber queue per session id.

The pattern (from the reference implementation's ``training.py``): background
threads -- the OpenF1 polling loops and the retraining jobs -- never touch the
event loop directly. They call :meth:`Broadcaster.publish_threadsafe`, which
hops back into the API's loop via ``asyncio.run_coroutine_threadsafe`` and puts
the message on each subscriber's queue. Each WebSocket connection drains its own
queue, so one slow client cannot stall the loop or another client.

Queues are bounded: if a client stops draining, its oldest messages are dropped
rather than growing memory without limit. Live predictions are only interesting
while they are current, so dropping stale ones is the correct failure mode.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from typing import Any

from app.live import race_control as rc

log = logging.getLogger(__name__)

QUEUE_MAXSIZE = 64


class Broadcaster:
    """Session-scoped pub/sub over asyncio queues."""

    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Capture the API event loop at startup; threads publish into it."""
        self._loop = loop or asyncio.get_running_loop()

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    # -- subscription ------------------------------------------------------
    async def subscribe(self, session_id: int) -> asyncio.Queue:
        async with self._lock:
            queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
            self._subscribers[session_id].add(queue)
            log.debug("subscriber added for session %s (%s total)",
                      session_id, len(self._subscribers[session_id]))
            return queue

    async def unsubscribe(self, session_id: int, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers[session_id].discard(queue)
            if not self._subscribers[session_id]:
                self._subscribers.pop(session_id, None)

    def subscriber_count(self, session_id: int) -> int:
        return len(self._subscribers.get(session_id, ()))

    # -- publishing --------------------------------------------------------
    async def publish(self, session_id: int, message: dict[str, Any]) -> int:
        """Publish from inside the event loop. Returns the number of receivers."""
        delivered = 0
        for queue in list(self._subscribers.get(session_id, ())):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()  # drop the oldest; keep the newest state
            with suppress(asyncio.QueueFull):
                queue.put_nowait(message)
                delivered += 1
        return delivered

    def publish_threadsafe(self, session_id: int, message: dict[str, Any]) -> None:
        """Publish from a background thread (polling loop, training job).

        Safe to call before any client has connected, and safe to call when the
        loop is gone -- both are no-ops rather than errors, because a live loop
        must not die because nobody is watching.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            log.debug("no event loop bound; dropping %s for session %s",
                      message.get("type"), session_id)
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self.publish(session_id, message), loop)
        except RuntimeError:  # loop shutting down mid-publish
            log.debug("event loop unavailable; dropping message for session %s", session_id)
            return
        # Surface exceptions from the coroutine without blocking the caller.
        future.add_done_callback(_log_publish_failure)


def _log_publish_failure(future: Any) -> None:
    with suppress(Exception):
        exc = future.exception()
        if exc is not None:
            log.warning("broadcast failed: %s", exc)


#: Process-wide broadcaster. One API process owns one event loop and one set of
#: live loops, so a module-level instance is the right scope.
broadcaster = Broadcaster()


# -- message constructors ----------------------------------------------------
# Shapes match the guide's WebSocket specification exactly; building them here
# keeps the contract in one place instead of spread across the loops.


def qualifying_update(period: str, predictions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "qualifying_update", "period": period, "predictions": predictions}


def race_update(
    lap_number: int,
    predictions: list[dict[str, Any]],
    *,
    is_gated: bool,
    order: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One lap of a live race.

    ``order`` carries the running order in the same shape a replay bundle uses
    for ``laps[n]`` -- number, position, gap, compound, tyre age, stops. A live
    race and a replayed one are the same thing to a viewer, so sending the same
    shape means the dashboard renders both through one path instead of keeping
    a second, live-only one that is exercised far less often.
    """
    return {
        "type": "race_update",
        "lap_number": lap_number,
        "is_gated": is_gated,
        "predictions": predictions,
        "order": order or [],
    }


def race_control(
    event_type: str,
    lap_number: int | None,
    *,
    message: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    """A call from race control.

    Carries the message itself and its severity, so a live client can decide
    what to surface on exactly the same rule a replay client uses.
    """
    return {
        "type": "race_control",
        "event_type": event_type,
        "lap_number": lap_number,
        "message": message,
        "severity": severity or rc.severity_of(message, None),
    }


def training_progress(stage: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Progress from the offline retraining job -- never a prediction path."""
    return {"type": "training_progress", "stage": stage, "detail": detail or {}}
