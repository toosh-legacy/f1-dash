"""F1's live timing hub, read with an F1 TV token, answering as OpenF1 would."""
from __future__ import annotations

import base64
import json
import urllib.parse
import zlib

import httpx
import pytest

from app.data import f1live, openf1_client


def _deflate(payload: dict) -> str:
    """Pack a payload the way the hub sends ``.z`` topics: raw deflate, base64."""
    packer = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = packer.compress(json.dumps(payload).encode()) + packer.flush()
    return base64.b64encode(raw).decode()


def _token(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


SESSION = {
    "Key": 11412,
    "Type": "Race",
    "Name": "Race",
    "StartDate": "2026-11-08T14:00:00",
    "EndDate": "2026-11-08T16:00:00",
    "GmtOffset": "-03:00:00",
    "Meeting": {"Key": 1299, "Location": "São Paulo", "Country": {"Name": "Brazil"},
                "Circuit": {"ShortName": "Interlagos"}},
}


@pytest.fixture()
def hub():
    live = f1live.LiveHub()
    live.connected = True
    live.ingest("SessionInfo", SESSION, "2026-11-08T17:00:00Z", initial=True)
    return live


def test_the_token_comes_out_of_the_cookie_however_it_was_copied():
    token = _token({"exp": 4102444800, "SubscribedProduct": "F1 TV Premium Monthly",
                    "SubscriptionStatus": "active"})
    cookie = urllib.parse.quote(json.dumps({"data": {"subscriptionToken": token}}))
    assert f1live.subscription_token(cookie) == token
    assert f1live.subscription_token(urllib.parse.unquote(cookie)) == token
    assert f1live.subscription_token(token) == token
    assert f1live.subscription_token("") is None
    assert f1live.subscription_token("%7B%22nope%22%3A1%7D") is None

    claims = f1live.token_claims(token)
    assert claims["product"] == "F1 TV Premium Monthly"
    assert claims["expired"] is False
    assert "subscriptionToken" not in json.dumps(claims)


def test_the_session_is_described_in_utc(hub):
    row = hub.answer("sessions", {"session_key": "latest"})[0]
    assert row["session_key"] == 11412
    assert row["circuit_short_name"] == "Interlagos"
    # 14:00 local at GMT-3 is 17:00 UTC.
    assert row["date_start"].startswith("2026-11-08T17:00:00")
    assert row["date_end"].startswith("2026-11-08T19:00:00")
    # Another session is not this hub's to answer.
    assert hub.answer("sessions", {"session_key": 999}) is None
    assert hub.answer("laps", {"session_key": 999}) is None


def test_car_data_and_positions_come_out_in_openf1_rows(hub):
    hub.ingest("CarData.z", _deflate({"Entries": [
        {"Utc": "2026-11-08T17:10:00.1234567Z",
         "Cars": {"16": {"Channels": {"0": 11000, "2": 301, "3": 8, "4": 100, "5": 0, "45": 12}}}},
        {"Utc": "2026-11-08T17:10:00.5Z",
         "Cars": {"16": {"Channels": {"0": 10500, "2": 280, "3": 7, "4": 0, "5": 100}}}},
    ]}), None)
    hub.ingest("Position.z", _deflate({"Position": [
        {"Timestamp": "2026-11-08T17:10:00.2Z",
         "Entries": {"16": {"Status": "OnTrack", "X": 120, "Y": -40, "Z": 7}}},
    ]}), None)

    rows = hub.answer("car_data", {"session_key": 11412, "driver_number": 16,
                                   "date>": "2026-11-08T17:10:00.3+00:00"})
    assert [(r["speed"], r["n_gear"], r["throttle"], r["brake"]) for r in rows] == [(280, 7, 0, 100)]

    spots = hub.answer("location", {"session_key": 11412, "date>": "2026-11-08T17:09:59+00:00"})
    assert spots == [{"date": "2026-11-08T17:10:00.2+00:00", "driver_number": 16,
                      "x": 120, "y": -40, "z": 7}]


def test_a_completed_lap_becomes_a_lap_row(hub):
    hub.ingest("TimingData", {"Lines": {"16": {"NumberOfLaps": 4, "Position": "2"}}},
               "2026-11-08T17:20:00Z", initial=True)
    hub.ingest("TimingData", {"Lines": {"16": {"NumberOfLaps": 5,
                                               "LastLapTime": {"Value": "1:12.500"}}}},
               "2026-11-08T17:21:12.5Z")
    laps = hub.answer("laps", {"session_key": 11412})
    assert laps[0]["lap_number"] == 5 and laps[0]["lap_duration"] == 72.5
    assert laps[0]["date_start"].startswith("2026-11-08T17:20:00")
    assert laps[1]["lap_number"] == 6 and laps[1]["lap_duration"] is None


def test_the_tower_answers_position_intervals_and_stints(hub):
    hub.ingest("TimingData", {"Lines": {
        "1": {"Position": "1", "NumberOfLaps": 10, "GapToLeader": "LAP 10"},
        "16": {"Position": "2", "NumberOfLaps": 10, "GapToLeader": "+1.234",
               "IntervalToPositionAhead": {"Value": "+1.234"}},
    }}, None, initial=True)
    # A list in the first state, patched by index afterwards.
    hub.ingest("TimingAppData", {"Lines": {"16": {"Stints": [
        {"Compound": "MEDIUM", "TotalLaps": 3, "StartLaps": 0},
    ]}}}, None, initial=True)
    hub.ingest("TimingAppData", {"Lines": {"16": {"Stints": {
        "1": {"Compound": "HARD", "TotalLaps": 7, "StartLaps": 2},
    }}}}, None)

    positions = {r["driver_number"]: r["position"] for r in hub.answer("position", {"session_key": 11412})}
    assert positions == {1: 1, 16: 2}
    gaps = {r["driver_number"]: (r["gap_to_leader"], r["interval"])
            for r in hub.answer("intervals", {"session_key": 11412})}
    assert gaps == {1: (0.0, 0.0), 16: (1.234, 1.234)}

    stints = hub.answer("stints", {"session_key": 11412})
    assert [s["compound"] for s in stints] == ["MEDIUM", "HARD"]
    last = stints[-1]
    # OpenF1's reader computes age as start age + laps since the stint began.
    assert last["tyre_age_at_start"] + (10 - last["lap_start"]) == 7


def test_race_control_both_shapes(hub):
    hub.ingest("RaceControlMessages", {"Messages": [
        {"Utc": "2026-11-08T17:05:00", "Category": "Flag", "Flag": "YELLOW",
         "Scope": "Sector", "Sector": 4, "Message": "YELLOW IN TRACK SECTOR 4"},
    ]}, None, initial=True)
    hub.ingest("RaceControlMessages", {"Messages": {"1": {
        "Utc": "2026-11-08T17:06:00", "Category": "SafetyCar", "Message": "SAFETY CAR DEPLOYED",
        "RacingNumber": "16", "Lap": 7}}}, None)
    rows = hub.answer("race_control", {"session_key": 11412})
    assert [r["message"] for r in rows] == ["YELLOW IN TRACK SECTOR 4", "SAFETY CAR DEPLOYED"]
    assert rows[1]["driver_number"] == 16 and rows[1]["lap_number"] == 7


def test_a_new_session_drops_the_last_ones_buffers(hub):
    hub.ingest("RaceControlMessages", {"Messages": [{"Utc": "2026-11-08T17:05:00", "Message": "x"}]}, None)
    hub.ingest("SessionInfo", {**SESSION, "Key": 11413}, None)
    assert hub.session_key == 11413
    assert hub.answer("race_control", {"session_key": 11413}) == []


def test_openf1_turns_to_the_hub_when_locked(monkeypatch, hub):
    def refuse(_request):
        return httpx.Response(401, json={"detail": "Live F1 session in progress"})

    client = openf1_client.OpenF1Client()
    client._client = httpx.Client(transport=httpx.MockTransport(refuse))
    monkeypatch.setattr(openf1_client.settings, "OPENF1_USERNAME", "")
    monkeypatch.setattr(openf1_client.settings, "OPENF1_PASSWORD", "")
    monkeypatch.setattr(openf1_client, "MIN_REQUEST_INTERVAL_S", 0, raising=False)
    monkeypatch.setattr(f1live, "hub", hub)

    assert client.latest_session()["session_key"] == 11412
    # An endpoint the hub does not carry still says why it failed.
    with pytest.raises(openf1_client.OpenF1Locked):
        client.weather(11412)
