"""Replay bundles: a completed session reconstructed for playback on the map.

OpenF1 exposes a ``location`` feed of raw track X/Y per car, which is enough to
redraw a session from the outside: the circuit itself, every car moving around
it, and the moments they peel into the pit lane.

Building one is expensive (one request per car, tens of thousands of samples
each) and the result never changes once a session is over, so a bundle is built
once as a background job and cached on disk as gzipped JSON. The dashboard then
fetches a single file and plays it locally.

The feed is coarser than its sample rate suggests: a car's position updates
roughly every 2.7 seconds, which at racing speed is a jump of some 250 metres
and about thirty distinct points per lap. Nothing here is drawn from raw
coordinates, because doing that cuts every corner. Instead:

* **The circuit outline** is reconstructed by folding every green lap of the
  race onto a single lap -- the sampling clock and the lap clock are
  independent, so across a race the same corner is caught from many different
  points -- and then refined twice by re-placing each fix along the loop the
  previous pass produced.
* **The pit lane** is traced from the fixes around timed stops. Distance from
  the racing line cannot find it on its own: a car running wide at a fast
  corner is further off line than a car in the pits.
* **Cars** are carried as *progress along* those paths rather than as
  positions, so interpolating between two fixes moves a car through the corners
  it actually drove, at the speed it drove them.
* **Per-lap race state** -- position, rolling pace, tyre and stop count -- is
  what the prediction panel reasons over.

Coordinates stay in OpenF1's own units (tenths of a metre, origin arbitrary).
The dashboard normalises them against the bundle's bounds, so no assumption
about scale or orientation is baked in here.

The work is split by what each part is about: :mod:`geometry` reconstructs the
circuit, :mod:`frames` turns fixes into playback, :mod:`narrative` reads the
race back out of the laps, :mod:`store` caches the result and :mod:`build`
assembles it. This module is the surface the rest of the application uses, so
that a caller need not know which of those a name comes from.
"""
from app.data.replay.build import build_bundle
from app.data.replay.catalogue import ReplayCatalogueEntry, catalogue
from app.data.replay.constants import BUNDLE_VERSION
from app.data.replay.shared import Progress, ReplayUnavailable
from app.data.replay.store import (
    bundle_bytes,
    bundle_cache_stats,
    bundle_generation,
    bundle_key,
    bundle_part,
    bundle_path,
    forget_bundle,
    is_cached,
    load_bundle,
    write_bundle_bytes,
)

__all__ = [
    "BUNDLE_VERSION",
    "Progress",
    "ReplayCatalogueEntry",
    "ReplayUnavailable",
    "build_bundle",
    "bundle_bytes",
    "bundle_cache_stats",
    "bundle_generation",
    "bundle_key",
    "bundle_part",
    "bundle_path",
    "catalogue",
    "forget_bundle",
    "is_cached",
    "load_bundle",
    "write_bundle_bytes",
]
