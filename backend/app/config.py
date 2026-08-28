"""Application configuration.

All tunables live here so deployment is a matter of environment variables, not code edits.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent


class Settings:
    """Runtime settings, sourced from the environment with development-safe defaults."""

    # --- Core ---------------------------------------------------------------
    # Avoid SQLite-only features anywhere in the codebase: swapping to Postgres
    # must stay a connection-string change (guide 3).
    DATABASE_URL: str = os.getenv("F1_DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'f1_dashboard.db'}")
    ENV: str = os.getenv("F1_ENV", "development")
    LOG_LEVEL: str = os.getenv("F1_LOG_LEVEL", "INFO")

    # --- Regulation regime --------------------------------------------------
    # Kept as data, never hardcoded at call sites (rule 6).
    CURRENT_REGS_REGIME: str = os.getenv("F1_REGS_REGIME", "2026")
    CURRENT_SEASON: int = int(os.getenv("F1_SEASON", "2026"))

    # --- Data sources -------------------------------------------------------
    FASTF1_CACHE_DIR: Path = Path(os.getenv("F1_FASTF1_CACHE", str(PROJECT_ROOT / ".fastf1_cache")))
    OPENF1_BASE_URL: str = os.getenv("F1_OPENF1_URL", "https://api.openf1.org/v1")
    OPENF1_TIMEOUT_S: float = float(os.getenv("F1_OPENF1_TIMEOUT", "10"))

    # --- Live loop cadence --------------------------------------------------
    RACE_POLL_INTERVAL_S: float = float(os.getenv("F1_RACE_POLL_INTERVAL", "5"))
    QUALIFYING_POLL_INTERVAL_S: float = float(os.getenv("F1_QUALI_POLL_INTERVAL", "5"))
    RACE_CONTROL_POLL_INTERVAL_S: float = float(os.getenv("F1_RC_POLL_INTERVAL", "2"))

    # --- Model registry -----------------------------------------------------
    MODEL_ARTIFACT_DIR: Path = Path(os.getenv("F1_MODEL_DIR", str(PROJECT_ROOT / "model_artifacts")))
    # A new version must beat the active one by at least this margin to be
    # considered an improvement by the validation gate (rule 2).
    VALIDATION_MIN_IMPROVEMENT: float = float(os.getenv("F1_VALIDATION_MARGIN", "0.0"))
    # Scores above this are treated as a leakage smell, not a success (M3).
    LEAKAGE_SUSPICION_SCORE: float = float(os.getenv("F1_LEAKAGE_SCORE", "0.98"))
    AUTO_PROMOTE: bool = os.getenv("F1_AUTO_PROMOTE", "false").lower() == "true"

    # --- Replays ------------------------------------------------------------
    # Built replay bundles are large and immutable, so they live on disk rather
    # than in the database and are served straight from the cache.
    REPLAY_DIR: Path = Path(os.getenv("F1_REPLAY_DIR", str(PROJECT_ROOT / "replays")))
    # Playback frame rate. 1 Hz keeps a two-hour race under a few MB; the
    # dashboard interpolates between frames for smooth motion.
    REPLAY_FRAME_HZ: float = float(os.getenv("F1_REPLAY_HZ", "1"))

    # --- API ----------------------------------------------------------------
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("F1_CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()
    ]

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
