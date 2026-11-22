"""Each circuit's surroundings, drawn from OpenStreetMap in the feed's frame.

The JSON files beside this module are written by ``build.py`` -- after every
replay build, on demand, or from scripts/build_circuit_maps.py -- and the
ones for circuits already raced are committed. Each holds grass, gravel,
water, woods, buildings, stands, roads, the other tarmac on site and the pit
lane, as polygons and lines in the same decimetre frame as the replay's
racing line, so the dashboard can draw them with the same transform as the
cars.

Where a map is looked for, in order:

1. **Beside this module** -- the committed maps, read-only on a function.
2. **The cache directory** (:data:`CACHE_DIR`) -- where ``build.write`` puts a
   newly made map. Locally that is this same directory, as it always was; on
   a function it is under ``DATA_ROOT`` in ``/tmp``, the only place writable.
3. **The durable file store** under ``circuits/<slug>.json`` -- every new map is
   published there too, so a fresh instance whose ``/tmp`` is empty fetches it
   into the cache directory rather than mapping the circuit all over again.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings
from app.storage import files

HERE = Path(__file__).resolve().parent
#: Where newly built maps are written: here locally, ``/tmp`` on a function.
CACHE_DIR = settings.DATA_ROOT / "circuits" if settings.SERVERLESS else HERE
#: Key prefix of maps in the durable file store.
STORE_PREFIX = "circuits/"


def store_key(stem: str) -> str:
    return f"{STORE_PREFIX}{stem}.json"


def _local_paths() -> list[Path]:
    """Every map on disk, committed ones first; one per name."""
    seen: dict[str, Path] = {}
    for directory in dict.fromkeys((HERE, CACHE_DIR)):
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                seen.setdefault(path.stem, path)
    return list(seen.values())


def _stored_stems() -> set[str]:
    """Names of maps in the file store (a cached listing, no blobs read)."""
    return {
        key[len(STORE_PREFIX):-len(".json")]
        for key in files.keys(STORE_PREFIX)
        if key.endswith(".json") and "/" not in key[len(STORE_PREFIX):]
    }


def _fetch_stored() -> bool:
    """Bring every stored map this instance lacks into the cache directory.

    Matching is by the name *inside* a map as well as by its file name, so a
    lookup cannot tell which stored map it wants without reading them; there
    are a few dozen circuits at most, so fetching the missing ones is fine.
    Returns whether any were fetched.
    """
    local = {path.stem for path in _local_paths()}
    fetched = False
    for stem in sorted(_stored_stems() - local):
        fetched |= files.materialize(store_key(stem), CACHE_DIR / f"{stem}.json")
    return fetched


@lru_cache(maxsize=16)
def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


@lru_cache(maxsize=16)
def _gzipped(path: str) -> bytes:
    import gzip

    return gzip.compress(Path(path).read_bytes(), compresslevel=6)


def forget() -> None:
    """Drop what has been read, after a map is (re)written."""
    _load.cache_clear()
    _gzipped.cache_clear()


def available() -> list[str]:
    """Every mapped circuit: committed, built here, or built anywhere else."""
    return sorted({p.stem for p in _local_paths()} | _stored_stems())


def _match(wanted: str) -> Path | None:
    for path in _local_paths():
        data = _load(str(path))
        names = {path.stem.replace("-", " "), str(data.get("circuit", "")).lower()}
        if any(name and (name in wanted or wanted in name) for name in names):
            return path
    return None


def path_of(circuit: str) -> Path | None:
    """The file holding a circuit's map, matched as ``find`` matches.

    Disk first, which is every call once a map has been seen; only a miss asks
    the file store, and then only for maps this instance does not have.
    """
    wanted = circuit.strip().lower().replace("-", " ")
    if not wanted:
        return None
    found = _match(wanted)
    if found is None and _fetch_stored():
        found = _match(wanted)
    return found


def gzipped(circuit: str) -> bytes | None:
    """The map as gzipped JSON, ready to send: a city circuit's is half a megabyte raw."""
    path = path_of(circuit)
    return _gzipped(str(path)) if path else None


def find(circuit: str) -> dict[str, Any] | None:
    """The map for a circuit, matched loosely on its name ('Mexico City',
    'mexico-city', 'Silverstone Circuit' all find theirs)."""
    path = path_of(circuit)
    return _load(str(path)) if path else None
