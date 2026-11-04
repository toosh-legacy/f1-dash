"""F1's own live timing feed, read with an F1 TV subscription token.

OpenF1 serves a running session only to paying supporters; without one, every
request during a session answers 401. F1's live timing hub
(``livetiming.formula1.com/signalrcore``) carries the same data first-hand.
Anonymously it carries timing, race control and the driver list. Car telemetry
(``CarData.z``) and car positions (``Position.z``) are sent only to a
connection that carries an F1 TV subscriber's token.

This module keeps one connection to that hub open for the life of the process
and buffers what it hears. It answers in OpenF1's shapes -- the same
``sessions``, ``drivers``, ``laps``, ``race_control``, ``location`` and
``car_data`` rows -- so :class:`app.data.openf1_client.OpenF1Client` can turn
to it when OpenF1 is locked, and nothing above the client knows the difference.

The token lives inside the ``login-session`` cookie formula1.com sets after
signing in. Copy the whole cookie value into ``backend/.env`` as
``F1TV_LOGIN_SESSION``. It lasts a few days, so copy it again before a race
weekend; ``GET /live/status`` shows when it runs out.

F1 TV is licensed for personal use: keep a server that uses this private.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
import urllib.parse
import zlib
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

HUB = "livetiming.formula1.com/signalrcore"
RS = "\x1e"  # SignalR Core message separator

TOPICS = [
    "Heartbeat",
    "SessionInfo",
    "DriverList",
    "TimingData",
    "TimingAppData",
    "LapCount",
    "TrackStatus",
    "RaceControlMessages",
    "CarData.z",
    "Position.z",
]

#: Samples kept per stream. Positions feed the live map, which only re-reads
#: the last few seconds; car data feeds the probe, which asks for two-minute
#: windows around the playhead. After the session OpenF1 opens up again and
#: the replay build fetches the whole thing from there.
POSITION_KEEP_S = 15 * 60
CAR_DATA_KEEP_S = 30 * 60

PING_S = 10.0
RECONNECT_MAX_S = 60.0

#: CarData.z channel numbers, as OpenF1 names them.
CHANNELS = {"0": "rpm", "2": "speed", "3": "n_gear", "4": "throttle", "5": "brake", "45": "drs"}


# --------------------------------------------------------------------------
# The token
# --------------------------------------------------------------------------
def subscription_token(raw: str | None = None) -> str | None:
    """The ``subscriptionToken`` out of a ``login-session`` cookie value.

    Accepts the cookie as copied (URL-encoded JSON), already decoded JSON, or
    the bare token itself.
    """
    raw = (raw if raw is not None else settings.F1TV_LOGIN_SESSION).strip().strip("'\"")
    if not raw:
        return None
    if raw.startswith("eyJ"):
        return raw
    decoded = raw
    for _ in range(3):
        if decoded.lstrip().startswith("{"):
            break
        decoded = urllib.parse.unquote(decoded)
    try:
        token = json.loads(decoded)["data"]["subscriptionToken"]
    except (ValueError, KeyError, TypeError):
        log.warning("F1TV_LOGIN_SESSION is set but holds no subscriptionToken")
        return None
    return str(token) if token else None


def token_claims(token: str | None) -> dict[str, Any]:
    """The parts of the token worth showing: product, status, expiry. Never the token."""
    if not token:
        return {}
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError):
        return {}
    exp = claims.get("exp")
    return {
        "product": claims.get("SubscribedProduct"),
        "status": claims.get("SubscriptionStatus"),
        "expires_at": datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat() if exp else None,
        "expired": bool(exp) and time.time() >= int(exp),
    }


# --------------------------------------------------------------------------
# Shapes: F1's feed into OpenF1's rows
# --------------------------------------------------------------------------
def _iso(moment: str | None) -> str | None:
    """F1 writes ``2026-11-01T22:28:39.6391559Z``; Python wants at most six digits."""
    if not moment:
        return None
    text = str(moment).rstrip("Z")
    if "." in text:
        head, frac = text.split(".", 1)
        text = f"{head}.{frac[:6]}"
    return text + "+00:00"


def _epoch(moment: str | None) -> float | None:
    iso = _iso(moment)
    if iso is None:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _inflate(payload: str) -> Any:
    return json.loads(zlib.decompress(base64.b64decode(payload), -zlib.MAX_WBITS))


def _lap_seconds(text: Any) -> float | None:
    """``"1:20.832"`` or ``"80.832"`` as seconds."""
    if isinstance(text, dict):
        text = text.get("Value")
    if not text:
        return None
    try:
        if ":" in str(text):
            minutes, seconds = str(text).split(":", 1)
            return int(minutes) * 60 + float(seconds)
        return float(text)
    except ValueError:
        return None


def _merge(into: dict[str, Any], update: dict[str, Any]) -> None:
    """Apply one of the feed's partial updates to the state it patches.

    The feed sends a list whole in a topic's first state, then patches it as
    a dict keyed by index (``"Stints": {"2": {...}}``), so lists are kept as
    index-keyed dicts to take those patches.
    """
    for key, value in update.items():
        if isinstance(value, list):
            value = {str(i): item for i, item in enumerate(value)}
        current = into.get(key)
        if isinstance(current, list):
            current = into[key] = {str(i): item for i, item in enumerate(current)}
        if isinstance(value, dict) and isinstance(current, dict):
            _merge(current, value)
        else:
            into[key] = value


# --------------------------------------------------------------------------
# The hub
# --------------------------------------------------------------------------
class LiveHub:
    """One connection to F1's live timing, buffered, answering as OpenF1 would."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = False
        self.error: str | None = None
        self.last_message_at: float | None = None
        self._reset()

    def _reset(self) -> None:
        self.session_info: dict[str, Any] = {}
        self.driver_list: dict[str, Any] = {}
        self.timing: dict[str, Any] = {}
        self.timing_app: dict[str, Any] = {}
        self.lap_count: dict[str, Any] = {}
        self.track_status: dict[str, Any] = {}
        self.control: list[dict[str, Any]] = []
        self.laps: list[dict[str, Any]] = []
        self.positions: deque[dict[str, Any]] = deque()
        self.car_data: deque[dict[str, Any]] = deque()
        self._laps_seen: dict[str, int] = {}

    # -- lifecycle -------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return subscription_token() is not None

    def start(self) -> None:
        if not self.configured or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="f1-live", daemon=True)
        self._thread.start()
        log.info("F1 live timing: connecting with an F1 TV token")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                asyncio.run(self._session())
                backoff = 2.0
            except Exception as exc:  # noqa: BLE001 - the connection must come back
                self.error = f"{type(exc).__name__}: {exc}"
                log.warning("F1 live timing dropped: %s", self.error)
            self.connected = False
            self._stop.wait(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)

    async def _session(self) -> None:
        import httpx
        import websockets

        token = subscription_token()
        if not token:
            raise RuntimeError("no F1 TV token")
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=15) as http:
            negotiated = await http.post(f"https://{HUB}/negotiate?negotiateVersion=1", headers=headers)
            negotiated.raise_for_status()
            connection = negotiated.json()["connectionToken"]
            cookies = "; ".join(f"{k}={v}" for k, v in negotiated.cookies.items())
        if cookies:
            headers["Cookie"] = cookies

        async with websockets.connect(
            f"wss://{HUB}?id={connection}", additional_headers=headers, max_size=None
        ) as ws:
            await ws.send(json.dumps({"protocol": "json", "version": 1}) + RS)
            await ws.send(json.dumps(
                {"type": 1, "invocationId": "1", "target": "Subscribe", "arguments": [TOPICS]}
            ) + RS)
            self.connected, self.error = True, None
            pinger = asyncio.create_task(self._ping(ws))
            try:
                while not self._stop.is_set():
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=60)
                    except asyncio.TimeoutError:
                        raise RuntimeError("no message from the hub for a minute")
                    for part in str(message).split(RS):
                        if part:
                            self._handle(json.loads(part))
            finally:
                pinger.cancel()

    async def _ping(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(PING_S)
            await ws.send(json.dumps({"type": 6}) + RS)

    # -- what the hub says -----------------------------------------------------
    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == 3:  # the answer to Subscribe: every topic's current state
            if message.get("error"):
                raise RuntimeError(f"subscribe refused: {message['error']}")
            now = datetime.now(timezone.utc).isoformat()
            for topic, data in (message.get("result") or {}).items():
                self.ingest(topic, data, now, initial=True)
        elif kind == 1 and message.get("target") == "feed":
            args = message.get("arguments") or []
            if len(args) >= 2:
                self.ingest(args[0], args[1], args[2] if len(args) > 2 else None)
        elif kind == 7:  # close
            raise RuntimeError(f"hub closed the connection: {message.get('error')}")

    def ingest(self, topic: str, data: Any, at: str | None, *, initial: bool = False) -> None:
        """Fold one message into the buffers. Public so tests can drive it."""
        self.last_message_at = time.time()
        if topic.endswith(".z") and isinstance(data, str):
            data = _inflate(data)
            topic = topic[:-2]
        if not data:
            return
        with self._lock:
            if topic == "SessionInfo":
                if initial or data.get("Key") not in (None, self.session_info.get("Key")):
                    if self.session_info.get("Key") not in (None, data.get("Key")):
                        self._reset()  # a new session: drop the last one's buffers
                    self.session_info = dict(data)
                else:
                    _merge(self.session_info, data)
            elif topic == "DriverList":
                _merge(self.driver_list, data)
            elif topic == "LapCount":
                _merge(self.lap_count, data)
            elif topic == "TrackStatus":
                _merge(self.track_status, data)
            elif topic == "RaceControlMessages":
                messages = data.get("Messages")
                rows = messages.values() if isinstance(messages, dict) else (messages or [])
                self.control.extend(self._control_row(row) for row in rows)
            elif topic == "TimingAppData":
                _merge(self.timing_app, data)
            elif topic == "TimingData":
                self._timing(data, at, initial)
            elif topic == "CarData":
                self._car_data(data)
            elif topic == "Position":
                self._positions(data)

    def _timing(self, data: dict[str, Any], at: str | None, initial: bool) -> None:
        _merge(self.timing, data)
        moment = _epoch(at) or time.time()
        for number, line in (data.get("Lines") or {}).items():
            if not isinstance(line, dict) or "NumberOfLaps" not in line:
                continue
            try:
                laps = int(line["NumberOfLaps"])
            except (TypeError, ValueError):
                continue
            before = self._laps_seen.get(number)
            self._laps_seen[number] = laps
            if initial or before is None or laps <= before:
                continue
            # A lap just completed: the feed gives its time alongside the count.
            full = (self.timing.get("Lines") or {}).get(number) or {}
            duration = _lap_seconds(line.get("LastLapTime") or full.get("LastLapTime"))
            finished = datetime.fromtimestamp(moment, tz=timezone.utc)
            if duration:
                self.laps.append({
                    "driver_number": int(number),
                    "lap_number": laps,
                    "date_start": (finished - timedelta(seconds=duration)).isoformat(),
                    "lap_duration": duration,
                    "is_pit_out_lap": bool(full.get("PitOut")),
                })
            # The next lap starts now; its length is not known yet.
            self.laps.append({
                "driver_number": int(number),
                "lap_number": laps + 1,
                "date_start": finished.isoformat(),
                "lap_duration": None,
                "is_pit_out_lap": False,
            })

    def _car_data(self, data: dict[str, Any]) -> None:
        for entry in data.get("Entries") or []:
            date = _iso(entry.get("Utc"))
            for number, car in (entry.get("Cars") or {}).items():
                channels = car.get("Channels") or {}
                row = {"date": date, "driver_number": int(number)}
                for channel, name in CHANNELS.items():
                    row[name] = channels.get(channel)
                self.car_data.append(row)
        self._trim(self.car_data, CAR_DATA_KEEP_S)

    def _positions(self, data: dict[str, Any]) -> None:
        for sample in data.get("Position") or []:
            date = _iso(sample.get("Timestamp"))
            for number, spot in (sample.get("Entries") or {}).items():
                self.positions.append({
                    "date": date,
                    "driver_number": int(number),
                    "x": spot.get("X"),
                    "y": spot.get("Y"),
                    "z": spot.get("Z"),
                })
        self._trim(self.positions, POSITION_KEEP_S)

    @staticmethod
    def _trim(rows: deque[dict[str, Any]], keep_s: float) -> None:
        if not rows:
            return
        newest = _epoch_param(rows[-1]["date"]) or time.time()
        while rows and (_epoch_param(rows[0]["date"]) or 0) < newest - keep_s:
            rows.popleft()

    @staticmethod
    def _control_row(row: dict[str, Any]) -> dict[str, Any]:
        number = row.get("RacingNumber")
        return {
            "date": _iso(row.get("Utc")),
            "category": row.get("Category"),
            "flag": row.get("Flag"),
            "scope": row.get("Scope"),
            "sector": row.get("Sector"),
            "driver_number": int(number) if str(number or "").isdigit() else None,
            "lap_number": row.get("Lap"),
            "message": row.get("Message"),
        }

    # -- answering as OpenF1 ---------------------------------------------------
    @property
    def session_key(self) -> int | None:
        key = self.session_info.get("Key")
        return int(key) if key is not None else None

    def session_row(self) -> dict[str, Any] | None:
        info = self.session_info
        if not info.get("Key"):
            return None
        meeting = info.get("Meeting") or {}
        offset = _offset(info.get("GmtOffset"))

        def utc(local: str | None) -> str | None:
            if not local:
                return None
            try:
                return (datetime.fromisoformat(local) - offset).replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                return None

        start = utc(info.get("StartDate"))
        return {
            "session_key": int(info["Key"]),
            "meeting_key": meeting.get("Key"),
            "session_name": info.get("Name"),
            "session_type": info.get("Type"),
            "circuit_short_name": (meeting.get("Circuit") or {}).get("ShortName"),
            "location": meeting.get("Location"),
            "country_name": (meeting.get("Country") or {}).get("Name"),
            "year": int(start[:4]) if start else None,
            "date_start": start,
            "date_end": utc(info.get("EndDate")),
            "source": "f1-live",
        }

    def answer(self, endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        """OpenF1 rows for ``endpoint`` from the live buffers, or ``None`` when
        this hub cannot answer (not connected, another session, an endpoint it
        does not carry). ``None`` tells the caller to raise as it would have."""
        if not self.connected and self.last_message_at is None:
            return None
        endpoint = endpoint.strip("/")
        key = params.get("session_key")
        current = self.session_key
        if endpoint == "sessions":
            row = self.session_row()
            if row is None:
                return None
            if key in (None, "latest") or str(key) == str(current):
                return [row]
            return None
        if key is None or str(key) != str(current):
            return None
        with self._lock:
            if endpoint == "drivers":
                return [self._driver_row(n, d) for n, d in self.driver_list.items() if isinstance(d, dict)]
            if endpoint == "laps":
                return _filter(list(self.laps), params)
            if endpoint == "race_control":
                return list(self.control)
            if endpoint == "location":
                return _filter(list(self.positions), params)
            if endpoint == "car_data":
                return _filter(list(self.car_data), params)
            if endpoint == "position":
                return self._position_rows()
            if endpoint == "intervals":
                return self._interval_rows()
            if endpoint == "stints":
                return self._stint_rows()
        return None

    # The timing tower as OpenF1's append-only feeds, one current row per car:
    # their readers take the last row per driver as the present value.
    def _lines(self, state: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        out = []
        for number, line in (state.get("Lines") or {}).items():
            if isinstance(line, dict) and str(number).isdigit():
                out.append((int(number), line))
        return out

    def _position_rows(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        return [
            {"date": now, "driver_number": number, "position": int(line["Position"])}
            for number, line in self._lines(self.timing)
            if str(line.get("Position") or "").isdigit()
        ]

    def _interval_rows(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for number, line in self._lines(self.timing):
            ahead = line.get("IntervalToPositionAhead")
            ahead = ahead.get("Value") if isinstance(ahead, dict) else ahead
            leader = line.get("GapToLeader")
            if str(line.get("Position")) == "1":
                leader, ahead = 0.0, 0.0
            rows.append({"date": now, "driver_number": number,
                         "gap_to_leader": _gap(leader), "interval": _gap(ahead)})
        return rows

    def _stint_rows(self) -> list[dict[str, Any]]:
        laps_now = {}
        for number, line in self._lines(self.timing):
            try:
                laps_now[number] = int(line.get("NumberOfLaps") or 0)
            except (TypeError, ValueError):
                laps_now[number] = 0
        rows = []
        for number, line in self._lines(self.timing_app):
            stints = line.get("Stints") or {}
            if isinstance(stints, list):
                stints = {str(i): v for i, v in enumerate(stints)}
            for index in sorted(stints, key=lambda k: int(k) if str(k).isdigit() else 0):
                stint = stints[index]
                if not isinstance(stint, dict) or not str(index).isdigit():
                    continue
                age = int(stint.get("TotalLaps") or 0)
                start_age = int(stint.get("StartLaps") or 0)
                rows.append({
                    "driver_number": number,
                    "stint_number": int(index) + 1,
                    "compound": (stint.get("Compound") or "").upper() or None,
                    "tyre_age_at_start": start_age,
                    # Chosen so OpenF1's arithmetic (age at start + laps since
                    # the stint began) lands on the feed's own tyre age.
                    "lap_start": laps_now.get(number, 0) - (age - start_age),
                })
        return rows

    def _driver_row(self, number: str, driver: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "driver_number": int(driver.get("RacingNumber") or number),
            "name_acronym": driver.get("Tla"),
            "full_name": driver.get("FullName"),
            "broadcast_name": driver.get("BroadcastName"),
            "team_name": driver.get("TeamName"),
            "team_colour": driver.get("TeamColour"),
            "headshot_url": driver.get("HeadshotUrl"),
            "first_name": driver.get("FirstName"),
            "last_name": driver.get("LastName"),
        }

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "connected": self.connected,
            "error": self.error,
            "session": self.session_row(),
            "last_message_at": datetime.fromtimestamp(self.last_message_at, tz=timezone.utc).isoformat()
            if self.last_message_at else None,
            "buffered": {"positions": len(self.positions), "car_data": len(self.car_data),
                         "laps": len(self.laps), "race_control": len(self.control)},
            "token": token_claims(subscription_token()),
        }


def _offset(text: Any) -> timedelta:
    """``"-03:00:00"`` as a timedelta."""
    try:
        sign = -1 if str(text).startswith("-") else 1
        hours, minutes, *_ = str(text).lstrip("+-").split(":")
        return sign * timedelta(hours=int(hours), minutes=int(minutes))
    except (ValueError, AttributeError):
        return timedelta(0)


def _gap(value: Any) -> float | None:
    """``"+1.234"`` as seconds; ``"1 L"`` / ``"LAP 5"`` (lapped, or the
    leader's lap count) as no gap, which is what OpenF1 sends there."""
    try:
        return float(str(value).lstrip("+")) if value not in (None, "") else None
    except ValueError:
        return None


def _filter(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """OpenF1's ``driver_number``, ``date>`` and ``date<`` filters."""
    number = params.get("driver_number")
    after = _epoch_param(params.get("date>"))
    before = _epoch_param(params.get("date<"))
    out = []
    for row in rows:
        if number is not None and row.get("driver_number") != int(number):
            continue
        if after is not None or before is not None:
            moment = _epoch_param(row.get("date"))
            if moment is None:
                continue
            if after is not None and moment <= after:
                continue
            if before is not None and moment >= before:
                continue
        out.append(row)
    return out


def _epoch_param(value: Any) -> float | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


hub = LiveHub()
