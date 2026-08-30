"""Background job runner for offline retraining (rule 1).

Retraining is a batch job. A live request may *enqueue* one and must return
immediately; it may never wait for one. This module owns the thread pool that
guarantees that, and reports progress back into the event loop through the
broadcaster -- the same thread-to-loop crossing the reference implementation
uses for training checkpoints.
"""
from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.live.broadcast import broadcaster, training_progress

log = logging.getLogger(__name__)

#: One worker: retraining is CPU-heavy and jobs are naturally serialised per
#: session anyway. Concurrency here would just contend for cores with inference.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="retrain")


@dataclass
class JobRecord:
    id: str
    kind: str
    session_id: int | None
    status: str = "queued"  # queued | running | succeeded | failed
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    stages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "session_id": self.session_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "result": self.result,
            "error": self.error,
            "stages": self.stages,
        }


class JobRegistry:
    """In-memory record of recent jobs, so the API can report status."""

    def __init__(self, keep: int = 50) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._keep = keep

    def create(self, kind: str, session_id: int | None) -> JobRecord:
        record = JobRecord(id=uuid.uuid4().hex[:12], kind=kind, session_id=session_id)
        with self._lock:
            self._jobs[record.id] = record
            self._order.append(record.id)
            while len(self._order) > self._keep:
                self._jobs.pop(self._order.pop(0), None)
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[JobRecord]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order[-limit:]) if i in self._jobs]


jobs = JobRegistry()


class ProgressReporter:
    """Handed to a training function so it can report stages as it works."""

    def __init__(self, record: JobRecord) -> None:
        self.record = record

    def __call__(self, stage: str, detail: dict[str, Any] | None = None) -> None:
        self.record.stages.append(stage)
        log.info("[job %s] %s %s", self.record.id, stage, detail or "")
        if self.record.session_id is not None:
            broadcaster.publish_threadsafe(
                self.record.session_id,
                training_progress(stage, {"job_id": self.record.id, **(detail or {})}),
            )


def submit(
    kind: str,
    session_id: int | None,
    fn: Callable[[ProgressReporter], dict[str, Any]],
) -> JobRecord:
    """Queue a training job and return its record immediately (rule 1)."""
    record = jobs.create(kind, session_id)
    _executor.submit(_run, record, fn)
    return record


def _run(record: JobRecord, fn: Callable[[ProgressReporter], dict[str, Any]]) -> None:
    record.status = "running"
    record.started_at = datetime.now(timezone.utc)
    reporter = ProgressReporter(record)
    reporter("started", {"kind": record.kind})
    try:
        record.result = fn(reporter)
        record.status = "succeeded"
        reporter("finished", record.result)
    except Exception as exc:  # a failed retrain must never take the API down
        record.status = "failed"
        record.error = f"{type(exc).__name__}: {exc}"
        log.exception("[job %s] retraining failed", record.id)
        reporter("failed", {"error": record.error})
    finally:
        record.finished_at = datetime.now(timezone.utc)


def shutdown(wait: bool = False) -> None:
    _executor.shutdown(wait=wait, cancel_futures=not wait)


def stats() -> dict[str, Any]:
    """What the background pool is doing -- reported by ``/health``.

    Rule 1 says a request path never trains. What makes that checkable from
    outside is seeing the training work queued somewhere else, and how deep
    that queue is.
    """
    recent = jobs.recent(10)
    running = [r for r in recent if r.status == "running"]
    finished = [r for r in recent if r.finished_at]
    return {
        "pending": pending_count(),
        "running": len(running),
        "workers": _executor._max_workers,
        "recent": [r.as_dict() for r in recent[:5]],
        "last_finished": finished[0].as_dict() if finished else None,
    }


def pending_count() -> int:
    return sum(1 for j in jobs.recent(50) if j.status in {"queued", "running"})


__all__ = [
    "Future",
    "JobRecord",
    "ProgressReporter",
    "jobs",
    "pending_count",
    "shutdown",
    "submit",
]
