"""Where this race is heading, from where it currently is.

The dashboard's prediction panel answers one question at any point in a race:
given what has happened up to this lap, who finishes where? Two independent
answers are given, and they are deliberately not blended into one number.

**The projection** is arithmetic, not learning. It starts from where the cars
actually are -- track position is the strongest single predictor of a finishing
order -- and corrects that with the time each car is expected to gain or lose
from here: its pace against the front of the field over the laps remaining, and
the time it still owes the pit lane.

    delta  = gap_to_leader - pace_advantage x laps_remaining x conversion
             + pit_loss x stops_owed
    order  = track position, weighted towards the projection by how much race
             is left to make its corrections come true

Every term is shown alongside the result, so a surprising order can be read
rather than trusted. The correction is damped by how hard the circuit is to
overtake at, because a pace advantage that cannot be used is not an advantage.

**The model** is the trained race-finish estimator, when one is active for this
regime and the session is known to the database. It sees the same race through
the engineered feature vector instead.

Where they disagree the panel shows both, because the disagreement is the
interesting part: it usually means a strategy is about to pay off or fail.
"""
from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass
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

#: How far to trust an extrapolated pace advantage when it is converted into
#: places. Measured, not chosen: scored over 194 laps of three finished races,
#: mean absolute error in finishing positions against this value --
#:
#:     trust     0.00   0.15   0.25   0.35   0.50   ramp either way
#:     error     2.14   2.12   2.18   2.23   2.32   2.33 - 2.36
#:
#: So a pace reading earns its place, but only just, and only applied gently.
#: Both of the obvious ramps -- more say early, more say late -- scored worse
#: than a light constant, because the shrinking horizon is already carried by
#: the time terms: pace advantage is multiplied by the laps remaining, so
#: damping by it again counted the same thing twice.
#:
#: The pit-stop term is damped by this too. It was tried at full strength, on
#: the grounds that a stop is arithmetic rather than a guess, and scored 2.76 --
#: much worse. The stop itself is certain; *whether a car still owes one* is
#: inferred from a compound-life table, and a wrong twenty-second penalty moves
#: a car further than a wrong tenth of a second ever could.
PACE_TRUST = 0.15

#: Floor on the seconds-per-place exchange rate. Cars nose to tail would make
#: a second worth the whole field, which no amount of pace can deliver.
MINIMUM_FIELD_SPACING_S = 0.8

#: How much of a pace advantage the hardest circuit to pass on takes away.
#: At Monaco a car half a second a lap quicker than the one ahead finishes
#: behind it; at Monza it does not. Scaled by the circuit's own difficulty, so
#: a value of 1.0 would mean the hardest circuit converts no pace into places
#: at all -- 0.8 leaves a little, since even Monaco has a pit lane.
PASSING_PENALTY = 0.8

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
    circuit = _circuit_profile(bundle, db)
    drivers = {d["number"]: d for d in bundle.get("drivers", [])}

    owed = {row["number"]: _stops_owed(row, remaining) for row in rows}
    leader_owed = min(owed.values(), default=0)

    # How much of a pace advantage a car can actually convert into places. At a
    # circuit where nobody overtakes, being quicker than the car ahead buys a
    # closer view of its gearbox and nothing else.
    conversion = 1.0 - PASSING_PENALTY * (circuit.overtaking_difficulty or 0.5)

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
        pace_gain = clamped * confidence * remaining * conversion
        stop_cost = (owed[number] - leader_owed) * circuit.pit_loss_s
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
                # The gap the ranking used, fallback included, so the anchor
                # and the projection are always measured on the same scale.
                "gap_used_s": round(gap, 2),
                # Kept apart so the ranking can trust them differently.
                "stop_cost_s": round(stop_cost, 2),
                "projected_delta_s": round(projected, 2),
            }
        )

    _rank(entries, remaining, total_laps)

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
        "pit_loss_s": circuit.pit_loss_s,
        "overtaking_difficulty": circuit.overtaking_difficulty,
        "track_position_weight": round(_anchor(remaining, total_laps), 2),
        "entries": entries,
        "model": model_note,
        "basis": "track position, corrected for pace, tyre state and stops still owed",
    }


def _rank(entries: list[dict[str, Any]], remaining: int, total_laps: int) -> None:
    """Order the field: the running order, moved by what the projection is worth.

    Track position is the strongest single predictor of a finishing order. Two
    earlier versions of this lost to it -- one ranked purely on projected time
    and threw the order away; the next blended the two in seconds, which sounds
    principled but compares a twenty-second pit stop against gaps of a few
    tenths, so any car owing a stop was flung the length of the field.

    What settles it is the exchange rate. A second is worth a different number
    of places in a train of backmarkers than it is at the front of a spread-out
    race, so the projection's time saving is converted into places using the
    *field's own spacing on this lap*, and then applied as a nudge to where the
    car already is. A car projected to gain nothing stays where it is, which is
    the right default; a car about to pit moves by however many cars it is
    actually going to come out behind.

    The nudge is scaled down as the race runs out, since a correction needs
    laps left in which to come true.
    """
    if not entries:
        return

    spacing = _field_spacing(entries)

    for rank, entry in enumerate(
        sorted(entries, key=lambda e: e["projected_delta_s"]), start=1
    ):
        entry["pace_rank"] = rank

    field = len(entries)
    for entry in entries:
        running = entry["position"] if entry["position"] is not None else entry["pace_rank"]
        # One weight over the whole correction, including the pit-stop term.
        # That term looks like arithmetic on a known pit loss and was tried at
        # full strength for exactly that reason -- and scored far worse (2.76
        # against 2.12). The reason is that *stops owed* is not observed, it is
        # inferred from a table of how long a compound lasts, and a car that
        # extends its stint gets thrown the length of the field by a
        # twenty-second penalty it was never going to pay.
        gained = entry["gap_used_s"] - entry["projected_delta_s"]
        places = (gained / spacing) * PACE_TRUST
        entry["projected_places_gained"] = round(places, 2)
        entry["_score"] = running - places

    entries.sort(key=lambda e: (e["_score"], e["gap_used_s"]))
    winner = entries[0]["projected_delta_s"]
    for index, entry in enumerate(entries, start=1):
        entry.pop("_score", None)
        entry["projected_position"] = index
        # Re-zero on the projected winner: measured from whoever leads *now*,
        # the figure goes negative for anyone projected to pass them and reads
        # as nonsense in a finishing order.
        entry["projected_gap_s"] = round(entry["projected_delta_s"] - winner, 2)
        entry["position_change"] = (
            entry["position"] - index if entry["position"] is not None else None
        )
    assert len({e["projected_position"] for e in entries}) == field


def _field_spacing(entries: list[dict[str, Any]]) -> float:
    """Seconds between one place and the next, as this race is actually spread.

    The median gap between adjacent cars, so a second buys fewer places in a
    tight midfield train than it does in a strung-out race. Falls back to a
    nominal second when the field is too small or too bunched to measure.
    """
    gaps = sorted(entry["gap_used_s"] for entry in entries)
    steps = [b - a for a, b in zip(gaps, gaps[1:]) if b - a > 0]
    if not steps:
        return MINIMUM_FIELD_SPACING_S
    return max(statistics.median(steps), MINIMUM_FIELD_SPACING_S)


def _anchor(remaining: int = 0, total_laps: int = 0) -> float:
    """How much of the ranking is simply where the cars are now.

    Reported to the panel so a reader can see how much of the order is the
    order on the road. The arguments are kept so callers read as they did;
    neither is used, for the reason in :data:`PACE_TRUST`.
    """
    return 1.0 - PACE_TRUST


@dataclass
class CircuitProfile:
    """What the circuit itself contributes to a projection."""

    pit_loss_s: float = DEFAULT_PIT_LOSS_S
    overtaking_difficulty: float | None = None


def _circuit_profile(bundle: dict[str, Any], db: DBSession | None) -> CircuitProfile:
    """This circuit's pit loss and how hard it is to pass on, if it is known.

    The replay names circuits as OpenF1 does and the database as FastF1 does,
    so the two are matched on identifying words rather than on either spelling.
    A near-miss here would silently price every stop wrongly, so the calendar's
    boilerplate is excluded and a partial match has to be a whole word.
    """
    if db is None:
        return CircuitProfile()
    session = bundle.get("session", {})
    circuits = db.scalars(select(m.Circuit)).all()

    # The circuit's own name first. Only if that finds nothing is the country
    # tried, which resolves the cases where the two sources disagree entirely
    # -- OpenF1's "Monte Carlo" against a database that calls it Monaco -- and
    # is a last resort because several rounds can share one.
    for wanted in (
        _words(session.get("circuit")) | _words(session.get("location")),
        _words(session.get("country")),
    ):
        if not wanted:
            continue
        for circuit in circuits:
            if wanted & (_words(circuit.id) | _words(circuit.name)):
                return CircuitProfile(
                    pit_loss_s=float(circuit.avg_pit_loss_s or DEFAULT_PIT_LOSS_S),
                    overtaking_difficulty=circuit.historical_overtaking_difficulty,
                )
    return CircuitProfile()


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


#: Words that appear in circuit and event names across the calendar and so
#: identify nothing. Without these, "Belgian Grand Prix" would match "Miami
#: Grand Prix" on two words out of three.
_GENERIC_NAME_WORDS = frozenset(
    {
        "grand", "prix", "gran", "premio", "circuit", "autodromo", "autodrome",
        "international", "raceway", "park", "speedway", "street", "national",
        "nazionale", "the", "and", "city",
    }
)


def _words(value: str | None) -> set[str]:
    """Identifying words in a circuit name, lowercased.

    Two-letter fragments and the calendar's boilerplate are dropped; what is
    left is the part that actually names a place -- "spa", "monza", "sakhir".
    """
    if not value:
        return set()
    tokens = re.split(r"[^a-z0-9]+", value.lower())
    return {
        token
        for token in tokens
        if len(token) > 2 and token not in _GENERIC_NAME_WORDS
    }


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
