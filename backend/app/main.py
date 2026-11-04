"""FastAPI application: assembly only.

Rule 1 is enforced at the routing layer -- ``POST /sessions/{id}/retrain``
hands the work to a background thread and returns a job record immediately --
but the routes themselves live in :mod:`app.api`, grouped by what they are
about. What is left here is the application itself: how it starts, how it
stops, who may call it, and where the dashboard is served from.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
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

api.register(app)


# -- public demo: read-only ----------------------------------------------------

READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def read_only_in_public(request: Request, call_next):
    """Refuse every write on a public deployment, unless the owner's token is on it.

    One rule at the edge rather than a check in each endpoint: a new POST
    route is closed to the public the day it is written, not when someone
    remembers to guard it.
    """
    if settings.PUBLIC_MODE and request.method not in READ_ONLY_METHODS:
        supplied = request.headers.get("x-admin-token", "")
        if not (settings.ADMIN_TOKEN and hmac.compare_digest(supplied, settings.ADMIN_TOKEN)):
            return JSONResponse(
                status_code=403,
                content={"detail": {
                    "reason": "read_only",
                    "message": "This is a read-only public demo: races already built can be "
                               "watched, nothing can be built or changed.",
                }},
            )
    return await call_next(request)


# Registered after the read-only guard on purpose: Starlette makes the last
# middleware added the outermost, so CORS wraps the guard and its 403 still
# carries Access-Control-Allow-Origin. The other way round, a page on another
# origin (Vercel) could not read the refusal and saw only "Failed to fetch".
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/app-config", include_in_schema=False)
def app_config() -> dict:
    """What the page needs to know about this deployment before it draws."""
    return {"public": settings.PUBLIC_MODE}


# -- dashboard ---------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        # A redirect, not the file: the page links its stylesheet and modules
        # relatively, and they are only served under /static/.
        return RedirectResponse("/static/dashboard.html")
