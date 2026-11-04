"""Each circuit's surroundings, drawn from OpenStreetMap in the feed's frame.

The JSON files beside this module are written by ``build.py`` -- after every
replay build, on demand, or from scripts/build_circuit_maps.py -- and the
ones for circuits already raced are committed. Each holds grass, gravel,
water, woods, buildings, stands, roads, the other tarmac on site and the pit
lane, as polygons and lines in the same decimetre frame as the replay's
racing line, so the dashboard can draw them with the same transform as the
cars.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


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
    return sorted(p.stem for p in HERE.glob("*.json"))


def path_of(circuit: str) -> Path | None:
    """The file holding a circuit's map, matched as ``find`` matches."""
    wanted = circuit.strip().lower().replace("-", " ")
    if not wanted:
        return None
    for path in HERE.glob("*.json"):
        data = _load(str(path))
        names = {path.stem.replace("-", " "), str(data.get("circuit", "")).lower()}
        if any(name and (name in wanted or wanted in name) for name in names):
            return path
    return None


def gzipped(circuit: str) -> bytes | None:
    """The map as gzipped JSON, ready to send: a city circuit's is half a megabyte raw."""
    path = path_of(circuit)
    return _gzipped(str(path)) if path else None


def find(circuit: str) -> dict[str, Any] | None:
    """The map for a circuit, matched loosely on its name ('Mexico City',
    'mexico-city', 'Silverstone Circuit' all find theirs)."""
    path = path_of(circuit)
    return _load(str(path)) if path else None
