"""Where this race is heading, from where it currently is.

The dashboard's prediction panel answers one question at any point in a race:
given what has happened up to this lap, who finishes where? Two independent
answers are given, and they are deliberately not blended into one number.

**The projection** is arithmetic, not learning. A car's finishing time is its
current gap to the leader, minus what its pace advantage will win it over the
remaining laps, plus the time it still owes the pit lane. Every term is shown
alongside the result, so a surprising order can be read rather than trusted:

    delta = gap_to_leader - pace_delta x laps_remaining + pit_loss x stops_owed

**The model** is the trained race-finish estimator, when one is active for this
regime and the session is known to the database. It sees the same race through
the engineered feature vector instead.

Where they disagree the panel shows both, because the disagreement is the
interesting part: it usually means a strategy is about to pay off or fail.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.db import models as m
from app.features.builder import FeatureBuilder
from app.features.engineering import LiveState
from app.models import registry

log = logging.getLogger(__name__)

#: Fallback pit loss when the circuit is not in the reference table.
DEFAULT_PIT_LOSS_S = 21.0

#: Pace advantage is clamped before it is extrapolated. A second a lap quicker
#: than the field, sustained to the flag, is already an extreme claim; anything
#: past that is a timing artefact -- an out-lap, traffic, a safety car -- and
#: multiplying it by fifty remaining laps would swamp the order.
PACE_CLAMP_S = 0.8

#: Pace is trusted in proportion to how much of it has been seen. On lap one
#: there is a single racing lap to go on, so its advantage barely counts.
PACE_CONFIDENCE_LAPS = 5

#: A car this far from the end of the race on old rubber still owes a stop.
#: Below it, whatever is fitted is being taken to the flag.
STOP_HORIZON_LAPS = 12

#: Tyre life assumed before a stop becomes necessary, by compound. Approximate
#: by design: the panel shows the assumption rather than hiding it.
COMPOUND_LIFE_LAPS = {
    "SOFT": 18,
    "MEDIUM": 28,
    "HARD": 40,
    "INTERMEDIATE": 30,
    "WET": 30,
}
DEFAULT_TYRE_LIFE_LAPS = 30

#: A car stopping for the run to the flag fits whatever covers the distance, so
#: stints still to come are measured against the longest-lived compound rather
#: than against whatever happens to be on the car now.
LONGEST_STINT_LAPS = max(COMPOUND_LIFE_LAPS.values())


def project_finish(
    bundle: dict[str, Any],
    lap: int,
    *,
    db: DBSession | None = None,
) -> dict[str, Any]:
    """Projected finishing order at ``lap``, with the reasoning behind it."""
    rows = bundle.get("laps", {}).get(str(lap)) or []
    if not rows:
        return {"lap": lap, "entries": [], "model": None, "basis": "no timing data"}

    total_laps = int(bundle.get("total_laps") or 0)
    remaining = max(total_laps - lap, 0)
    pit_loss = _pit_loss(bundle, db)
    drivers = {d["number"]: d for d in bundle.get("drivers", [])}

    owed = {row["number"]: _stops_owed(row, remaining) for row in rows}
    leader_owed = min(owed.values(), default=0)

    entries: list[dict[str, Any]] = []
    for row in rows:
        number = row["number"]
        pace_delta = row.get("pace_delta_s") or 0.0
        gap = row.get("gap_to_leader_s")
        # A car with no interval reading is timed off its position instead --
        # crude, but it keeps a full grid on screen rather than dropping cars.
        if gap is None:
            gap = float((row.get("position") or len(rows)) - 1) * 1.5

        confidence = min(lap, PACE_CONFIDENCE_LAPS) / PACE_CONFIDENCE_LAPS
        clamped = max(min(pace_delta, PACE_CLAMP_S), -PACE_CLAMP_S)
        pace_gain = clamped * confidence * remaining
        stop_cost = (owed[number] - leader_owed) * pit_loss
        projected = gap - pace_gain + stop_cost

        driver = drivers.get(number, {})
        entries.append(
            {
                "number": number,
                "code": driver.get("code"),
                "name": driver.get("name"),
                "team": driver.get("team"),
                "colour": driver.get("colour"),
                "position": row.get("position"),
                "compound": row.get("compound"),
                "tyre_age": row.get("tyre_age"),
                "stops": row.get("stops"),
                "stops_owed": owed[number],
                "pace_s": row.get("pace_s"),
                "pace_delta_s": row.get("pace_delta_s"),
                "pace_gain_s": round(pace_gain, 2),
                "gap_to_leader_s": row.get("gap_to_leader_s"),
                "projected_delta_s": round(projected, 2),
            }
        )

    entries.sort(key=lambda e: e["projected_delta_s"])
    for index, entry in enumerate(entries, start=1):
        entry["projected_position"] = index
        entry["position_change"] = (
            entry["position"] - index if entry["position"] is not None else None
        )

    model_note = None
    if db is not None:
        try:
            model_note = _model_view(db, bundle, lap, entries)
        except Exception as exc:  # the panel must survive a missing model
            log.warning("model projection unavailable: %s", exc)
            model_note = {"available": False, "reason": str(exc)}

    return {
        "lap": lap,
        "total_laps": total_laps,
        "laps_remaining": remaining,
        "pit_loss_s": pit_loss,
        "entries": entries,
        "model": model_note,
        "basis": "pace, position, tyre state and stops still owed",
    }


def _stops_owed(row: dict[str, Any], remaining: int) -> int:
    """Stops this car still has to make, from tyre state and laps left."""
    if remaining <= STOP_HORIZON_LAPS:
        return 0
    life = COMPOUND_LIFE_LAPS.get((row.get("compound") or "").upper(), DEFAULT_TYRE_LIFE_LAPS)
    age = row.get("tyre_age") or 0
    left_on_these = max(life - age, 0)
    if left_on_these >= remaining:
        return 0
    # Whatever the current set cannot cover has to be covered by fresh sets.
    return max(1, -(-(remaining - left_on_these) // LONGEST_STINT_LAPS))


def _pit_loss(bundle: dict[str, Any], db: DBSession | None) -> float:
    if db is None:
        return DEFAULT_PIT_LOSS_S
    circuit_name = (bundle.get("session", {}).get("circuit") or "").lower()
    if not circuit_name:
        return DEFAULT_PIT_LOSS_S
    for circuit in db.scalars(select(m.Circuit)).all():
        if circuit.id in circuit_name or circuit_name.split()[0] in circuit.id:
            return float(circuit.avg_pit_loss_s or DEFAULT_PIT_LOSS_S)
    return DEFAULT_PIT_LOSS_S


def _model_view(
    db: DBSession,
    bundle: dict[str, Any],
    lap: int,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run the trained finish-position model over the same lap, if one is live."""
    session = resolve_db_session(db, bundle)
    if session is None:
        return {"available": False, "reason": "session not in the database"}

    active = registry.load_active(
        db, m.ModelType.RACE_FINISH_POSITION.value, regs_regime=session.regs_regime
    )
    if active is None:
        return {"available": False, "reason": "no active race-finish model"}
    model, record = active

    drivers = {
        driver.driver_number: driver
        for driver in db.scalars(select(m.Driver)).all()
        if driver.driver_number
    }
    builder = FeatureBuilder(db, season=session.year, as_of=session.start_time)
    total_laps = int(bundle.get("total_laps") or 0)

    vectors: list[dict[str, float]] = []
    numbers: list[int] = []
    for entry in entries:
        driver = drivers.get(entry["number"])
        if driver is None:
            continue
        live = LiveState(
            lap_number=lap,
            total_laps=total_laps or None,
            position=entry.get("position"),
            gap_ahead_s=None,
            gap_behind_s=None,
            compound=entry.get("compound"),
            tyre_age_laps=entry.get("tyre_age"),
            safety_car_active=False,
            vsc_active=False,
            red_flag_active=False,
        )
        vector = builder.build_vector(session, driver, f"lap_{lap}", live=live)
        vectors.append(vector.values)
        numbers.append(entry["number"])

    if not vectors:
        return {"available": False, "reason": "no drivers matched the entry list"}

    predictions = model.predict(vectors).tolist()
    ranked = sorted(zip(numbers, predictions), key=lambda pair: pair[1])
    order = {number: index for index, (number, _score) in enumerate(ranked, start=1)}
    for entry in entries:
        entry["model_position"] = order.get(entry["number"])
        entry["model_score"] = next(
            (round(float(score), 2) for number, score in ranked if number == entry["number"]),
            None,
        )
    return {
        "available": True,
        "model_id": record.id,
        "model_type": record.model_type,
        "version": record.version,
        "regs_regime": record.regs_regime,
    }


def resolve_db_session(db: DBSession, bundle: dict[str, Any]) -> m.Session | None:
    """Find the stored session a replay belongs to, and remember the link.

    The database is seeded from FastF1 and the replay comes from OpenF1, so the
    two are joined on OpenF1's session key -- recorded the first time a match is
    found by date, since that is the only field both sides agree on exactly.
    """
    meta = bundle.get("session", {})
    key = meta.get("session_key")
    if key is None:
        return None

    session = db.scalar(select(m.Session).where(m.Session.openf1_session_key == int(key)))
    if session is not None:
        return session

    started = meta.get("date_start")
    if not started:
        return None
    day = str(started)[:10]
    wanted = "Sprint" if str(meta.get("name", "")).lower().startswith("sprint") else "Race"
    for candidate in db.scalars(
        select(m.Session).where(m.Session.session_type == wanted)
    ).all():
        if candidate.start_time and candidate.start_time.date().isoformat() == day:
            candidate.openf1_session_key = int(key)
            db.flush()
            return candidate
    return None
