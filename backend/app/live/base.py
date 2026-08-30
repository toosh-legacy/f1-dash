"""Shared machinery for the live inference loops.

Each loop is a daemon thread that polls OpenF1, decides whether anything worth
predicting has changed (a new lap, a new qualifying period), runs the *already
trained* active model, and broadcasts. No loop ever trains (rule 1).

Failure policy: a poll that raises is logged and retried with backoff. A live
loop must survive a flaky upstream -- an F1 session is 90 minutes long and the
API will hiccup.
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.data.openf1_client import OpenF1Client, OpenF1Error
from app.live.broadcast import broadcaster

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 60.0


@dataclass
class LoopStatus:
    session_id: int
    session_key: int | None
    kind: str
    running: bool = False
    polls: int = 0
    updates_published: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    last_context: str | None = None
    started_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    #: When the last poll finished, and when one last produced an update.
    #: The interesting failure is not a loop that has stopped -- that is
    #: obvious -- but one that is still turning and has not heard anything
    #: useful for ten minutes. Age since the last poll is what shows that.
    last_poll_at: float | None = None
    last_publish_at: float | None = None

    #: How long recent polls took, in milliseconds. The whole claim of this
    #: system is that live inference never waits on training, so the number
    #: that has to be visible is how long a live tick actually takes.
    poll_ms: deque[float] = field(default_factory=lambda: deque(maxlen=120))

    def record_poll(self, elapsed_ms: float) -> None:
        self.poll_ms.append(elapsed_ms)
        self.last_poll_at = time.monotonic()

    def _percentiles(self) -> dict[str, float | None]:
        if not self.poll_ms:
            return {"p50_ms": None, "p95_ms": None, "max_ms": None}
        ordered = sorted(self.poll_ms)
        index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
        return {
            "p50_ms": round(statistics.median(ordered), 1),
            "p95_ms": round(ordered[index], 1),
            "max_ms": round(ordered[-1], 1),
        }

    def _age(self, stamp: float | None) -> float | None:
        return round(time.monotonic() - stamp, 1) if stamp else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_key": self.session_key,
            "kind": self.kind,
            "running": self.running,
            "polls": self.polls,
            "updates_published": self.updates_published,
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error,
            "last_context": self.last_context,
            "uptime_s": round(time.monotonic() - self.started_at, 1) if self.started_at else None,
            "last_poll_age_s": self._age(self.last_poll_at),
            "last_publish_age_s": self._age(self.last_publish_at),
            "poll": self._percentiles(),
            **self.extra,
        }


class LiveLoop(ABC):
    """Polling thread with a stop flag, backoff, and a status record."""

    kind = "live"

    def __init__(
        self,
        session_id: int,
        session_key: int,
        *,
        poll_interval_s: float,
        client: OpenF1Client | None = None,
    ) -> None:
        self.session_id = session_id
        self.session_key = session_key
        self.poll_interval_s = poll_interval_s
        self.client = client or OpenF1Client()
        self.status = LoopStatus(session_id=session_id, session_key=session_key, kind=self.kind)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "LiveLoop":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"{self.kind}-{self.session_id}", daemon=True
        )
        self.status.running = True
        self.status.started_at = time.monotonic()
        self._thread.start()
        log.info("%s loop started for session %s (openf1 key %s)",
                 self.kind, self.session_id, self.session_key)
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.status.running = False
        self.client.close()
        log.info("%s loop stopped for session %s", self.kind, self.session_id)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- loop --------------------------------------------------------------
    def _run(self) -> None:
        backoff = self.poll_interval_s
        while not self._stop.is_set():
            try:
                self.status.polls += 1
                started = time.perf_counter()
                self.poll_once()
                self.status.record_poll((time.perf_counter() - started) * 1000.0)
                self.status.consecutive_errors = 0
                backoff = self.poll_interval_s
            except OpenF1Error as exc:
                backoff = self._handle_error("openf1 unavailable", exc, backoff)
            except Exception as exc:  # never let a bad tick kill the session
                backoff = self._handle_error("live loop error", exc, backoff)
            self._stop.wait(backoff)
        self.status.running = False

    def _handle_error(self, label: str, exc: Exception, backoff: float) -> float:
        self.status.consecutive_errors += 1
        self.status.last_error = f"{type(exc).__name__}: {exc}"
        log.warning("[%s session %s] %s (attempt %s): %s",
                    self.kind, self.session_id, label, self.status.consecutive_errors, exc)
        return min(backoff * 2, MAX_BACKOFF_S)

    def publish(self, message: dict[str, Any]) -> None:
        broadcaster.publish_threadsafe(self.session_id, message)
        self.status.updates_published += 1
        self.status.last_publish_at = time.monotonic()

    @abstractmethod
    def poll_once(self) -> None:
        """One polling tick. Implementations must be idempotent."""


class LoopManager:
    """Registry of running loops, one per session."""

    def __init__(self) -> None:
        self._loops: dict[int, LiveLoop] = {}
        self._lock = threading.Lock()

    def start(self, loop: LiveLoop) -> LiveLoop:
        with self._lock:
            existing = self._loops.get(loop.session_id)
            if existing and existing.is_running:
                return existing
            self._loops[loop.session_id] = loop
        return loop.start()

    def stop(self, session_id: int) -> bool:
        with self._lock:
            loop = self._loops.pop(session_id, None)
        if loop is None:
            return False
        loop.stop()
        return True

    def get(self, session_id: int) -> LiveLoop | None:
        with self._lock:
            return self._loops.get(session_id)

    def statuses(self) -> list[dict[str, Any]]:
        with self._lock:
            return [loop.status.as_dict() for loop in self._loops.values()]

    def stop_all(self) -> None:
        with self._lock:
            loops = list(self._loops.values())
            self._loops.clear()
        for loop in loops:
            loop.stop()


loops = LoopManager()
