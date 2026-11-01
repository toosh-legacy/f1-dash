"""Draw a circuit's real surroundings from OpenStreetMap, in the feed's frame.

The position feed gives a circuit's racing line in its own local frame --
decimetres, rotated to suit nobody -- and nothing about what is around it.
OpenStreetMap has the rest: the pit lane, the grass, the gravel, the stands
and the buildings, mapped by people who have been there.

For a circuit with a built replay this

  1. finds where on Earth the circuit is (a table of every venue on the
     calendar, then OSM's own geocoder for anything new),
  2. downloads the OSM data around it -- an area sized from the racing line,
     split into tiles if the API says it is too much at once,
  3. finds the similarity transform that lays the racing line onto OSM's
     raceways (or, at a street circuit with none, onto its roads): ICP from
     many starting angles, mirror allowed,
  4. carries every feature worth drawing through that transform into the
     feed's frame, simplified, and
  5. writes app/data/circuits/<slug>.json, served by GET /circuits/{name}/map.

It runs by itself after every replay build (``ensure_map``), on demand when
the dashboard asks for a circuit nobody has mapped, and from the command line:

    .venv/Scripts/python scripts/build_circuit_maps.py --all

OSM data is (c) OpenStreetMap contributors, ODbL, and the page credits it.
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
from scipy.spatial import cKDTree

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[2]
CACHE = BACKEND / ".osm_cache"
USER_AGENT = "f1-dash circuit maps (personal project)"

#: Where each venue is, by the feed's circuit name: the middle of the track,
#: near enough that the download around it holds the whole circuit.
LOCATIONS: dict[str, tuple[float, float]] = {
    "melbourne": (-37.8497, 144.9680),
    "shanghai": (31.3389, 121.2197),
    "suzuka": (34.8431, 136.5407),
    "sakhir": (26.0325, 50.5106),
    "jeddah": (21.6319, 39.1044),
    "miami": (25.9581, -80.2389),
    "montreal": (45.5000, -73.5228),
    "monte carlo": (43.7347, 7.4206),
    "monaco": (43.7347, 7.4206),
    "catalunya": (41.5700, 2.2611),
    "barcelona": (41.5700, 2.2611),
    "spielberg": (47.2197, 14.7647),
    "silverstone": (52.0715, -1.0155),
    "spa-francorchamps": (50.4372, 5.9714),
    "hungaroring": (47.5815, 19.2500),
    "zandvoort": (52.3880, 4.5440),
    "monza": (45.6156, 9.2811),
    "madring": (40.4650, -3.6150),
    "madrid": (40.4650, -3.6150),
    "baku": (40.3725, 49.8533),
    "singapore": (1.2914, 103.8640),
    "austin": (30.1340, -97.6360),
    "mexico city": (19.4030, -99.0890),
    "interlagos": (-23.7036, -46.6997),
    "sao paulo": (-23.7036, -46.6997),
    "las vegas": (36.1147, -115.1728),
    "lusail": (25.4900, 51.4542),
    "yas marina": (24.4672, 54.6031),
    "yas marina circuit": (24.4672, 54.6031),
    "imola": (44.3439, 11.7167),
}

FEED_UNITS_PER_M = 10.0          # the feed is in decimetres
SIMPLIFY_M = 0.8
#: A fit worse than this is not a fit; the map is not written.
MAX_MISS_M = 8.0
#: How far past the racing line the map reaches, as a share of its size.
REACH = 0.7


class MapError(RuntimeError):
    """This circuit could not be mapped (not found, or no fit)."""


def slug(circuit: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", circuit.strip().lower()).strip("-")


# -- where, and what is there ------------------------------------------------

def _get(url: str, timeout: float = 180) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def locate(circuit: str, country: str | None = None) -> tuple[float, float]:
    """The circuit's rough centre: from the table, else OSM's geocoder."""
    key = circuit.strip().lower()
    if key in LOCATIONS:
        return LOCATIONS[key]
    for query in (f"{circuit} circuit", f"{circuit} raceway", circuit):
        if country:
            query = f"{query}, {country}"
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "limit": 1})
        try:
            hits = json.loads(_get(url, timeout=30))
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            log.warning("geocoding %r failed: %s", query, error)
            continue
        if hits:
            return float(hits[0]["lat"]), float(hits[0]["lon"])
    raise MapError(f"cannot find {circuit} on the map")


def bbox_for(track: dict[str, Any], lat: float, lon: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) around the circuit, sized from its line.

    Half the line's span, stretched by REACH for the surroundings, plus slack
    for the centre in the table being a few hundred metres off."""
    b = track["bounds"]
    span_m = max(b["max_x"] - b["min_x"], b["max_y"] - b["min_y"]) / FEED_UNITS_PER_M
    half_m = span_m * (0.5 + REACH * 0.6) + 400
    dlat = half_m / 110_540.0
    dlon = half_m / (111_320.0 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def fetch(name: str, box: tuple[float, float, float, float], depth: int = 0) -> list[bytes]:
    """OSM data in the box, as one or more XML documents.

    The API refuses an area with too many nodes (a city around a street
    circuit); then the box is split in four and each quarter asked for."""
    south, west, north, east = box
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{name}-{south:.4f}_{west:.4f}_{north:.4f}_{east:.4f}.osm.gz"
    if path.exists():
        return [gzip.decompress(path.read_bytes())]
    url = f"https://api.openstreetmap.org/api/0.6/map?bbox={west:.5f},{south:.5f},{east:.5f},{north:.5f}"
    try:
        log.info("fetching %s", url)
        data = _get(url)
    except urllib.error.HTTPError as error:
        if error.code in (400, 509) and depth < 3:
            mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
            out: list[bytes] = []
            for quarter in ((south, west, mid_lat, mid_lon), (south, mid_lon, mid_lat, east),
                            (mid_lat, west, north, mid_lon), (mid_lat, mid_lon, north, east)):
                out += fetch(name, quarter, depth + 1)
            return out
        raise MapError(f"OpenStreetMap refused {url}: {error}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise MapError(f"OpenStreetMap unreachable: {error}") from error
    path.write_bytes(gzip.compress(data))
    return [data]


def parse(documents: list[bytes]):
    """Ways (tags, points, closed) and multipolygon relations, merged across tiles."""
    nodes: dict[str, tuple[float, float]] = {}
    roots = [ET.fromstring(doc) for doc in documents]
    for root in roots:
        for n in root.iter("node"):
            nodes[n.get("id")] = (float(n.get("lat")), float(n.get("lon")))
    ways: dict[str, tuple] = {}
    relations: dict[str, tuple] = {}
    for root in roots:
        for w in root.iter("way"):
            if w.get("id") in ways:
                continue
            refs = [nd.get("ref") for nd in w.iter("nd")]
            tags = {t.get("k"): t.get("v") for t in w.iter("tag")}
            pts = [nodes[r] for r in refs if r in nodes]
            if len(pts) >= 2:
                ways[w.get("id")] = (tags, pts, refs[0] == refs[-1] and len(refs) > 3)
        for rel in root.iter("relation"):
            tags = {t.get("k"): t.get("v") for t in rel.iter("tag")}
            if tags.get("type") != "multipolygon":
                continue
            outers = [m.get("ref") for m in rel.iter("member")
                      if m.get("type") == "way" and m.get("role") in ("outer", "")]
            relations[rel.get("id")] = (tags, outers)
    return ways, list(relations.values())


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


def align(line_m: np.ndarray, target_m: np.ndarray, near: np.ndarray | None = None):
    """Lay the feed's line (metres, its own frame) onto OSM's tarmac.

    Returns (mirror, s, R, t, residual) with osm ~ s R (M line) + t, where M
    flips y when mirror is set. ICP starts from every 6 degrees, both ways
    round, with the line's middle put at the middle of the target and, if
    given, at ``near`` (where the circuit is said to be)."""
    tree = cKDTree(target_m)
    src0 = densify(line_m, 6.0)
    starts = [target_m.mean(0)] + ([np.asarray(near, dtype=float)] if near is not None else [])
    best = None
    for mirror in (False, True):
        src = src0 * (1, -1) if mirror else src0
        for centre in starts:
            for deg in range(0, 360, 6):
                th = math.radians(deg)
                r = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
                s = 1.0
                t = centre - (r @ src.mean(0))
                for it in range(60):
                    moved = (s * (r @ src.T)).T + t
                    dist, idx = tree.query(moved)
                    cut = np.quantile(dist, 0.85 if it > 10 else 1.0)
                    keep = dist <= cut
                    s, r, t = umeyama(src[keep], target_m[idx[keep]])
                    s = min(max(s, 0.9), 1.1)
                    t = target_m[idx[keep]].mean(0) - s * (r @ src[keep].mean(0))
                moved = (s * (r @ src.T)).T + t
                dist, _ = tree.query(moved)
                score = float(np.median(dist))
                if best is None or score < best[-1]:
                    best = (mirror, s, r, t, score)
    return best


# -- the build ----------------------------------------------------------------

LAYERS = ("grass", "wood", "water", "gravel", "paved", "building", "stand", "road", "tarmac", "pitlane")


def build(circuit: str, track: dict[str, Any], source: str = "", country: str | None = None) -> dict:
    """The map of what is around ``circuit``, in the frame of ``track``."""
    lat0, lon0 = locate(circuit, country)
    ways, relations = parse(fetch(slug(circuit), bbox_for(track, lat0, lon0)))
    proj = Projection(lat0, lon0)

    def lines_where(test):
        return [proj(pts) for tags, pts, _ in ways.values() if test(tags)]

    raceways = lines_where(lambda t: t.get("highway") == "raceway" and "karting" not in t.get("sport", ""))
    target = np.vstack([densify(p, 2.0) for p in raceways]) if raceways else np.zeros((0, 2))
    if len(target) < 300:
        # A street circuit: the track is the town's roads, mapped as roads.
        roads = lines_where(lambda t: t.get("highway") in ROAD_TYPES | {"raceway", "pedestrian", "living_street"})
        if not roads:
            raise MapError(f"no tarmac mapped around {circuit}")
        target = np.vstack([target] + [densify(p, 2.0) for p in roads])

    line_m = np.asarray(track["path"], dtype=float) / FEED_UNITS_PER_M
    mirror, s, r, t, residual = align(line_m, target, near=(0.0, 0.0))
    log.info("%s: aligned, mirror=%s scale=%.4f median miss=%.2f m", circuit, mirror, s, residual)
    if residual > MAX_MISS_M:
        raise MapError(f"{circuit}: no fit to OpenStreetMap (median miss {residual:.1f} m)")

    def to_feed(pts_m: np.ndarray) -> np.ndarray:
        local = (r.T @ ((pts_m - t).T)).T / s
        if mirror:
            local = local * (1, -1)
        return local * FEED_UNITS_PER_M

    b = track["bounds"]
    span = max(b["max_x"] - b["min_x"], b["max_y"] - b["min_y"])
    lo = np.array([b["min_x"], b["min_y"]]) - span * REACH
    hi = np.array([b["max_x"], b["max_y"]]) + span * REACH

    # The racing line as drawn by the feed, to tell the GP track's own OSM
    # ways (already drawn) from the other tarmac around it.
    line_tree = cKDTree(densify(np.asarray(track["path"], dtype=float), 20.0))
    pit = np.asarray(track.get("pit_path") or [[1e12, 1e12]], dtype=float)
    pit_tree = cKDTree(densify(pit, 20.0) if len(pit) > 1 else pit)

    layers: dict[str, list] = {k: [] for k in LAYERS}

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
            # At a street circuit the track is a road too; it is drawn already.
            if np.median(line_tree.query(densify(poly, 30.0))[0]) < 80:
                continue
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
        "fit": {"mirror": bool(mirror), "scale": round(float(s), 5), "median_miss_m": round(residual, 2),
                "source": source, "centre": [lat0, lon0]},
        "layers": layers,
    }


def write(data: dict) -> Path:
    path = HERE / f"{slug(data['circuit'])}.json"
    path.write_text(json.dumps(data, separators=(",", ":")))
    return path


def ensure_map(bundle: dict[str, Any], force: bool = False) -> Path | None:
    """Map the circuit of a built replay, unless it is mapped already.

    Called after every replay build, so a circuit is mapped the first time
    anyone builds a race there. Returns the file written, or None if there
    was nothing to do."""
    from app.data import circuits   # the package; imported late to avoid a cycle

    session = bundle.get("session") or {}
    circuit = session.get("circuit") or session.get("location")
    if not circuit or not bundle.get("track"):
        return None
    if not force and circuits.find(circuit) is not None:
        return None
    data = build(circuit, bundle["track"], source=f"session {session.get('session_key')}",
                 country=session.get("country"))
    path = write(data)
    circuits.forget()
    return path
