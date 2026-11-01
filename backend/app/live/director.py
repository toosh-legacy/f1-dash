"""Following a live session without anyone pressing start.

Once a minute the director asks OpenF1 for its latest session. Inside that
session's live window it makes sure the session exists locally, then starts
everything a viewer needs: the position feed for the map, and -- for
qualifying and races -- the prediction loop. When the window closes it stops
them and queues the session's replay build, so the session that was just
watched live can be watched again, and the next session at the same circuit
has a layout to draw on.

Without an OpenF1 account a running session answers 401 to everything. The
director reports that as ``locked`` rather than as a failure: it is the one
state the dashboard should explain to the viewer.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.data.openf1_client import OpenF1Client, OpenF1Error, OpenF1Locked, auth, budget
from app.data.replay.shared import _parse_date
from app.db import models as m
from app.db.database import session_scope
from app.live.base import loops
from app.live.feed import PositionFeed, feeds, layout_for

log = logging.getLogger(__name__)

#: OpenF1 serves a session live from half an hour before it starts until half
#: an hour after it ends; the director follows a narrower slice of that.
LEAD_IN = timedelta(minutes=10)
RUN_OUT = timedelta(minutes=15)

#: While following a session, check whether it has ended this often.
FOLLOWING_INTERVAL_S = 30.0

#: OpenF1's session names, as the local calendar spells them.
SESSION_TYPES = {
    "practice 1": m.SessionType.FP1,
    "practice 2": m.SessionType.FP2,
    "practice 3": m.SessionType.FP3,
    "sprint qualifying": m.SessionType.SQ,
    "sprint shootout": m.SessionType.SQ,
    "qualifying": m.SessionType.QUALIFYING,
    "sprint": m.SessionType.SPRINT,
    "race": m.SessionType.RACE,
}


def session_type_of(name: str) -> m.SessionType | None:
    return SESSION_TYPES.get(str(name or "").strip().lower())


def live_window(row: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether ``row`` (an OpenF1 session) is running, give or take the margins."""
    start, end = _parse_date(row.get("date_start")), _parse_date(row.get("date_end"))
    if start is None:
        return False
    now = now or datetime.now(timezone.utc)
    end = end or start + timedelta(hours=2)
    return start - LEAD_IN <= now <= end + RUN_OUT


class Director:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._client: OpenF1Client | None = None
        self.following: dict[str, Any] | None = None   # {session_id, session_key, row}
        self.state = "idle"
        self.message: str | None = "not started"
        self.checked_at: str | None = None
        self.latest: dict[str, Any] | None = None

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-director", daemon=True)
        self._thread.start()
        log.info("live director watching OpenF1 every %.0fs", settings.LIVE_WATCH_INTERVAL_S)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as exc:  # the watcher must outlive any one bad check
                log.warning("live director check failed: %s", exc)
                self._set("error", f"{type(exc).__name__}: {exc}")
            self._stop.wait(FOLLOWING_INTERVAL_S if self.following else settings.LIVE_WATCH_INTERVAL_S)

    # -- one look at OpenF1 ------------------------------------------------------
    def check(self) -> None:
        self.checked_at = datetime.now(timezone.utc).isoformat()
        try:
            row = self.client().latest_session()
        except OpenF1Locked:
            # A session is on and this server may not read it.
            self._set("locked", "a session is live, but OpenF1 needs an account to show it")
            return
        except OpenF1Error as exc:
            self._set("error", str(exc))
            return
        self.latest = _brief(row) if row else None

        following = self.following
        if row and live_window(row):
            key = int(row["session_key"])
            if following is None or following["session_key"] != key:
                if following is not None:
                    self.finish()
                self.follow(row)
            else:
                self._set("live", f"following {_label(row)}")
            return

        if following is not None:
            self.finish()
        self._set("idle", f"no session live; latest was {_label(row)}" if row else "no sessions")

    def client(self) -> OpenF1Client:
        if self._client is None:
            self._client = OpenF1Client()
        return self._client

    # -- following --------------------------------------------------------------
    def follow(self, row: dict[str, Any], *, total_laps: int | None = None) -> dict[str, Any]:
        """Start everything a live session needs. Safe to call again."""
        key = int(row["session_key"])
        session_type = session_type_of(row.get("session_name"))
        with session_scope() as db:
            session = ensure_session(db, row)
            session.status = m.SessionStatus.LIVE.value
            session_id = session.id
        _seed_entries(key)

        feeds.start(PositionFeed(session_id, key))
        if session_type is not None and loops.get(session_id) is None:
            from app.live.qualifying_loop import QualifyingLoop
            from app.live.race_loop import RaceLoop

            if session_type.is_race:
                loops.start(RaceLoop(session_id, key, total_laps=total_laps))
            elif session_type.is_qualifying:
                loops.start(QualifyingLoop(session_id, key))

        with self._lock:
            self.following = {"session_id": session_id, "session_key": key, "row": _brief(row)}
        self._set("live", f"following {_label(row)}")
        log.info("following live session %s (%s) as session %s", key, _label(row), session_id)
        _ensure_layout(self.client(), row)
        return self.following

    def finish(self) -> None:
        """Stop following, mark the session done, and queue its replay."""
        with self._lock:
            following, self.following = self.following, None
        if following is None:
            return
        session_id, key = following["session_id"], following["session_key"]
        feeds.stop(session_id)
        loops.stop(session_id)
        with session_scope() as db:
            session = db.get(m.Session, session_id)
            if session is not None:
                session.status = m.SessionStatus.COMPLETED.value
        log.info("live session %s over; queueing its replay", key)
        _queue_build(key)

    def follow_latest(self) -> dict[str, Any]:
        """The live button: look now rather than waiting for the next check."""
        self.check()
        return self.status()

    # -- reporting --------------------------------------------------------------
    def _set(self, state: str, message: str | None) -> None:
        self.state, self.message = state, message

    def status(self) -> dict[str, Any]:
        following = self.following
        return {
            "state": self.state,
            "message": self.message,
            "checked_at": self.checked_at,
            "autostart": settings.LIVE_AUTOSTART,
            "running": bool(self._thread and self._thread.is_alive()),
            "following": following,
            "latest_session": self.latest,
            "openf1": {
                **auth.status(),
                "requests_last_minute": budget.used(),
                "request_limit_per_minute": settings.OPENF1_MAX_RPM,
            },
        }


# --------------------------------------------------------------------------
# The local calendar
# --------------------------------------------------------------------------
def ensure_session(db, row: dict[str, Any]) -> m.Session:
    """The local session for an OpenF1 session row, created if the calendar
    stops short of it (a seeded calendar often does)."""
    key = int(row["session_key"])
    found = db.scalar(select(m.Session).where(m.Session.openf1_session_key == key))
    if found is not None:
        return found

    session_type = session_type_of(row.get("session_name"))
    type_value = session_type.value if session_type else str(row.get("session_name") or "Session")[:16]
    start = _parse_date(row.get("date_start"))
    year = int(row.get("year") or (start.year if start else settings.CURRENT_SEASON))

    # The same session on the seeded calendar: same kind, starting within a few
    # hours. Circuit ids differ between FastF1 and OpenF1, so the time is the
    # reliable join.
    if start is not None:
        for candidate in db.scalars(
            select(m.Session).where(m.Session.year == year, m.Session.session_type == type_value)
        ):
            if candidate.start_time is None:
                continue
            begun = candidate.start_time
            if begun.tzinfo is None:
                begun = begun.replace(tzinfo=timezone.utc)
            if abs((begun - start).total_seconds()) < 6 * 3600:
                candidate.openf1_session_key = key
                db.flush()
                return candidate

    circuit = _ensure_circuit(db, row)
    existing = db.scalar(
        select(m.Session).where(
            m.Session.year == year,
            m.Session.circuit_id == circuit.id,
            m.Session.session_type == type_value,
        )
    )
    if existing is not None:
        existing.openf1_session_key = key
        existing.start_time = existing.start_time or start
        db.flush()
        return existing

    name = str(row.get("session_name") or "").lower()
    session = m.Session(
        year=year,
        circuit_id=circuit.id,
        session_type=type_value,
        regs_regime=settings.CURRENT_REGS_REGIME if year >= 2026 else str(year),
        is_sprint_weekend="sprint" in name,
        start_time=start,
        status=m.SessionStatus.SCHEDULED.value,
        openf1_session_key=key,
    )
    db.add(session)
    db.flush()
    return session


def _ensure_circuit(db, row: dict[str, Any]) -> m.Circuit:
    from app.data.fastf1_client import circuit_id_from_event
    from app.data.seed import circuit_reference_for

    location = str(row.get("location") or row.get("circuit_short_name") or "unknown")
    circuit_id = circuit_id_from_event(location, location)
    circuit = db.get(m.Circuit, circuit_id)
    if circuit is None:
        reference = circuit_reference_for(
            location, str(row.get("circuit_short_name") or ""), str(row.get("country_name") or "")
        )
        circuit = m.Circuit(
            id=circuit_id,
            name=f"{row.get('country_name') or location} Grand Prix",
            type=reference.type,
            altitude_m=reference.altitude_m,
            avg_pit_loss_s=reference.avg_pit_loss_s,
            historical_sc_rate=reference.sc_rate,
            historical_overtaking_difficulty=reference.overtaking_difficulty,
        )
        db.add(circuit)
        db.flush()
    return circuit


def _seed_entries(session_key: int) -> None:
    """Drivers and teams for this session, so predictions have someone to name."""
    from app.data import seed

    try:
        with session_scope() as db:
            seed.seed_entries_from_openf1(db, session_key)
    except Exception as exc:  # predictions degrade; the map does not need this
        log.warning("could not seed entries for %s: %s", session_key, exc)


# --------------------------------------------------------------------------
# Replays
# --------------------------------------------------------------------------
def _queue_build(session_key: int) -> None:
    from app.api.replays import build_replay

    try:
        build_replay(session_key)
    except Exception as exc:
        log.warning("could not queue the replay of %s: %s", session_key, exc)


def _ensure_layout(client: OpenF1Client, row: dict[str, Any]) -> None:
    """Give a first visit to a circuit something to draw on: if no replay at
    this track is built, build last year's race there."""
    circuit = str(row.get("circuit_short_name") or "")
    if not circuit or layout_for(circuit) is not None:
        return
    start = _parse_date(row.get("date_start"))
    year = (start.year if start else settings.CURRENT_SEASON) - 1
    try:
        earlier = client.sessions(year=year, circuit_short_name=circuit, session_name="Race")
    except OpenF1Error as exc:
        log.info("no earlier race at %s to lay out on: %s", circuit, exc)
        return
    if earlier:
        key = int(earlier[-1]["session_key"])
        log.info("no layout for %s yet; building %s race (%s) for one", circuit, year, key)
        _queue_build(key)


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_key": row.get("session_key"),
        "name": row.get("session_name"),
        "type": row.get("session_type"),
        "circuit": row.get("circuit_short_name"),
        "location": row.get("location"),
        "country": row.get("country_name"),
        "date_start": row.get("date_start"),
        "date_end": row.get("date_end"),
    }


def _label(row: dict[str, Any] | None) -> str:
    if not row:
        return "nothing"
    return f"{row.get('circuit_short_name')} {row.get('session_name')} ({row.get('session_key')})"


director = Director()
