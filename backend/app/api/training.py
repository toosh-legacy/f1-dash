"""Retraining, and the job records it returns instead of blocking (rule 1)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session as DBSession

from app import schemas
from app.api.deps import require_session
from app.db import models as m
from app.db.database import get_db
from app.training import jobs
from app.training import retrain_qualifying, retrain_race

router = APIRouter()


@router.post("/sessions/{session_id}/retrain", response_model=schemas.JobOut, tags=["training"])
def retrain(session_id: int, db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """Queue the offline retraining pipeline. Returns immediately (rule 1)."""
    session = require_session(db, session_id)
    session_type = m.SessionType(session.session_type)
    if session_type.is_qualifying:
        return retrain_qualifying.trigger(session_id)
    if session_type.is_race:
        return retrain_race.trigger(session_id)
    raise HTTPException(
        status_code=400,
        detail=f"{session.session_type} sessions are not a training target; "
        "retrain from a qualifying or race session",
    )


@router.get("/jobs/{job_id}", response_model=schemas.JobOut, tags=["training"])
def get_job(job_id: str) -> dict[str, Any]:
    record = jobs.jobs.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return record.as_dict()


@router.get("/jobs", response_model=list[schemas.JobOut], tags=["training"])
def list_jobs(limit: int = Query(default=20, le=50)) -> list[dict[str, Any]]:
    return [record.as_dict() for record in jobs.jobs.recent(limit)]
