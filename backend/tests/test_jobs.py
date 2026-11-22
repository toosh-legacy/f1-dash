"""Background jobs: recorded in the database, run by a thread or dispatched."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import cli
from app.config import settings
from app.db.database import session_scope
from app.db.job_record import JobRow
from app.training import handlers, jobs


def _wait(job_id: str, timeout: float = 10.0) -> jobs.JobRecord:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = jobs.get(job_id)
        if record is not None and record.status in {"succeeded", "failed"}:
            return record
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


@pytest.fixture
def thread_runner(monkeypatch):
    monkeypatch.setattr(settings, "JOB_RUNNER", "thread")


@pytest.fixture
def github_runner(monkeypatch):
    monkeypatch.setattr(settings, "JOB_RUNNER", "github")
    monkeypatch.setattr(settings, "GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(settings, "GITHUB_WORKFLOW", "worker.yml")
    monkeypatch.setattr(settings, "GITHUB_REF", "main")


@pytest.fixture
def echo(monkeypatch):
    """A handler kind that reports a stage and hands its params back."""
    def run(params, progress):
        progress("echoing", {"n": params.get("n")})
        return {"echo": params}

    monkeypatch.setitem(handlers.HANDLERS, "echo", run)


@pytest.fixture
def boom(monkeypatch):
    def run(params, progress):
        raise RuntimeError("the handler broke")

    monkeypatch.setitem(handlers.HANDLERS, "boom", run)


def test_thread_runner_runs_the_handler_and_records_its_result(thread_runner, echo):
    job = jobs.submit("echo", {"n": 1})
    assert job["status"] == "queued" and job["kind"] == "echo"
    record = _wait(job["job_id"])
    assert record.status == "succeeded"
    assert record.result == {"echo": {"n": 1}}
    assert "echoing" in record.stages and record.stages[-1] == "finished"
    assert record.started_at is not None and record.finished_at is not None


def test_a_failing_handler_fails_the_job_with_its_error(thread_runner, boom):
    record = _wait(jobs.submit("boom", {})["job_id"])
    assert record.status == "failed"
    assert "RuntimeError: the handler broke" in record.error


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        jobs.submit("no-such-kind", {})


def test_the_same_work_queued_twice_is_one_job(github_runner, monkeypatch):
    """A replay build already under way is returned, not started again."""
    monkeypatch.setattr(settings, "GITHUB_DISPATCH_TOKEN", "t")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(204))
    first = jobs.submit("replay", {"session_key": 424242, "force": False})
    again = jobs.submit("replay", {"session_key": 424242, "force": True})
    other = jobs.submit("replay", {"session_key": 424243, "force": False})
    assert again["job_id"] == first["job_id"]
    assert other["job_id"] != first["job_id"]


def test_github_runner_without_a_token_fails_with_what_to_set(github_runner, monkeypatch, echo):
    monkeypatch.setattr(settings, "GITHUB_DISPATCH_TOKEN", "")
    job = jobs.submit("echo", {"n": 2})
    assert job["status"] == "failed"
    assert "GITHUB_DISPATCH_TOKEN" in job["error"]
    assert jobs.get(job["job_id"]).status == "failed"


def test_github_runner_dispatches_the_worker_and_leaves_the_job_queued(
    github_runner, monkeypatch, echo
):
    monkeypatch.setattr(settings, "GITHUB_DISPATCH_TOKEN", "secret-token")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "post", fake_post)
    job = jobs.submit("echo", {"n": 3})
    assert job["status"] == "queued"
    url, body, headers = calls[0]
    assert url == "https://api.github.com/repos/owner/repo/actions/workflows/worker.yml/dispatches"
    assert body == {"ref": "main", "inputs": {"job_id": job["job_id"]}}
    assert headers["Authorization"] == "Bearer secret-token"
    assert jobs.get(job["job_id"]).status == "queued"


def test_a_refused_dispatch_fails_the_job(github_runner, monkeypatch, echo):
    monkeypatch.setattr(settings, "GITHUB_DISPATCH_TOKEN", "secret-token")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(404, text="Not Found"))
    job = jobs.submit("echo", {"n": 4})
    assert job["status"] == "failed" and "404" in job["error"]


def _age(job_id: str, **columns) -> None:
    with session_scope() as db:
        row = db.get(JobRow, job_id)
        for name, value in columns.items():
            setattr(row, name, value)


def test_a_job_nobody_picked_up_is_reported_failed(github_runner, monkeypatch, echo):
    monkeypatch.setattr(settings, "GITHUB_DISPATCH_TOKEN", "t")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(204))
    job_id = jobs.submit("echo", {"n": 5})["job_id"]
    assert jobs.get(job_id).status == "queued"
    _age(job_id, created_at=datetime.now(timezone.utc) - timedelta(minutes=31))
    record = jobs.get(job_id)
    assert record.status == "failed" and "never picked it up" in record.error


def test_a_job_running_for_hours_is_reported_failed(github_runner, monkeypatch, echo):
    monkeypatch.setattr(settings, "GITHUB_DISPATCH_TOKEN", "t")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(204))
    job_id = jobs.submit("echo", {"n": 6})["job_id"]
    long_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    _age(job_id, status="running", started_at=long_ago)
    record = jobs.get(job_id)
    assert record.status == "failed" and "timed out" in record.error
    # Dead work is not "already under way": the same job may be queued again.
    assert jobs.active("echo", {"n": 6}) is None


def test_run_job_cli_runs_a_queued_job(github_runner, monkeypatch, echo):
    monkeypatch.setattr(settings, "GITHUB_DISPATCH_TOKEN", "t")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(204))
    job_id = jobs.submit("echo", {"n": 7})["job_id"]
    assert cli.main(["run-job", job_id]) == 0
    record = jobs.get(job_id)
    assert record.status == "succeeded" and record.result == {"echo": {"n": 7}}
    # Not queued any more: a second run does nothing, and says it succeeded.
    assert cli.main(["run-job", job_id]) == 0


def test_run_job_cli_exits_non_zero_when_the_job_fails(github_runner, monkeypatch, boom):
    monkeypatch.setattr(settings, "GITHUB_DISPATCH_TOKEN", "t")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(204))
    job_id = jobs.submit("boom", {})["job_id"]
    assert cli.main(["run-job", job_id]) == 1
    assert jobs.get(job_id).status == "failed"
    assert cli.main(["run-job", "does-not-exist"]) == 2


def test_work_cli_drains_the_queue(github_runner, monkeypatch, echo):
    monkeypatch.setattr(settings, "GITHUB_DISPATCH_TOKEN", "t")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(204))
    ids = [jobs.submit("echo", {"n": 100 + i})["job_id"] for i in range(2)]
    # Only jobs this test queued: anything older left queued is someone else's.
    with session_scope() as db:
        for row in db.query(JobRow).filter(JobRow.status == "queued", JobRow.id.notin_(ids)):
            row.status = "failed"
    assert cli.main(["work"]) == 0
    assert all(jobs.get(i).status == "succeeded" for i in ids)


def test_progress_is_written_to_the_row_at_most_once_a_second(github_runner, monkeypatch, echo):
    monkeypatch.setattr(settings, "GITHUB_DISPATCH_TOKEN", "t")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(204))
    job_id = jobs.submit("echo", {"n": 8})["job_id"]
    writes = []
    real_write = jobs._write
    monkeypatch.setattr(jobs, "_write", lambda i, **v: (writes.append(v), real_write(i, **v)))
    reporter = jobs.ProgressReporter(jobs.get(job_id), persist=True)
    for car in range(20):
        reporter("locations", {"car": car})
    assert len(writes) == 1
    assert jobs.get(job_id).detail == {"car": 0}
    reporter.flush()
    assert jobs.get(job_id).detail == {"car": 19}
