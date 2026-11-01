"""Draw each circuit's real surroundings from OpenStreetMap, in the feed's frame.

The position feed gives a circuit's racing line in its own local frame --
decimetres, rotated to suit nobody -- and nothing about what is around it.
OpenStreetMap has the rest: the pit lane, the grass, the gravel, the stands
and the buildings, mapped by people who have been there.

For each circuit this script

  1. downloads the OSM data around it (the main API's /map call),
  2. finds the similarity transform that lays the feed's racing line onto the
     OSM raceways (ICP from many starting angles, mirror allowed),
  3. carries every feature worth drawing through that transform into the
     feed's frame, simplified, and
  4. writes app/data/circuits/<slug>.json, which the API serves and the map
     draws under the cars.

Run it once per circuit, after a replay there has been built:

    .venv/Scripts/python scripts/build_circuit_maps.py silverstone

The output is committed; OSM data is (c) OpenStreetMap contributors, ODbL,
and the page credits it.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "data" / "circuits"
CACHE = ROOT / ".osm_cache"

# name -> (feed circuit name, centre lat, centre lon, half-height deg, half-width deg)
CIRCUITS = {
    "silverstone": ("Silverstone", 52.0715, -1.0155, 0.0135, 0.0200),
    "hungaroring": ("Hungaroring", 47.5815, 19.2500, 0.0080, 0.0090),
    "zandvoort": ("Zandvoort", 52.3880, 4.5440, 0.0070, 0.0110),
    "austin": ("Austin", 30.1340, -97.6360, 0.0110, 0.0120),
    "mexico-city": ("Mexico City", 19.4030, -99.0890, 0.0090, 0.0100),
}

FEED_UNITS_PER_M = 10.0          # the feed is in decimetres
SIMPLIFY_M = 0.8


# -- download and parse ----------------------------------------------------

def fetch(slug: str) -> bytes:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{slug}.osm.gz"
    if path.exists():
        return gzip.decompress(path.read_bytes())
    _, lat, lon, dlat, dlon = CIRCUITS[slug]
    url = (f"https://api.openstreetmap.org/api/0.6/map?"
           f"bbox={lon - dlon:.5f},{lat - dlat:.5f},{lon + dlon:.5f},{lat + dlat:.5f}")
    print("fetching", url)
    request = urllib.request.Request(url, headers={"User-Agent": "f1-dash circuit maps (personal project)"})
    with urllib.request.urlopen(request, timeout=180) as response:
        data = response.read()
    path.write_bytes(gzip.compress(data))
    return data


def parse(xml: bytes):
    root = ET.fromstring(xml)
    nodes = {n.get("id"): (float(n.get("lat")), float(n.get("lon"))) for n in root.iter("node")}
    ways = {}
    for w in root.iter("way"):
        refs = [nd.get("ref") for nd in w.iter("nd")]
        tags = {t.get("k"): t.get("v") for t in w.iter("tag")}
        pts = [nodes[r] for r in refs if r in nodes]
        if len(pts) >= 2:
            ways[w.get("id")] = (tags, pts, refs[0] == refs[-1] and len(refs) > 3)
    relations = []
    for rel in root.iter("relation"):
        tags = {t.get("k"): t.get("v") for t in rel.iter("tag")}
        if tags.get("type") != "multipolygon":
            continue
        outers = [m.get("ref") for m in rel.iter("member")
                  if m.get("type") == "way" and m.get("role") in ("outer", "")]
        relations.append((tags, outers))
    return ways, relations


# -- classifying -------------------------------------------------------------

GRASS_LANDUSE = {"grass", "meadow", "village_green", "recreation_ground", "farmland", "orchard"}
GRASS_NATURAL = {"grassland", "heath"}
GRASS_LEISURE = {"golf_course", "pitch", "park", "garden"}


def area_class(tags: dict) -> str | None:
    """What a closed way or multipolygon is, for drawing; None to leave out."""
    building = tags.get("building")
    if tags.get("leisure") == "bleachers" or building in ("grandstand", "stadium"):
        return "stand"
    if building:
        return "building"
    natural, landuse = tags.get("natural"), tags.get("landuse")
    if natural == "water" or landuse in ("reservoir", "basin") or tags.get("waterway") == "riverbank":
        return "water"
    if natural in ("wood", "scrub") or landuse == "forest":
        return "wood"
    if natural in ("sand", "shingle") or tags.get("surface") in ("sand", "gravel") or tags.get("golf") == "bunker":
        return "gravel"
    if (landuse in GRASS_LANDUSE or natural in GRASS_NATURAL or tags.get("leisure") in GRASS_LEISURE
            or tags.get("surface") == "grass"):
        return "grass"
    if (tags.get("amenity") == "parking" or "area:highway" in tags or tags.get("aeroway") == "apron"
            or (tags.get("area") == "yes" and tags.get("surface") in ("asphalt", "concrete", "paved"))):
        return "paved"
    return None


ROAD_TYPES = {"service", "unclassified", "tertiary", "secondary", "primary", "trunk",
              "trunk_link", "residential", "track"}


# -- geometry ----------------------------------------------------------------

class Projection:
    """lat/lon to local metres, east and north, around a fixed centre."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = 111_320.0 * math.cos(math.radians(lat0))
        self.ky = 110_540.0

    def __call__(self, pts):
        a = np.asarray(pts, dtype=float)
        return np.column_stack(((a[:, 1] - self.lon0) * self.kx, (a[:, 0] - self.lat0) * self.ky))


def densify(poly: np.ndarray, step: float) -> np.ndarray:
    out = [poly[0]]
    for a, b in zip(poly[:-1], poly[1:]):
        n = max(int(np.linalg.norm(b - a) / step), 1)
        for k in range(1, n + 1):
            out.append(a + (b - a) * k / n)
    return np.asarray(out)


def simplify(poly: np.ndarray, tol: float) -> np.ndarray:
    """Ramer-Douglas-Peucker, iterative. A closed ring is split at the point
    farthest from its start, so the two halves each have a real chord."""
    if len(poly) < 3:
        return poly
    if np.allclose(poly[0], poly[-1]):
        far = int(np.argmax(np.hypot(*(poly - poly[0]).T)))
        if far == 0:
            return poly[:1]
        return np.vstack((simplify(poly[:far + 1], tol)[:-1], simplify(poly[far:], tol)))
    keep = np.zeros(len(poly), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(poly) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = poly[i], poly[j]
        seg = b - a
        span = np.hypot(*seg) or 1e-9
        rel = poly[i + 1:j] - a
        d = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / span
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack += [(i, m), (m, j)]
    return poly[keep]


def umeyama(src: np.ndarray, dst: np.ndarray):
    """Least-squares similarity dst ~ s R src + t (R may be a reflection only
    if src was mirrored beforehand; here R is a proper rotation)."""
    mu_s, mu_d = src.mean(0), dst.mean(0)
    a, b = src - mu_s, dst - mu_d
    cov = b.T @ a / len(src)
    u, d, vt = np.linalg.svd(cov)
    sign = np.eye(2)
    if np.linalg.det(u @ vt) < 0:
        sign[1, 1] = -1
    r = u @ sign @ vt
    s = np.trace(np.diag(d) @ sign) / (a ** 2).sum(1).mean()
    return s, r, mu_d - s * r @ mu_s


def align(line_m: np.ndarray, raceway_m: np.ndarray):
    """Lay the feed's line (metres, its own frame) onto the OSM raceways.

    Returns (mirror, s, R, t, residual) with osm ~ s R (M line) + t, where M
    flips y when mirror is set."""
    tree = cKDTree(raceway_m)
    src0 = densify(line_m, 6.0)
    best = None
    for mirror in (False, True):
        src = src0 * (1, -1) if mirror else src0
        centred = src - src.mean(0)
        for deg in range(0, 360, 6):
            th = math.radians(deg)
            r = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
            s, t = 1.0, None
            # Start with the centres together; the raceways around it include
            # other circuits, so let ICP walk from there.
            t = raceway_m.mean(0) - (r @ src.mean(0))
            for it in range(60):
                moved = (s * (r @ src.T)).T + t
                dist, idx = tree.query(moved)
                cut = np.quantile(dist, 0.85 if it > 10 else 1.0)
                keep = dist <= cut
                s, r, t = umeyama(src[keep], raceway_m[idx[keep]])
                s = min(max(s, 0.9), 1.1)
                t = (raceway_m[idx[keep]].mean(0) - s * (r @ src[keep].mean(0)))
            moved = (s * (r @ src.T)).T + t
            dist, _ = tree.query(moved)
            score = float(np.median(dist))
            if best is None or score < best[-1]:
                best = (mirror, s, r, t, score)
    del centred
    return best


# -- the build ----------------------------------------------------------------

def feed_track(circuit: str):
    """The racing line and pit path of any built replay at this circuit."""
    for path in sorted((ROOT / "replays").glob("session_*.json.gz"), reverse=True):
        bundle = json.loads(gzip.decompress(path.read_bytes()))
        if (bundle.get("session") or {}).get("circuit") == circuit:
            return bundle["track"], path.name
    raise SystemExit(f"no built replay at {circuit}; build one first")


def build(slug: str) -> dict:
    circuit, lat0, lon0, *_ = CIRCUITS[slug]
    track, source = feed_track(circuit)
    ways, relations = parse(fetch(slug))
    proj = Projection(lat0, lon0)

    raceway_lines = [proj(pts) for tags, pts, _ in ways.values()
                     if tags.get("highway") == "raceway" and "karting" not in tags.get("sport", "")]
    raceway_m = np.vstack([densify(p, 2.0) for p in raceway_lines])
    line_m = np.asarray(track["path"], dtype=float) / FEED_UNITS_PER_M
    mirror, s, r, t, residual = align(line_m, raceway_m)
    print(f"{slug}: aligned from {source}, mirror={mirror} scale={s:.4f} median miss={residual:.2f} m")
    if residual > 6:
        print("  warning: a poor fit; check the bbox and the drawing")

    def to_feed(pts_m: np.ndarray) -> np.ndarray:
        local = (r.T @ ((pts_m - t).T)).T / s
        if mirror:
            local = local * (1, -1)
        return local * FEED_UNITS_PER_M

    b = track["bounds"]
    span = max(b["max_x"] - b["min_x"], b["max_y"] - b["min_y"])
    lo = np.array([b["min_x"], b["min_y"]]) - span * 0.7
    hi = np.array([b["max_x"], b["max_y"]]) + span * 0.7

    # The racing line as drawn by the feed, to tell the GP track's own OSM
    # ways (already drawn) from the other tarmac around it.
    line_tree = cKDTree(densify(np.asarray(track["path"], dtype=float), 20.0))
    pit = np.asarray(track.get("pit_path") or [[1e12, 1e12]], dtype=float)
    pit_tree = cKDTree(densify(pit, 20.0) if len(pit) > 1 else pit)

    layers: dict[str, list] = {k: [] for k in
                               ("grass", "wood", "water", "gravel", "paved", "building", "stand",
                                "road", "tarmac", "pitlane")}

    def keep(poly_feed: np.ndarray) -> bool:
        mn, mx = poly_feed.min(0), poly_feed.max(0)
        return bool((mx >= lo).all() and (mn <= hi).all())

    def emit(layer: str, poly_feed: np.ndarray, closed: bool):
        poly_feed = simplify(poly_feed, SIMPLIFY_M * FEED_UNITS_PER_M)
        if len(poly_feed) < (3 if closed else 2):
            return
        layers[layer].append(np.round(poly_feed).astype(int).tolist())

    used = set()
    for tags, outers in relations:
        kind = area_class(tags)
        if not kind:
            continue
        for ref in outers:
            if ref in ways and ways[ref][2]:
                poly = to_feed(proj(ways[ref][1]))
                if keep(poly):
                    emit(kind, poly, True)
                used.add(ref)

    for wid, (tags, pts, closed) in ways.items():
        if wid in used:
            continue
        poly = to_feed(proj(pts))
        if not keep(poly):
            continue
        highway = tags.get("highway")
        if highway == "raceway":
            if "karting" in tags.get("sport", ""):
                continue
            near_line = np.median(line_tree.query(densify(poly, 30.0))[0]) < 120   # 12 m
            if tags.get("raceway") == "pit_lane" or "pit" in (tags.get("name") or "").lower():
                if np.median(pit_tree.query(densify(poly, 30.0))[0]) < 150:
                    emit("pitlane", poly, False)
                    continue
            if not near_line:
                emit("tarmac", poly, False)
            continue
        if highway in ROAD_TYPES and not closed:
            emit("road", poly, False)
            continue
        if closed:
            kind = area_class(tags)
            if kind:
                emit(kind, poly, True)

    return {
        "circuit": circuit,
        "units": "feed",
        "attribution": "© OpenStreetMap contributors",
        "fit": {"mirror": mirror, "scale": round(float(s), 5), "median_miss_m": round(residual, 2),
                "source": source},
        "layers": layers,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slugs", nargs="*", default=sorted(CIRCUITS), help="circuits to build")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    for slug in args.slugs:
        data = build(slug)
        counts = {k: len(v) for k, v in data["layers"].items()}
        path = OUT / f"{slug}.json"
        path.write_text(json.dumps(data, separators=(",", ":")))
        print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB) {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
