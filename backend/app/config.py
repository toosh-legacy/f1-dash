"""Application configuration.

All tunables live here so deployment is a matter of environment variables, not code edits.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent


def _load_dotenv(path: Path) -> None:
    """Read ``KEY=VALUE`` lines from ``backend/.env`` into the environment.

    Deliberately tiny rather than a dependency: no interpolation, no export
    syntax. A variable already set in the real environment wins, so a host's
    own configuration is never overridden by a file left on disk.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_dotenv(PROJECT_ROOT / ".env")

#: Running as a Vercel function: read-only code directory, a writable /tmp
#: that does not outlive the instance, and no process that stays up between
#: requests. Everything durable then lives in the database.
SERVERLESS: bool = bool(os.getenv("VERCEL")) or os.getenv("F1_SERVERLESS", "").lower() == "true"
#: Where files that are only a cache go: the project directory locally, /tmp
#: on a function.
DATA_ROOT: Path = Path("/tmp/f1") if SERVERLESS else PROJECT_ROOT


def _database_url() -> str:
    """The database to use, and the driver to reach it with.

    ``F1_DATABASE_URL`` wins. Otherwise the variables Vercel's Neon
    integration injects are used, so attaching the database to the project is
    the whole of the setup. Providers hand out ``postgres://`` or
    ``postgresql://``, which SQLAlchemy maps to psycopg2; psycopg 3 is what is
    installed, so the scheme is rewritten to name it.
    """
    url = (
        os.getenv("F1_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or f"sqlite:///{PROJECT_ROOT / 'f1_dashboard.db'}"
    )
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class Settings:
    """Runtime settings, sourced from the environment with development-safe defaults."""

    # --- Core ---------------------------------------------------------------
    # Avoid SQLite-only features anywhere in the codebase: swapping to Postgres
    # must stay a connection-string change (guide 3).
    DATABASE_URL: str = _database_url()
    SERVERLESS: bool = SERVERLESS
    ENV: str = os.getenv("F1_ENV", "development")
    LOG_LEVEL: str = os.getenv("F1_LOG_LEVEL", "INFO")

    # --- Regulation regime --------------------------------------------------
    # Kept as data, never hardcoded at call sites (rule 6).
    CURRENT_REGS_REGIME: str = os.getenv("F1_REGS_REGIME", "2026")
    CURRENT_SEASON: int = int(os.getenv("F1_SEASON", "2026"))

    # --- Data sources -------------------------------------------------------
    FASTF1_CACHE_DIR: Path = Path(os.getenv("F1_FASTF1_CACHE", str(DATA_ROOT / ".fastf1_cache")))
    OPENF1_BASE_URL: str = os.getenv("F1_OPENF1_URL", "https://api.openf1.org/v1")
    OPENF1_TIMEOUT_S: float = float(os.getenv("F1_OPENF1_TIMEOUT", "10"))
    # A paid OpenF1 account. Without one the API works between sessions only:
    # while a session is running OpenF1 refuses every unauthenticated request
    # (401), past sessions included. The username and password are exchanged
    # for an hour-long bearer token server-side; neither ever reaches a browser.
    OPENF1_USERNAME: str = os.getenv("F1_OPENF1_USERNAME", "").strip()
    OPENF1_PASSWORD: str = os.getenv("F1_OPENF1_PASSWORD", "").strip()
    OPENF1_TOKEN_URL: str = os.getenv("F1_OPENF1_TOKEN_URL", "https://api.openf1.org/token")
    # Requests per rolling minute, shared by every client in the process. OpenF1
    # allows 30/min free and 60/min to sponsors; both defaults leave headroom.
    OPENF1_MAX_RPM: int = int(os.getenv("F1_OPENF1_MAX_RPM", "0")) or (
        55 if os.getenv("F1_OPENF1_USERNAME", "").strip() else 28
    )

    # F1 TV subscriber login: the whole ``login-session`` cookie formula1.com
    # sets after signing in. Unlocks F1's own live timing (car telemetry and
    # positions included) and its archive of past sessions. Lasts a few days.
    F1TV_LOGIN_SESSION: str = os.getenv("F1TV_LOGIN_SESSION", "").strip()
    # Where the raw F1 archive is backed up to (scripts/backup_f1_archive.py).
    F1_ARCHIVE_DIR: Path = Path(os.getenv("F1_ARCHIVE_DIR", str(DATA_ROOT / "archive")))

    @property
    def openf1_authenticated(self) -> bool:
        return bool(self.OPENF1_USERNAME and self.OPENF1_PASSWORD)

    # --- Live sessions ------------------------------------------------------
    # Watch OpenF1 for a session going live and start following it unprompted:
    # the loops, the position feed, and the replay build once it has finished.
    # Never on a function: nothing stays up between requests to watch.
    LIVE_AUTOSTART: bool = os.getenv(
        "F1_LIVE_AUTOSTART", "false" if SERVERLESS else "true"
    ).lower() == "true"
    LIVE_WATCH_INTERVAL_S: float = float(os.getenv("F1_LIVE_WATCH_INTERVAL", "60"))
    # How often car positions are pulled while a session runs. OpenF1 itself
    # lags the track by about three seconds, so faster than this buys nothing.
    LIVE_POSITION_INTERVAL_S: float = float(os.getenv("F1_LIVE_POSITION_INTERVAL", "4"))
    # Laps and race control, for the timing tower and the flags.
    LIVE_TIMING_INTERVAL_S: float = float(os.getenv("F1_LIVE_TIMING_INTERVAL", "20"))

    # --- Live loop cadence --------------------------------------------------
    # Each race tick is five requests; at 10 s that is 30 a minute, which with
    # the position feed stays inside a sponsor account's 60.
    RACE_POLL_INTERVAL_S: float = float(os.getenv("F1_RACE_POLL_INTERVAL", "10"))
    QUALIFYING_POLL_INTERVAL_S: float = float(os.getenv("F1_QUALI_POLL_INTERVAL", "10"))
    RACE_CONTROL_POLL_INTERVAL_S: float = float(os.getenv("F1_RC_POLL_INTERVAL", "2"))

    # --- Model registry -----------------------------------------------------
    MODEL_ARTIFACT_DIR: Path = Path(os.getenv("F1_MODEL_DIR", str(DATA_ROOT / "model_artifacts")))
    # A new version must beat the active one by at least this margin to be
    # considered an improvement by the validation gate (rule 2).
    VALIDATION_MIN_IMPROVEMENT: float = float(os.getenv("F1_VALIDATION_MARGIN", "0.0"))
    # Scores above this are treated as a leakage smell, not a success (M3).
    LEAKAGE_SUSPICION_SCORE: float = float(os.getenv("F1_LEAKAGE_SCORE", "0.98"))
    AUTO_PROMOTE: bool = os.getenv("F1_AUTO_PROMOTE", "false").lower() == "true"

    # --- Replays ------------------------------------------------------------
    # Built replay bundles are large and immutable, so they live on disk rather
    # than in the database and are served straight from the cache.
    REPLAY_DIR: Path = Path(os.getenv("F1_REPLAY_DIR", str(DATA_ROOT / "replays")))
    # Playback frame rate. 1 Hz keeps a two-hour race under a few MB; the
    # dashboard interpolates between frames for smooth motion.
    REPLAY_FRAME_HZ: float = float(os.getenv("F1_REPLAY_HZ", "1"))

    # --- Public demo --------------------------------------------------------
    # Off by default. When on, the API is read-only: anyone may watch what is
    # already built, nobody may build, retrain, promote or delete -- except a
    # request carrying ADMIN_TOKEN in an ``X-Admin-Token`` header. Turn it on
    # before exposing this API beyond your own machine.
    PUBLIC_MODE: bool = os.getenv("F1_PUBLIC_MODE", "false").lower() == "true"
    ADMIN_TOKEN: str = os.getenv("F1_ADMIN_TOKEN", "").strip()

    # --- API ----------------------------------------------------------------
    CORS_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv(
            "F1_CORS_ORIGINS",
            # The Node dev server, and the dashboard hosted on Vercel, which
            # calls this API on localhost from the viewer's own browser.
            "http://localhost:3000,http://127.0.0.1:3000,https://f1-dash-backend.vercel.app",
        ).split(",")
        if o.strip()
    ]

    # --- Background jobs ----------------------------------------------------
    # "thread": run in this process (a laptop, a long-lived server).
    # "github": record the job in the database and start the GitHub Actions
    # worker (.github/workflows/worker.yml) to run it -- for a function, which
    # cannot keep working after it has answered.
    JOB_RUNNER: str = os.getenv("F1_JOB_RUNNER", "github" if SERVERLESS else "thread").strip().lower()
    GITHUB_REPO: str = os.getenv("F1_GITHUB_REPO", "toosh-legacy/f1-dash").strip()
    GITHUB_WORKFLOW: str = os.getenv("F1_GITHUB_WORKFLOW", "worker.yml").strip()
    GITHUB_REF: str = os.getenv("F1_GITHUB_REF", "main").strip()
    # Fine-grained token, this repository only, Actions: read and write.
    GITHUB_DISPATCH_TOKEN: str = os.getenv("GITHUB_DISPATCH_TOKEN", "").strip()

    def ensure_dirs(self) -> None:
        self.FASTF1_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.MODEL_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        self.REPLAY_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


settings = get_settings()
