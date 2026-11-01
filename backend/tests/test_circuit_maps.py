"""Each circuit's real surroundings, from OpenStreetMap, in the feed's frame."""
from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from app.data import circuits
from app.main import app
from app.training import jobs
from scripts import build_circuit_maps as builder


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
