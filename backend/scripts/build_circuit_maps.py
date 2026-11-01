"""Map circuits' surroundings from OpenStreetMap (see app/data/circuits/build.py).

    .venv/Scripts/python scripts/build_circuit_maps.py --all          # every built replay's circuit
    .venv/Scripts/python scripts/build_circuit_maps.py Monza --force  # one, rebuilt

A circuit needs a built replay (its racing line is what the map is laid onto).
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.data.circuits import build as builder  # noqa: E402


def built_bundles():
    """One built replay per circuit, newest first."""
    seen = set()
    for path in sorted((BACKEND / "replays").glob("session_*.json.gz"), reverse=True):
        bundle = json.loads(gzip.decompress(path.read_bytes()))
        circuit = (bundle.get("session") or {}).get("circuit")
        if circuit and circuit not in seen:
            seen.add(circuit)
            yield circuit, bundle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("circuits", nargs="*", help="circuit names, as the feed spells them")
    parser.add_argument("--all", action="store_true", help="every circuit with a built replay")
    parser.add_argument("--force", action="store_true", help="rebuild maps that exist")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    wanted = {c.lower() for c in args.circuits}
    failed = 0
    for circuit, bundle in built_bundles():
        if not args.all and circuit.lower() not in wanted:
            continue
        try:
            path = builder.ensure_map(bundle, force=args.force)
        except builder.MapError as error:
            print(f"{circuit}: {error}")
            failed += 1
            continue
        if path is None:
            print(f"{circuit}: mapped already")
        else:
            data = json.loads(path.read_text())
            counts = {k: len(v) for k, v in data["layers"].items()}
            print(f"{circuit}: {data['fit']['median_miss_m']} m miss, "
                  f"{path.stat().st_size // 1024} KB {counts}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
