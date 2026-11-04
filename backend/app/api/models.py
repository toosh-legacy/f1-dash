"""The registry: what has been trained, and what is allowed to serve."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DBSession

from app import schemas
from app.config import settings
from app.db import models as m
from app.db.database import get_db
from app.models import calibration
from app.models import registry

router = APIRouter()


@router.get("/models", response_model=list[schemas.PredictionModelOut], tags=["models"])
def list_models(model_type: str | None = None, db: DBSession = Depends(get_db)):
    return registry.list_models(db, model_type)


@router.post("/models/{model_id}/promote", response_model=schemas.PromoteResponse, tags=["models"])
def promote_model(model_id: int, db: DBSession = Depends(get_db)):
    """Flip ``is_active`` after manual review -- deliberately separate from retraining."""
    previous = None
    record = db.get(m.PredictionModel, model_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no model {model_id}")
    if record.regs_regime != settings.CURRENT_REGS_REGIME:
        raise HTTPException(
            status_code=409,
            detail=(
                f"model was trained under regime {record.regs_regime!r}, current regime is "
                f"{settings.CURRENT_REGS_REGIME!r}; promoting it would serve predictions "
                "from a superseded set of regulations"
            ),
        )
    active = registry.active_record(db, record.model_type)
    if active is not None:
        previous = active.version
    promoted = registry.promote(db, model_id)
    db.commit()
    return schemas.PromoteResponse(
        id=promoted.id,
        model_type=promoted.model_type,
        version=promoted.version,
        is_active=promoted.is_active,
        previous_active_version=previous,
    )


@router.get("/models/compare", tags=["models"])
def compare_models(db: DBSession = Depends(get_db)) -> dict[str, Any]:
    """Score every evaluated model against the others on the laps they share.

    Promotion is deliberate here, and this is the evidence for it: run a
    challenger over a replay with ``POST /replays/{key}/predict?model_id=N``,
    then read this to see whether it was actually closer to the finishing order
    than the model already serving.
    """
    return calibration.compare_models(db)
