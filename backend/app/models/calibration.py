"""How wrong the predictions usually are, measured against what happened.

A projected finishing order is unfalsifiable while a race is running, and a
number nobody can check is a number nobody should trust. But a replay is a
finished race: the projections are stored per lap, the classification is in the
bundle, and comparing them is arithmetic.

So every prediction this project makes can be scored against the result, and
three answers are scored side by side:

* **track position** -- where the car is right now, taken as the finishing
  order. The baseline any prediction has to beat to have earned its place.
* **the projection** -- the arithmetic in :mod:`app.live.projection`.
* **the model** -- the trained finish-position estimator.

The measure is mean absolute error in places. Two places of error means a
typical car finishes two positions away from where it was projected.

Scores are grouped by how far through the race the prediction was made, since
a projection on lap two and one on the last lap are not the same claim. Only
laps that have already been evaluated are scored: this reads the cache and
never runs a model, so it stays cheap enough to serve on request.
"""
from __future__ import annotations

import logging
import statistics
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.data import replay
from app.db import models as m

log = logging.getLogger(__name__)

#: Race quarters. A prediction is only comparable with another made at a
#: similar point, because the amount of race left to be wrong about differs.
STAGES: tuple[tuple[str, float, float], ...] = (
    ("opening quarter", 0.0, 0.25),
    ("second quarter", 0.25, 0.5),
    ("third quarter", 0.5, 0.75),
    ("final quarter", 0.75, 1.01),
)

METHODS = ("track_position", "projection", "model")


def stage_for(lap: int, total_laps: int) -> str | None:
    """Which quarter of the race a lap falls in."""
    if total_laps <= 0:
        return None
    fraction = lap / total_laps
    for name, low, high in STAGES:
        if low <= fraction < high:
            return name
    return STAGES[-1][0]


def score_replay(db: DBSession, session_key: int) -> dict[str, Any]:
    """Score every evaluated lap of one replay against its classification."""
    bundle = replay.load_bundle(session_key)
    if bundle is None:
        return {"session_key": session_key, "scored_laps": 0, "reason": "replay not built"}

    actual = {
        row["number"]: row["position"]
        for row in bundle.get("results", [])
        if row.get("position") is not None
    }
    if not actual:
        return {"session_key": session_key, "scored_laps": 0, "reason": "no classification"}

    total_laps = int(bundle.get("total_laps") or 0)
    rows = db.scalars(
        select(m.ReplayProjection)
        .where(m.ReplayProjection.openf1_session_key == session_key)
        .order_by(m.ReplayProjection.lap_number)
    ).all()

    laps: list[dict[str, Any]] = []
    for row in rows:
        scored = _score_lap(row.payload, actual)
        if scored is None:
            continue
        laps.append(
            {
                "lap": row.lap_number,
                "stage": stage_for(row.lap_number, total_laps),
                "model_version": row.model_version,
                **scored,
            }
        )

    return {
        "session_key": session_key,
        "circuit": bundle.get("session", {}).get("circuit"),
        "total_laps": total_laps,
        "scored_laps": len(laps),
        "laps": laps,
        "by_stage": _by_stage(laps),
        "overall": _means(laps),
    }


def summary(db: DBSession, session_keys: list[int] | None = None) -> dict[str, Any]:
    """Scores across every replay that has been evaluated.

    This is what the prediction panel quotes back to a viewer: at this stage of
    a race, this is how far out the projection has typically been.
    """
    keys = session_keys or [
        key
        for (key,) in db.execute(
            select(m.ReplayProjection.openf1_session_key).distinct()
        ).all()
    ]

    laps: list[dict[str, Any]] = []
    races: list[dict[str, Any]] = []
    for key in keys:
        scored = score_replay(db, key)
        if not scored.get("scored_laps"):
            continue
        laps.extend(scored["laps"])
        races.append(
            {
                "session_key": key,
                "circuit": scored.get("circuit"),
                "scored_laps": scored["scored_laps"],
                "overall": scored["overall"],
            }
        )

    return {
        "races": races,
        "scored_laps": len(laps),
        "by_stage": _by_stage(laps),
        "overall": _means(laps),
        "measure": "mean absolute error, in finishing positions",
    }


# --------------------------------------------------------------------------
def _score_lap(payload: dict[str, Any], actual: dict[int, int]) -> dict[str, Any] | None:
    """Mean absolute error of each method on one lap, in places."""
    entries = [e for e in payload.get("entries", []) if e.get("number") in actual]
    if len(entries) < 5:  # a handful of cars is not a finishing order
        return None

    field = len(entries)
    errors: dict[str, list[float]] = {method: [] for method in METHODS}
    for entry in entries:
        finished = actual[entry["number"]]
        # A car with no reading is placed at the back of the field, which is
        # what its absence usually means.
        errors["track_position"].append(abs(finished - (entry.get("position") or field)))
        errors["projection"].append(abs(finished - entry["projected_position"]))
        if entry.get("model_position"):
            errors["model"].append(abs(finished - entry["model_position"]))

    scored = {
        method: round(statistics.mean(values), 3)
        for method, values in errors.items()
        if values
    }
    scored["cars"] = field
    return scored


def _means(laps: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"laps": len(laps)}
    for method in METHODS:
        values = [lap[method] for lap in laps if lap.get(method) is not None]
        out[method] = round(statistics.mean(values), 3) if values else None
    out["best"] = _best(out)
    return out


def _best(means: dict[str, Any]) -> str | None:
    scored = {
        method: means[method]
        for method in METHODS
        if isinstance(means.get(method), (int, float))
    }
    return min(scored, key=scored.get) if scored else None


def _by_stage(laps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for name, _low, _high in STAGES:
        in_stage = [lap for lap in laps if lap.get("stage") == name]
        if in_stage:
            out.append({"stage": name, **_means(in_stage)})
    return out
