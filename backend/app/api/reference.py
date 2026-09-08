"""Circuits, drivers, and seeding them from real season data."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app import schemas
from app.data import seed as seeding
from app.data.fastf1_client import FastF1Unavailable
from app.db import models as m
from app.db.database import get_db

router = APIRouter()


@router.get("/circuits", response_model=list[schemas.CircuitOut], tags=["reference"])
def list_circuits(db: DBSession = Depends(get_db)):
    return db.scalars(select(m.Circuit).order_by(m.Circuit.name)).all()


@router.get("/drivers", response_model=list[schemas.DriverOut], tags=["reference"])
def list_drivers(db: DBSession = Depends(get_db)):
    return db.scalars(select(m.Driver).order_by(m.Driver.driver_number)).all()


@router.post("/seed", tags=["reference"])
def seed(request: schemas.SeedRequest, db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """Seed circuits, calendar, teams and drivers from real season data (M1)."""
    try:
        result = seeding.seed_all(db, request.year, request.limit_events)
    except (RuntimeError, FastF1Unavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.commit()
    return result
