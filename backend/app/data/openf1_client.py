"""OpenF1 access -- live session state.

Read-only REST polling against https://api.openf1.org/v1 (no API key). The same
client works against a completed session's historical window, which is how the
live loops are tested out of season: pass a real ``session_key`` from last
weekend and the loop replays it.

Schema note (guide section 7): 2026 replaces DRS with Manual Override. The field
name for that is not stable across the schema's evolution, so
:meth:`OpenF1Client.detect_override_field` probes the live payload for a
plausible field instead of hardcoding one, and callers degrade gracefully when
it is absent.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

#: Candidate field names for the Manual Override flag, in preference order.
#: ``drs`` stays last: on a pre-2026 session key it is the equivalent signal.
OVERRIDE_FIELD_CANDIDATES = ("manual_override", "override", "mo_active", "drs")

#: Candidate field names for the active-aero mode (Z-mode / X-mode) split.
AERO_MODE_FIELD_CANDIDATES = ("aero_mode", "active_aero", "wing_mode")


#: OpenF1 is a free, unauthenticated API and rate-limits accordingly. A live
#: tick fans out over several endpoints, so requests are spaced client-side
#: rather than discovering the limit through 429s mid-session.
MIN_REQUEST_INTERVAL_S = 0.35
MAX_RETRIES_ON_THROTTLE = 3


class OpenF1Error(RuntimeError):
    pass


@dataclass
class LiveDriverState:
    """Current on-track state for one car, assembled from several endpoints."""

    driver_number: int
    driver_id: str | None = None
    position: int | None = None
    gap_to_leader_s: float | None = None
    interval_s: float | None = None
    lap_number: int | None = None
    last_lap_time_s: float | None = None
    compound: str | None = None
    tyre_age_laps: int | None = None
    stint_number: int | None = None
    speed_trap_kph: float | None = None
    manual_override_active: bool | None = None
    aero_mode: str | None = None


class OpenF1Client:
    """Synchronous OpenF1 client, safe to call from polling threads."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or settings.OPENF1_BASE_URL).rstrip("/")
        self._client = httpx.Client(
            timeout=timeout or settings.OPENF1_TIMEOUT_S,
            headers={"user-agent": "f1-prediction-dashboard/1.0"},
        )
        self._override_field: str | None = None
        self._aero_field: str | None = None
        self._throttle_lock = threading.Lock()
        self._last_request_at = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenF1Client":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------
    def _throttle(self) -> None:
        """Space requests out so a polling tick does not trip the rate limiter."""
        with self._throttle_lock:
            wait = MIN_REQUEST_INTERVAL_S - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _get(self, endpoint: str, **params: Any) -> list[dict[str, Any]]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        clean = {k: v for k, v in params.items() if v is not None}

        for attempt in range(MAX_RETRIES_ON_THROTTLE):
            self._throttle()
            try:
                response = self._client.get(url, params=clean)
            except httpx.HTTPError as exc:
                raise OpenF1Error(f"GET {endpoint} failed: {exc}") from exc

            # OpenF1 answers 404 for a feed that has no rows for this session --
            # `intervals` during qualifying, for instance. That is an empty
            # result, not a failure, and must not stall a live loop.
            if response.status_code == 404:
                log.debug("no %s data for %s", endpoint, clean)
                return []

            if response.status_code == 429:
                # Honour Retry-After when offered, otherwise back off linearly.
                delay = _retry_after_seconds(response) or (attempt + 1) * 1.5
                log.info("openf1 throttled on %s; retrying in %.1fs", endpoint, delay)
                time.sleep(delay)
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise OpenF1Error(f"GET {endpoint} failed: {exc}") from exc

            payload = response.json()
            if not isinstance(payload, list):
                raise OpenF1Error(f"GET {endpoint} returned {type(payload).__name__}, expected list")
            return payload

        raise OpenF1Error(f"GET {endpoint} still rate-limited after {MAX_RETRIES_ON_THROTTLE} attempts")

    # -- sessions ----------------------------------------------------------
    def sessions(self, year: int | None = None, **filters: Any) -> list[dict[str, Any]]:
        return self._get("sessions", year=year or settings.CURRENT_SEASON, **filters)

    def latest_session(self) -> dict[str, Any] | None:
        rows = self._get("sessions", session_key="latest")
        return rows[0] if rows else None

    def find_session_key(self, year: int, country_or_circuit: str, session_name: str) -> int | None:
        """Resolve an OpenF1 ``session_key`` from human-readable identifiers."""
        for row in self.sessions(year=year, session_name=session_name):
            haystack = " ".join(
                str(row.get(k, "")) for k in ("country_name", "circuit_short_name", "location")
            ).lower()
            if country_or_circuit.lower() in haystack:
                return int(row["session_key"])
        return None

    def drivers(self, session_key: int) -> list[dict[str, Any]]:
        return self._get("drivers", session_key=session_key)

    # -- live state --------------------------------------------------------
    def position(self, session_key: int, **filters: Any) -> list[dict[str, Any]]:
        return self._get("position", session_key=session_key, **filters)

    def intervals(self, session_key: int, **filters: Any) -> list[dict[str, Any]]:
        return self._get("intervals", session_key=session_key, **filters)

    def laps(self, session_key: int, **filters: Any) -> list[dict[str, Any]]:
        return self._get("laps", session_key=session_key, **filters)

    def stints(self, session_key: int, **filters: Any) -> list[dict[str, Any]]:
        return self._get("stints", session_key=session_key, **filters)

    def car_data(self, session_key: int, **filters: Any) -> list[dict[str, Any]]:
        return self._get("car_data", session_key=session_key, **filters)

    def pit(self, session_key: int, **filters: Any) -> list[dict[str, Any]]:
        return self._get("pit", session_key=session_key, **filters)

    def location(self, session_key: int, driver_number: int, **filters: Any) -> list[dict[str, Any]]:
        """Track position samples (~3.7 Hz) for one car.

        Always scoped to a single driver: an unfiltered query spans the whole
        field for the whole session and is refused.
        """
        return self._get(
            "location", session_key=session_key, driver_number=driver_number, **filters
        )

    def weather(self, session_key: int) -> dict[str, Any] | None:
        rows = self._get("weather", session_key=session_key)
        return rows[-1] if rows else None

    def race_control(self, session_key: int, **filters: Any) -> list[dict[str, Any]]:
        return self._get("race_control", session_key=session_key, **filters)

    # -- schema probing ----------------------------------------------------
    def _car_data_sample(self, session_key: int) -> list[dict[str, Any]]:
        """One car_data row, for schema probing.

        ``car_data`` is per-sample telemetry and OpenF1 rejects an unfiltered
        query (422) because the result would be enormous, so the probe is scoped
        to a single car.
        """
        drivers = self.drivers(session_key)
        if not drivers:
            return []
        number = drivers[0].get("driver_number")
        if number is None:
            return []
        return self.car_data(session_key, driver_number=number)[:1]

    def detect_override_field(self, session_key: int) -> str | None:
        """Find the field carrying the Manual Override flag in this schema.

        Cached per client instance. Returns ``None`` when no candidate is
        present, in which case the feature degrades to its fallback rather than
        the loop crashing.
        """
        if self._override_field is not None:
            return self._override_field or None
        sample = self._car_data_sample(session_key)
        if not sample:
            return None
        keys = set(sample[0])
        self._override_field = next((c for c in OVERRIDE_FIELD_CANDIDATES if c in keys), "")
        if not self._override_field:
            log.warning(
                "no Manual Override field found in car_data for session %s; "
                "override features fall back to neutral (checked: %s)",
                session_key,
                ", ".join(OVERRIDE_FIELD_CANDIDATES),
            )
        return self._override_field or None

    def detect_aero_mode_field(self, session_key: int) -> str | None:
        """Active-aero mode field, if this schema exposes one (extended feature)."""
        if self._aero_field is not None:
            return self._aero_field or None
        sample = self._car_data_sample(session_key)
        if not sample:
            return None
        keys = set(sample[0])
        self._aero_field = next((c for c in AERO_MODE_FIELD_CANDIDATES if c in keys), "")
        return self._aero_field or None

    # -- assembled snapshot ------------------------------------------------
    def live_state(
        self, session_key: int, *, include_telemetry: bool = False
    ) -> dict[int, LiveDriverState]:
        """One state object per car, merged from position/interval/lap/stint feeds.

        ``include_telemetry`` additionally pulls per-car ``car_data`` for the
        Manual Override flag. It is off by default: that endpoint is one request
        per car per tick, which the rate limiter will not tolerate at live
        cadence, and the feature falls back to neutral without it.
        """
        states: dict[int, LiveDriverState] = {}

        def state_for(number: Any) -> LiveDriverState | None:
            if number is None:
                return None
            try:
                num = int(number)
            except (TypeError, ValueError):
                return None
            return states.setdefault(num, LiveDriverState(driver_number=num))

        for row in self.drivers(session_key):
            st = state_for(row.get("driver_number"))
            if st is not None:
                st.driver_id = row.get("name_acronym") or st.driver_id

        # Feeds are append-only; the last row per driver is the current value.
        for row in self.position(session_key):
            st = state_for(row.get("driver_number"))
            if st is not None and row.get("position") is not None:
                st.position = int(row["position"])

        for row in self.intervals(session_key):
            st = state_for(row.get("driver_number"))
            if st is None:
                continue
            st.gap_to_leader_s = _as_float(row.get("gap_to_leader"))
            st.interval_s = _as_float(row.get("interval"))

        for row in self.laps(session_key):
            st = state_for(row.get("driver_number"))
            if st is None:
                continue
            lap = row.get("lap_number")
            if lap is not None and (st.lap_number is None or int(lap) >= st.lap_number):
                st.lap_number = int(lap)
                st.last_lap_time_s = _as_float(row.get("lap_duration")) or st.last_lap_time_s
                st.speed_trap_kph = _as_float(row.get("st_speed")) or st.speed_trap_kph

        for row in self.stints(session_key):
            st = state_for(row.get("driver_number"))
            if st is None:
                continue
            stint_no = row.get("stint_number")
            if stint_no is not None and (st.stint_number is None or int(stint_no) >= st.stint_number):
                st.stint_number = int(stint_no)
                st.compound = row.get("compound") or st.compound
                start_age = row.get("tyre_age_at_start") or 0
                lap_start = row.get("lap_start")
                if st.lap_number is not None and lap_start is not None:
                    st.tyre_age_laps = int(start_age) + max(0, st.lap_number - int(lap_start))

        if include_telemetry:
            self._merge_telemetry(session_key, states)

        return states

    def _merge_telemetry(self, session_key: int, states: dict[int, LiveDriverState]) -> None:
        """Attach Manual Override / active-aero state, one query per car."""
        override_field = self.detect_override_field(session_key)
        aero_field = self.detect_aero_mode_field(session_key)
        if not (override_field or aero_field):
            return
        for number, st in states.items():
            rows = self.car_data(session_key, driver_number=number)
            if not rows:
                continue
            latest = rows[-1]
            if override_field and latest.get(override_field) is not None:
                st.manual_override_active = bool(latest[override_field])
            if aero_field and latest.get(aero_field) is not None:
                st.aero_mode = str(latest[aero_field])

    def current_lap(self, session_key: int) -> int | None:
        """Highest lap number seen across the field."""
        laps = [int(r["lap_number"]) for r in self.laps(session_key) if r.get("lap_number")]
        return max(laps) if laps else None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f
