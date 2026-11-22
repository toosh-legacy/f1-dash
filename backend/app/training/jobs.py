"""Background jobs: recorded in the database, run by a thread or by a worker.

Retraining, replay builds and prediction passes are batch work. A request may
*enqueue* one and must return immediately; it may never wait for one (rule 1).

A job is a row in ``jobs`` (:mod:`app.db.job_record`) naming a handler kind
and its JSON params -- not a closure -- because the process that queues it is
not always the one that runs it:

* ``JOB_RUNNER == "thread"`` (a laptop, a long-lived server): the row is run
  on this process's single worker thread, as before.
* ``JOB_RUNNER == "github"`` (a Vercel function, which cannot keep working
  after it has answered): the row is left queued and the GitHub Actions
  worker (``.github/workflows/worker.yml``) is dispatched to run it with
  ``python -m app.cli run-job <id>``.

Either way the runner writes progress and the outcome back to the row, and
``GET /jobs/{id}`` reads it from there, so the dashboard's polling does not
care which ran it.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select, update

from app.config import settings
from app.db.database import engine, session_scope
from app.db.job_record import JobRow
from app.live.broadcast import broadcaster, training_progress

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")

#: A job running longer than this is dead: the worker's own timeout is two
#: hours, and a thread that long has outlived any real job.
RUNNING_TIMEOUT = timedelta(hours=2)
#: A queued job nobody has started in this long never will be: the worker was
#: not dispatched, or failed before it reached the job. A thread runner works
#: through a queue in order, so it is given the same allowance as a run.
QUEUED_TIMEOUT_GITHUB = timedelta(minutes=30)
QUEUED_TIMEOUT_THREAD = timedelta(hours=2)

#: Progress is written at most this often; terminal states always are. A
#: replay build reports a stage per car, and a write each is wasted traffic
#: to a database that may be across the internet.
PROGRESS_WRITE_INTERVAL_S = 1.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive; they were written as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _jsonable(value: Any) -> Any:
    """A deep copy that a JSON column will accept.

    Results carry numpy scalars and datetimes now and then; failing a finished
    job at the last write over a type would throw the work away.
    """
    if value is None:
        return None
    return json.loads(json.dumps(value, default=str))


# --- The in-memory view of a job ---------------------------------------------


@dataclass
class JobRecord:
    """A job as the API reports it. Built from a row, or standalone for a
    reporter that only prints (the CLI's backfill)."""

    id: str
    kind: str
    session_id: int | None
    status: str = "queued"  # queued | running | succeeded | failed
    params: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    stages: list[str] = field(default_factory=list)
    #: The detail of the latest stage -- "car 7 of 22" while a replay's
    #: position feed comes in -- so a progress display can say how far along.
    detail: dict[str, Any] | None = None

    @classmethod
    def from_row(cls, row: JobRow) -> "JobRecord":
        return cls(
            id=row.id,
            kind=row.kind,
            session_id=row.session_id,
            status=row.status,
            params=dict(row.params or {}),
            created_at=_aware(row.created_at),
            started_at=_aware(row.started_at),
            finished_at=_aware(row.finished_at),
            result=row.result,
            error=row.error,
            stages=list(row.stages or []),
            detail=row.detail,
        )

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
            "detail": self.detail,
        }


class ProgressReporter:
    """Handed to a handler so it can report stages as it works.

    Stages are kept in memory and written to the job's row at most once a
    second (``persist``); a reporter with ``persist=False`` only logs and
    broadcasts, for callers with no row behind them.
    """

    def __init__(self, record: JobRecord, persist: bool = False) -> None:
        self.record = record
        self.persist = persist
        self._last_write = 0.0

    def __call__(self, stage: str, detail: dict[str, Any] | None = None) -> None:
        self.record.stages.append(stage)
        self.record.detail = _jsonable(detail)
        log.info("[job %s] %s %s", self.record.id, stage, detail or "")
        if self.persist and time.monotonic() - self._last_write >= PROGRESS_WRITE_INTERVAL_S:
            self.flush()
        if self.record.session_id is not None:
            broadcaster.publish_threadsafe(
                self.record.session_id,
                training_progress(stage, {"job_id": self.record.id, **(detail or {})}),
            )

    def flush(self) -> None:
        """Write the stages so far. A failed write never fails the job."""
        self._last_write = time.monotonic()
        try:
            _write(self.record.id, stages=list(self.record.stages), detail=self.record.detail)
        except Exception:  # noqa: BLE001 - progress is a nicety, the work is not
            log.warning("[job %s] could not record progress", self.record.id, exc_info=True)


# --- The table ----------------------------------------------------------------

_table_ready = False
_table_lock = threading.Lock()


def _ensure_table() -> None:
    """Create ``jobs`` on first use.

    ``init_db`` creates it too; this covers a process that reaches jobs
    first (a worker, a script, a test) without making every caller remember.
    """
    global _table_ready
    if _table_ready:
        return
    with _table_lock:
        if not _table_ready:
            JobRow.__table__.create(bind=engine, checkfirst=True)
            _table_ready = True


def _write(job_id: str, **values: Any) -> None:
    _ensure_table()
    with session_scope() as db:
        db.execute(update(JobRow).where(JobRow.id == job_id).values(**values))


def _insert(kind: str, params: dict[str, Any], session_id: int | None, **values: Any) -> JobRecord:
    _ensure_table()
    row = JobRow(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        params=_jsonable(params) or {},
        session_id=session_id,
        status=values.pop("status", "queued"),
        stages=[],
        created_at=_now(),
        **values,
    )
    with session_scope() as db:
        db.add(row)
        db.flush()
        return JobRecord.from_row(row)


def _expire_if_stale(row: JobRow, now: datetime) -> bool:
    """Mark a job that can no longer finish as failed. Returns whether it was.

    Nothing else would: a worker that never started, or a process that died
    mid-job, leaves its row saying "queued" or "running" for ever, and a
    dashboard polling it would wait for ever too.
    """
    if row.status == "running":
        started = _aware(row.started_at) or _aware(row.created_at)
        if started and now - started > RUNNING_TIMEOUT:
            row.error = (
                f"timed out: running for over {int(RUNNING_TIMEOUT.total_seconds() // 3600)} h "
                "without finishing (the worker was stopped or crashed)"
            )
        else:
            return False
    elif row.status == "queued":
        limit = QUEUED_TIMEOUT_GITHUB if settings.JOB_RUNNER == "github" else QUEUED_TIMEOUT_THREAD
        created = _aware(row.created_at)
        if created and now - created > limit:
            row.error = (
                f"worker never picked it up: queued for over {int(limit.total_seconds() // 60)} min. "
                + (
                    "Check the GitHub Actions worker runs for this repository."
                    if settings.JOB_RUNNER == "github"
                    else "The process that queued it probably restarted."
                )
            )
        else:
            return False
    else:
        return False
    row.status = "failed"
    row.finished_at = now
    return True


def get(job_id: str) -> JobRecord | None:
    """One job, read from the database, with a dead one reported as failed."""
    _ensure_table()
    with session_scope() as db:
        row = db.get(JobRow, job_id)
        if row is None:
            return None
        _expire_if_stale(row, _now())
        return JobRecord.from_row(row)


def recent(limit: int = 20) -> list[JobRecord]:
    """The newest jobs first."""
    _ensure_table()
    now = _now()
    with session_scope() as db:
        rows = db.scalars(
            select(JobRow).order_by(JobRow.created_at.desc()).limit(limit)
        ).all()
        for row in rows:
            _expire_if_stale(row, now)
        return [JobRecord.from_row(row) for row in rows]


def _dedupe_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    from app.training.handlers import DEDUPE_KEYS

    keys = DEDUPE_KEYS.get(kind)
    return {k: params.get(k) for k in keys} if keys else params


def active(kind: str, params: dict[str, Any]) -> JobRecord | None:
    """The queued or running job already doing this work, if there is one."""
    _ensure_table()
    wanted = _dedupe_params(kind, _jsonable(params) or {})
    now = _now()
    with session_scope() as db:
        rows = db.scalars(
            select(JobRow)
            .where(JobRow.kind == kind, JobRow.status.in_(ACTIVE))
            .order_by(JobRow.created_at.desc())
        ).all()
        for row in rows:
            if _expire_if_stale(row, now):
                continue
            if _dedupe_params(kind, dict(row.params or {})) == wanted:
                return JobRecord.from_row(row)
    return None


# --- Submitting ---------------------------------------------------------------

#: One worker: retraining is CPU-heavy and jobs are naturally serialised per
#: session anyway. Concurrency here would just contend for cores with inference.
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
WORKERS = 1


def _pool() -> ThreadPoolExecutor:
    """The thread pool, made again if it was shut down (an app restarted in
    the same process, as the tests do)."""
    global _executor
    with _executor_lock:
        if _executor is None or getattr(_executor, "_shutdown", False):
            _executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="jobs")
        return _executor


def submit(kind: str, params: dict[str, Any] | None = None, session_id: int | None = None) -> dict[str, Any]:
    """Record a job and start it running somewhere. Returns at once (rule 1).

    The same work already queued or running is returned rather than started
    twice. Under the github runner a dispatch that cannot be made fails the
    job straight away, with what to configure, instead of leaving it queued.
    """
    from app.training.handlers import HANDLERS

    if kind not in HANDLERS:
        raise ValueError(f"no handler for job kind {kind!r}")
    params = params or {}
    existing = active(kind, params)
    if existing is not None:
        return existing.as_dict()

    record = _insert(kind, params, session_id)
    if settings.JOB_RUNNER == "github":
        error = _dispatch(record.id)
        if error is not None:
            log.error("[job %s] %s", record.id, error)
            _write(record.id, status="failed", error=error, finished_at=_now())
            record.status, record.error, record.finished_at = "failed", error, _now()
    else:
        _pool().submit(execute, record.id)
    return record.as_dict()


def record_done(kind: str, params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """A job that had nothing to do (the replay was already built), recorded
    as succeeded so a caller polling its id finds it finished."""
    now = _now()
    record = _insert(
        kind, params, None, status="succeeded", started_at=now, finished_at=now,
        result=_jsonable(result),
    )
    return record.as_dict()


def _dispatch(job_id: str) -> str | None:
    """Start the GitHub Actions worker on this job. Returns an error, or None."""
    if not settings.GITHUB_DISPATCH_TOKEN:
        return (
            "no worker configured: F1_JOB_RUNNER is 'github' but GITHUB_DISPATCH_TOKEN is not set. "
            f"Create a fine-grained token for {settings.GITHUB_REPO} with Actions: read and write "
            "and set it as GITHUB_DISPATCH_TOKEN, or set F1_JOB_RUNNER=thread to run jobs in this process."
        )
    url = (
        f"https://api.github.com/repos/{settings.GITHUB_REPO}"
        f"/actions/workflows/{settings.GITHUB_WORKFLOW}/dispatches"
    )
    try:
        response = httpx.post(
            url,
            json={"ref": settings.GITHUB_REF, "inputs": {"job_id": job_id}},
            headers={
                "Authorization": f"Bearer {settings.GITHUB_DISPATCH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        return f"could not reach GitHub to start the worker: {type(exc).__name__}: {exc}"
    if response.status_code >= 300:
        return (
            f"GitHub refused to start the worker ({response.status_code}): {response.text[:300]}. "
            f"Check that {settings.GITHUB_WORKFLOW} exists on {settings.GITHUB_REF} of "
            f"{settings.GITHUB_REPO} and that GITHUB_DISPATCH_TOKEN may run Actions there."
        )
    return None


# --- Running ------------------------------------------------------------------


def _claim(job_id: str, force: bool = False) -> bool:
    """Move a queued job to running; False if someone else already has it.

    A conditional update rather than read-then-write, so two workers draining
    the same queue never both run one job.
    """
    _ensure_table()
    allowed = ("queued", "running", "failed", "succeeded") if force else ("queued",)
    with session_scope() as db:
        claimed = db.execute(
            update(JobRow)
            .where(JobRow.id == job_id, JobRow.status.in_(allowed))
            .values(status="running", started_at=_now(), finished_at=None, error=None,
                    result=None, stages=[], detail=None)
        )
        return bool(claimed.rowcount)


def execute(job_id: str, force: bool = False) -> JobRecord | None:
    """Run one job to its end and record the outcome. Blocking.

    Used by the thread runner and by ``app.cli run-job``. Returns the final
    record, or None if the job is missing or was not queued (``force`` runs
    it whatever its status).
    """
    from app.training.handlers import HANDLERS

    if not _claim(job_id, force=force):
        log.warning("[job %s] not run: missing, or not queued", job_id)
        return None
    record = get(job_id)
    if record is None:   # pragma: no cover - deleted between claim and read
        return None
    reporter = ProgressReporter(record, persist=True)
    reporter("started", {"kind": record.kind})
    try:
        handler = HANDLERS.get(record.kind)
        if handler is None:
            raise LookupError(f"no handler for job kind {record.kind!r}")
        record.result = _jsonable(handler(record.params, reporter))
        record.status = "succeeded"
        reporter("finished", record.result)
    except Exception as exc:  # a failed job must never take its runner down
        record.status = "failed"
        record.error = f"{type(exc).__name__}: {exc}"
        log.exception("[job %s] %s failed", record.id, record.kind)
        reporter("failed", {"error": record.error})
    record.finished_at = _now()
    try:
        _write(
            record.id,
            status=record.status,
            result=record.result,
            error=record.error,
            stages=list(record.stages),
            detail=record.detail,
            finished_at=record.finished_at,
        )
    except Exception:  # noqa: BLE001
        log.exception("[job %s] could not record the outcome", record.id)
        raise
    return record


def next_queued() -> str | None:
    """The oldest queued job's id, for a worker draining the queue."""
    _ensure_table()
    with session_scope() as db:
        return db.scalars(
            select(JobRow.id).where(JobRow.status == "queued").order_by(JobRow.created_at).limit(1)
        ).first()


def shutdown(wait: bool = False) -> None:
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait, cancel_futures=not wait)
            _executor = None


# --- Reporting ----------------------------------------------------------------


def stats() -> dict[str, Any]:
    """What the background runner is doing -- reported by ``/health``.

    Rule 1 says a request path never trains. What makes that checkable from
    outside is seeing the training work queued somewhere else, and how deep
    that queue is.
    """
    try:
        latest = recent(10)
    except Exception as exc:  # noqa: BLE001 - health must answer even so
        return {"runner": settings.JOB_RUNNER, "error": f"{type(exc).__name__}: {exc}"}
    finished = [r for r in latest if r.finished_at]
    return {
        "runner": settings.JOB_RUNNER,
        "pending": sum(1 for r in latest if r.status in ACTIVE),
        "running": sum(1 for r in latest if r.status == "running"),
        "workers": WORKERS if settings.JOB_RUNNER == "thread" else None,
        "recent": [r.as_dict() for r in latest[:5]],
        "last_finished": finished[0].as_dict() if finished else None,
    }


def pending_count() -> int:
    try:
        return sum(1 for r in recent(50) if r.status in ACTIVE)
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "JobRecord",
    "ProgressReporter",
    "active",
    "execute",
    "get",
    "next_queued",
    "pending_count",
    "recent",
    "record_done",
    "shutdown",
    "stats",
    "submit",
]
