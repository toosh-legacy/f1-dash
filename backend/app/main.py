"""FastAPI application: assembly only.

Rule 1 is enforced at the routing layer -- ``POST /sessions/{id}/retrain``
hands the work to a background thread and returns a job record immediately --
but the routes themselves live in :mod:`app.api`, grouped by what they are
about. What is left here is the application itself: how it starts, how it
stops, who may call it, and where the dashboard is served from.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import api
from app.config import settings
from app.db.database import init_db
from app.live.base import loops
from app.live.broadcast import broadcaster
from app.data.f1live import hub as f1live_hub
from app.live.director import director
from app.live.feed import feeds
from app.training import jobs

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("app.main")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Background threads publish into this loop; capture it once at startup.
    broadcaster.bind_loop(asyncio.get_running_loop())
    log.info("API ready (regime %s, season %s)", settings.CURRENT_REGS_REGIME, settings.CURRENT_SEASON)
    # F1's live timing, if an F1 TV token is configured: stands in for OpenF1
    # while a session is on and OpenF1 is closed to unpaid callers.
    f1live_hub.start()
    if settings.LIVE_AUTOSTART:
        director.start()
    try:
        yield
    finally:
        director.stop()
        f1live_hub.stop()
        feeds.stop_all()
        loops.stop_all()
        jobs.shutdown()


app = FastAPI(
    title="F1 Live Prediction Dashboard",
    version="1.0.0",
    description=(
        "Per-car live predictions for the current F1 season. Retraining is a "
        "batch job; live inference never triggers training."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

api.register(app)


# -- dashboard ---------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        # A redirect, not the file: the page links its stylesheet and modules
        # relatively, and they are only served under /static/.
        return RedirectResponse("/static/dashboard.html")
