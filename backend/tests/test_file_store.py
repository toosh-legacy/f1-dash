"""The durable file store: a file outlives the disk it was written to.

Deleting the local file between writing and reading stands in for a fresh
function instance, whose ``/tmp`` has never seen the file.
"""
from __future__ import annotations

import threading
import uuid

import pytest

from app.db import models as m
from app.features.engineering import QUALIFYING_FEATURES
from app.models import registry
from app.models.base import ValidationReport
from app.models.qualifying_model import QualifyingAdvancementModel
from app.storage import files


@pytest.fixture
def key():
    name = f"test/{uuid.uuid4().hex}/blob.bin"
    yield name
    files.delete(name)


def test_publish_then_materialize_after_the_file_is_gone(tmp_path, key):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"durable bytes")
    files.publish(path, key)

    path.unlink()
    assert files.exists(key, path)  # known to the store without a download

    target = tmp_path / "fresh" / "instance" / "blob.bin"
    assert files.materialize(key, target)
    assert target.read_bytes() == b"durable bytes"
    assert files.read_bytes(key) == b"durable bytes"
    assert not list(target.parent.glob("*.tmp"))  # the write was atomic


def test_publish_replaces_an_earlier_copy(tmp_path, key):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"first")
    files.publish(path, key)
    path.write_bytes(b"second")
    files.publish(path, key)
    assert files.read_bytes(key) == b"second"


def test_delete_removes_it_everywhere_the_store_answers(tmp_path, key):
    files.publish_bytes(key, b"x")
    assert files.delete(key)
    assert not files.delete(key)
    assert not files.exists(key, tmp_path / "missing")
    assert files.read_bytes(key) is None
    assert not files.materialize(key, tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


def test_exists_sees_another_instances_publish_once_the_listing_is_fresh(tmp_path, key):
    files.forget_keys()
    assert not files.exists(key)
    # Written behind this process's back, as another instance would.
    files.publish_bytes(key, b"y")
    files.forget_keys()
    assert files.exists(key)
    assert key in files.keys(key.rsplit("/", 1)[0] + "/")


def test_keys_are_scoped_to_their_prefix(key):
    prefix = key.rsplit("/", 1)[0] + "/"
    sibling = prefix + "other.bin"
    files.publish_many([(key, b"a"), (sibling, b"b")])
    try:
        assert files.keys(prefix, fresh=True) == {key, sibling}
        # A literal underscore or percent in a prefix is not a wildcard.
        assert files.keys(prefix.replace("/", "_"), fresh=True) == set()
    finally:
        files.delete(sibling)


def test_concurrent_materialize_downloads_once(tmp_path, key, monkeypatch):
    files.publish_bytes(key, b"shared")
    calls = []
    real = files.read_bytes

    def counting(k, **kw):
        calls.append(k)
        return real(k, **kw)

    monkeypatch.setattr(files, "read_bytes", counting)
    target = tmp_path / "blob.bin"
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(files.materialize(key, target)))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True] * 6
    assert len(calls) <= 1
    assert target.read_bytes() == b"shared"


def test_a_model_loads_after_its_artifact_file_is_deleted(db):
    rows = []
    for i in range(20):
        row = {name: 0.0 for name in QUALIFYING_FEATURES}
        row["team_quali_pace_percentile"] = i / 20
        rows.append(row)
    labels = [1 if i >= 10 else 0 for i in range(20)]
    model = QualifyingAdvancementModel().fit(rows, labels)
    model_type = m.ModelType.QUALIFYING_ADVANCEMENT.value
    record, _ = registry.register(
        db, model_type, model, ValidationReport("accuracy", 0.7, 10), training_rows=20,
    )

    # A fresh instance: no in-process cache, no file on disk.
    registry.clear_model_cache()
    path = registry.artifact_path(model_type, record.version)
    path.unlink()

    loaded, loaded_record = registry.load_by_id(db, record.id)
    assert loaded_record.id == record.id
    assert loaded.feature_names == model.feature_names
    assert path.exists()  # materialized back into the cache directory

    # With neither the file nor the stored copy, the error says where it looked.
    registry.clear_model_cache()
    path.unlink()
    files.delete(registry.artifact_key(model_type, record.version), db=db)
    with pytest.raises(FileNotFoundError, match="file store"):
        registry.load_by_id(db, record.id)


def test_a_replay_bundle_is_read_from_its_database_row(monkeypatch, tmp_path):
    """Bundles keep their own table; a missing file is fetched, not "not built"."""
    import gzip
    import json

    from app.data import replay
    from app.data.replay import store
    from app.db.database import session_scope

    monkeypatch.setattr(store.settings, "REPLAY_DIR", tmp_path)
    monkeypatch.setattr(store, "_shared_keys", None)
    session_key = 990_000 + uuid.uuid4().int % 9_999
    payload = gzip.compress(json.dumps({"frames": [], "events": [], "session": {}}).encode())
    with session_scope() as db:
        db.add(m.ReplayBundle(openf1_session_key=session_key, version=replay.BUNDLE_VERSION,
                              payload=payload, size_bytes=len(payload)))
    try:
        assert not replay.bundle_path(session_key).exists()
        assert replay.is_cached(session_key)
        assert replay.bundle_bytes(session_key) == payload
        replay.bundle_path(session_key).unlink()
        replay.forget_bundle(session_key)
        assert replay.load_bundle(session_key, use_cache=False)["frames"] == []
    finally:
        with session_scope() as db:
            db.query(m.ReplayBundle).filter_by(openf1_session_key=session_key).delete()
        monkeypatch.setattr(store, "_shared_keys", None)


def test_a_circuit_map_only_in_the_store_is_found(monkeypatch, tmp_path):
    import json

    from app.data import circuits

    monkeypatch.setattr(circuits, "CACHE_DIR", tmp_path)
    stem = f"test-ring-{uuid.uuid4().hex[:8]}"
    files.publish_bytes(circuits.store_key(stem),
                        json.dumps({"circuit": "Test Ring", "layers": {}}).encode())
    try:
        circuits.forget()
        assert stem in circuits.available()
        found = circuits.path_of(stem)
        assert found == tmp_path / f"{stem}.json"
        assert circuits.find(stem)["circuit"] == "Test Ring"
    finally:
        files.delete(circuits.store_key(stem))
        circuits.forget()
