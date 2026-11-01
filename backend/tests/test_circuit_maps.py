"""Each circuit's real surroundings, from OpenStreetMap, in the feed's frame."""
from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from app.data import circuits
from app.main import app
from app.training import jobs
from app.data.circuits import build as builder


def test_every_committed_map_has_the_layers_the_page_draws():
    names = circuits.available()
    assert "silverstone" in names
    for name in names:
        data = circuits.find(name)
        assert data["attribution"].startswith("©")
        assert data["fit"]["median_miss_m"] < 6, name   # laid onto the racing line
        assert set(data["layers"]) >= {"grass", "gravel", "stand", "building", "road", "tarmac", "pitlane"}
        assert data["layers"]["grass"], name


def test_a_circuit_is_found_by_any_reasonable_name():
    assert circuits.find("Mexico City")["circuit"] == "Mexico City"
    assert circuits.find("mexico-city")["circuit"] == "Mexico City"
    assert circuits.find("Silverstone Circuit")["circuit"] == "Silverstone"
    assert circuits.find("Nowhere Raceway") is None


def test_the_api_serves_a_map_and_says_when_there_is_none():
    client = TestClient(app)
    answer = client.get("/circuits/Silverstone/map")
    assert answer.status_code == 200
    assert answer.json()["layers"]["stand"]
    assert client.get("/circuits/Nowhere/map").status_code == 404


def test_alignment_recovers_a_known_rotation_and_shift():
    # A lopsided loop, turned 40 degrees and moved: ICP should lay it back.
    t = np.linspace(0, 2 * np.pi, 300, endpoint=False)
    line = np.column_stack((300 * np.cos(t) + 60 * np.cos(3 * t), 160 * np.sin(t)))
    th = np.radians(40)
    r = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    osm = (r @ line.T).T + (500, -200)
    mirror, s, rot, shift, miss = builder.align(line, builder.densify(np.vstack([osm, osm[:1]]), 2.0))
    assert not mirror
    assert miss < 1.5
    assert abs(s - 1) < 0.02
    assert np.allclose(rot, r, atol=0.02)


def test_closed_rings_survive_simplification():
    square = np.array([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], dtype=float)
    out = builder.simplify(square, 0.5)
    assert len(out) >= 4 and np.allclose(out[0], out[-1])


def test_a_job_reports_the_detail_of_its_latest_stage():
    record = jobs.JobRecord(id="x", kind="replay", session_id=None)
    report = jobs.ProgressReporter(record)
    report("locations", {"car": 44, "done": 3, "of": 22})
    assert record.as_dict()["detail"] == {"car": 44, "done": 3, "of": 22}
    assert record.as_dict()["stages"] == ["locations"]


def test_every_venue_on_the_calendar_can_be_found_without_asking_the_internet():
    for name in ("Melbourne", "Shanghai", "Suzuka", "Sakhir", "Jeddah", "Miami", "Montreal",
                 "Monte Carlo", "Catalunya", "Spielberg", "Silverstone", "Spa-Francorchamps",
                 "Hungaroring", "Zandvoort", "Monza", "Madring", "Baku", "Singapore", "Austin",
                 "Mexico City", "Interlagos", "Las Vegas", "Lusail", "Yas Marina"):
        lat, lon = builder.locate(name)
        assert -60 < lat < 70 and -180 <= lon <= 180, name


def test_the_download_is_sized_from_the_racing_line():
    small = {"bounds": {"min_x": 0, "max_x": 10_000, "min_y": 0, "max_y": 5_000}}     # 1 km
    big = {"bounds": {"min_x": 0, "max_x": 40_000, "min_y": 0, "max_y": 20_000}}      # 4 km
    s = builder.bbox_for(small, 52.0, -1.0)
    b = builder.bbox_for(big, 52.0, -1.0)
    assert (b[2] - b[0]) > 2 * (s[2] - s[0])
    assert s[0] < 52.0 < s[2] and s[1] < -1.0 < s[3]


def test_too_big_an_area_is_split_into_quarters(monkeypatch, tmp_path):
    import urllib.error

    monkeypatch.setattr(builder, "CACHE", tmp_path)
    asked = []

    def fake_get(url, timeout=180):
        asked.append(url)
        if len(asked) == 1:
            raise urllib.error.HTTPError(url, 400, "too many nodes", None, None)
        return b"<osm></osm>"

    monkeypatch.setattr(builder, "_get", fake_get)
    docs = builder.fetch("test", (0.0, 0.0, 1.0, 1.0))
    assert len(docs) == 4 and len(asked) == 5


def test_a_mapped_circuit_is_not_mapped_again():
    bundle = {"session": {"circuit": "Silverstone"}, "track": {"path": [[0, 0]]}}
    assert builder.ensure_map(bundle) is None


def test_an_unmapped_circuit_with_a_replay_is_mapped_on_demand(monkeypatch):
    from app.api import replays as replays_api
    from app.data.circuits import build as circuit_builder

    built = {}
    monkeypatch.setattr(replays_api, "_bundle_at", lambda circuit, db: {"session": {"circuit": circuit}})
    monkeypatch.setattr(circuit_builder, "ensure_map", lambda bundle: built.update(b=bundle))
    # Run the job inline: the shared pool may already be shut down by now.
    monkeypatch.setattr(replays_api.jobs, "submit",
                        lambda kind, sid, fn: fn(jobs.ProgressReporter(jobs.JobRecord(id="t", kind=kind, session_id=None))))
    client = TestClient(app)
    answer = client.get("/circuits/Atlantis/map")
    assert answer.status_code == 202 and answer.json()["status"] == "mapping"
    assert built["b"]["session"]["circuit"] == "Atlantis"
    assert not replays_api._mapping   # free to try again if it failed
