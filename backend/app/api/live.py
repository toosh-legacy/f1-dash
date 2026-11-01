"""Live loop control, and the socket a dashboard watches a session through."""
from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session as DBSession

from app import schemas
from app.api.deps import require_session
from app.config import settings
from app.db import models as m
from app.db.database import get_db
from app.live.base import loops
from app.live.broadcast import broadcaster
from app.live.director import director
from app.live.feed import PositionFeed, feeds
from app.live.qualifying_loop import QualifyingLoop
from app.live.race_loop import RaceLoop

router = APIRouter()


@router.get("/live", response_model=list[schemas.LiveLoopOut], tags=["live"])
def list_live_loops():
    return loops.statuses() + feeds.statuses()


@router.get("/live/status", tags=["live"])
def live_status() -> dict[str, Any]:
    """Whether a live session can be watched, and if not, why not.

    The dashboard reads this to decide what its Live button says: following a
    session, nothing on, or a session on that OpenF1 will not show without an
    account.
    """
    return {**director.status(), "loops": loops.statuses(), "feeds": feeds.statuses()}


@router.post("/live/check", tags=["live"])
def live_check() -> dict[str, Any]:
    """Look at OpenF1 now rather than at the next scheduled check, and follow
    whatever is live."""
    return {**director.follow_latest(), "loops": loops.statuses(), "feeds": feeds.statuses()}


@router.get("/sessions/{session_id}/live/state", tags=["live"])
def live_state(session_id: int) -> dict[str, Any]:
    """Everything a viewer joining mid-session needs to draw it at once: the
    drivers, the circuit, the last few minutes of frames, the timing, and the
    running order of every lap so far. The socket carries it on from there."""
    feed = feeds.get(session_id)
    if feed is None or not isinstance(feed, PositionFeed):
        raise HTTPException(status_code=404, detail=f"session {session_id} is not being followed live")
    state = feed.snapshot()
    loop = loops.get(session_id)
    state["laps"] = {str(lap): order for lap, order in getattr(loop, "orders", {}).items()}
    state["loop"] = loop.status.as_dict() if loop else None
    state["feed"] = feed.status.as_dict()
    return state


@router.post("/sessions/{session_id}/live/start", response_model=schemas.LiveLoopOut, tags=["live"])
def start_live_loop(
    session_id: int, request: schemas.StartLoopRequest, db: DBSession = Depends(get_db)
):
    """Start the polling loop for a session (qualifying or race cadence)."""
    session = require_session(db, session_id)
    session_key = request.openf1_session_key or session.openf1_session_key
    if session_key is None:
        raise HTTPException(
            status_code=400,
            detail="no OpenF1 session key: pass openf1_session_key, or store one on the session",
        )
    session.openf1_session_key = session_key
    db.commit()

    session_type = m.SessionType(session.session_type)
    kwargs: dict[str, Any] = {}
    if request.poll_interval_s:
        kwargs["poll_interval_s"] = request.poll_interval_s
    if session_type.is_qualifying:
        loop = QualifyingLoop(session_id, session_key, **kwargs)
    elif session_type.is_race:
        loop = RaceLoop(session_id, session_key, total_laps=request.total_laps, **kwargs)
    else:
        raise HTTPException(
            status_code=400, detail=f"{session.session_type} has no live prediction loop"
        )
    feeds.start(PositionFeed(session_id, session_key))
    return loops.start(loop).status.as_dict()


@router.post("/sessions/{session_id}/live/stop", tags=["live"])
def stop_live_loop(session_id: int) -> dict[str, Any]:
    stopped = loops.stop(session_id)
    stopped = feeds.stop(session_id) or stopped
    if not stopped:
        raise HTTPException(status_code=404, detail=f"no live loop running for session {session_id}")
    return {"session_id": session_id, "stopped": True}


@router.websocket("/sessions/{session_id}/live")
async def live_socket(websocket: WebSocket, session_id: int) -> None:
    """One message per period boundary (qualifying) or per lap (race), plus
    immediate ``race_control`` messages on any state change."""
    await websocket.accept()
    queue = await broadcaster.subscribe(session_id)
    await websocket.send_json(
        {
            "type": "connected",
            "session_id": session_id,
            "regs_regime": settings.CURRENT_REGS_REGIME,
            "server_time": datetime.now(timezone.utc).isoformat(),
        }
    )
    try:
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    except RuntimeError:  # socket closed mid-send
        pass
    finally:
        await broadcaster.unsubscribe(session_id, queue)
        with suppress(Exception):
            await websocket.close()
