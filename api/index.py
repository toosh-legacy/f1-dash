"""The Vercel function: the FastAPI app under ``/api``.

Vercel serves the dashboard itself from ``public/`` and rewrites every
``/api/...`` request here (see vercel.json). The app in ``backend/app`` knows
nothing about that prefix -- locally it answers ``/health`` at the root -- so
this module hands it each request with ``root_path`` set to ``/api``. That is
exactly what mounting it at ``/api`` would do, minus the part where a mounted
app never sees ASGI lifespan events.

Lifespan is not something to rely on in a function anyway, so the one thing
start-up must do -- create any missing tables -- happens here, once per cold
start, at import.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.db.database import init_db  # noqa: E402
from app.live.broadcast import broadcaster  # noqa: E402
from app.main import app as api_app  # noqa: E402

log = logging.getLogger("api.index")

PREFIX = "/api"

try:
    init_db()
except Exception as error:  # noqa: BLE001 - answer /api/health with the reason rather than crash
    log.error("creating tables failed at cold start: %s", error)


async def app(scope, receive, send):
    """ASGI entry point Vercel loads (the top-level name ``app``)."""
    if scope["type"] in ("http", "websocket"):
        # Anything a background thread publishes goes nowhere without a bound
        # loop; bind the one this instance serves requests on, once.
        if broadcaster.loop is None or broadcaster.loop.is_closed():
            import asyncio

            broadcaster.bind_loop(asyncio.get_running_loop())
        path = scope.get("path", "")
        if path == PREFIX or path.startswith(PREFIX + "/"):
            # Starlette routes on ``path`` minus ``root_path``, so the path
            # stays whole and the prefix is declared rather than cut off.
            scope = {**scope, "root_path": scope.get("root_path", "") + PREFIX}
    await api_app(scope, receive, send)
