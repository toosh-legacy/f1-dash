"""Race-control state: detection, persistence, and prediction gating (rule 4).

A confident finishing-position number during a safety car assumes normal racing
that isn't happening. So live race predictions are gated: during any non-green
state the prediction is still produced (the dashboard keeps updating) but is
flagged ``is_gated`` and rendered as low-confidence.

Race-control messages are also pushed to clients *immediately* on any state
change, rather than waiting for the next scheduled per-lap update.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.db import models as m
from app.db.models import RaceControlEventType as RC

log = logging.getLogger(__name__)

# Ordered most specific first: "VIRTUAL SAFETY CAR" must not match plain
# "SAFETY CAR", and "SAFETY CAR IN THIS LAP" is still a safety-car state.
_MESSAGE_PATTERNS: list[tuple[re.Pattern[str], RC]] = [
    (re.compile(r"\bRED\b|RED FLAG", re.I), RC.RED_FLAG),
    (re.compile(r"VIRTUAL SAFETY CAR|\bVSC\b", re.I), RC.VSC),
    (re.compile(r"SAFETY CAR|\bSC\b(?!\w)", re.I), RC.SAFETY_CAR),
    (re.compile(r"YELLOW", re.I), RC.YELLOW),
    (re.compile(r"GREEN|TRACK CLEAR|CLEAR\b", re.I), RC.GREEN),
]

#: Messages that *end* a state rather than starting one.
_ENDING = re.compile(r"ENDING|IN THIS LAP|DEPLOYED\s*ENDS|CLEAR|WITHDRAWN", re.I)


@dataclass
class GateState:
    """Current race-control state for one session."""

    event_type: RC = RC.GREEN
    lap_number: int | None = None

    @property
    def is_gated(self) -> bool:
        return self.event_type.suppresses_predictions

    @property
    def reason(self) -> str | None:
        return self.event_type.value if self.is_gated else None


def classify_message(text: str, flag: str | None = None, category: str | None = None) -> RC | None:
    """Map a race-control message to a state, or ``None`` if it carries none."""
    blob = " ".join(part for part in (flag, category, text) if part)
    if not blob.strip():
        return None
    for pattern, event_type in _MESSAGE_PATTERNS:
        if pattern.search(blob):
            # "SAFETY CAR IN THIS LAP" means the SC is coming in -> back to green.
            if event_type in {RC.SAFETY_CAR, RC.VSC} and _ENDING.search(blob):
                return RC.GREEN
            if event_type is RC.RED_FLAG and _ENDING.search(blob):
                return RC.GREEN
            return event_type
    return None


def classify_fastf1_message(message: dict[str, Any]) -> RC | None:
    """Classify a FastF1 historical race-control row."""
    return classify_message(
        str(message.get("message", "")),
        flag=str(message.get("flag", "")),
        category=str(message.get("category", "")),
    )


def classify_openf1_message(message: dict[str, Any]) -> RC | None:
    """Classify a live OpenF1 ``race_control`` row."""
    return classify_message(
        str(message.get("message", "")),
        flag=str(message.get("flag", "")),
        category=str(message.get("category", "")),
    )


def current_state(db: DBSession, session_id: int) -> GateState:
    """Latest recorded state for a session; green when nothing is recorded."""
    row = db.scalar(
        select(m.RaceControlEvent)
        .where(m.RaceControlEvent.session_id == session_id)
        .order_by(m.RaceControlEvent.created_at.desc(), m.RaceControlEvent.id.desc())
        .limit(1)
    )
    if row is None:
        return GateState()
    return GateState(event_type=RC(row.event_type), lap_number=row.lap_number)


def record_event(
    db: DBSession,
    session_id: int,
    event_type: RC,
    lap_number: int | None,
    message: str | None = None,
) -> m.RaceControlEvent:
    event = m.RaceControlEvent(
        session_id=session_id,
        event_type=event_type.value,
        lap_number=lap_number,
        message=(message or "")[:512] or None,
    )
    db.add(event)
    db.flush()
    return event


def state_at_lap(db: DBSession, session_id: int, lap_number: int) -> GateState:
    """The state that applied at a given lap -- used when replaying a session."""
    row = db.scalar(
        select(m.RaceControlEvent)
        .where(
            m.RaceControlEvent.session_id == session_id,
            m.RaceControlEvent.lap_number.isnot(None),
            m.RaceControlEvent.lap_number <= lap_number,
        )
        .order_by(m.RaceControlEvent.lap_number.desc(), m.RaceControlEvent.id.desc())
        .limit(1)
    )
    if row is None:
        return GateState()
    return GateState(event_type=RC(row.event_type), lap_number=row.lap_number)
