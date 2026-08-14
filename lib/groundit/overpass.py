"""OSM data via the Overpass API: query building and response parsing.

One query fetches every requested layer as a single union, because Overpass
rate limits per request, not per byte - two round trips for buildings and
roads is strictly worse than one for both.

"out geom;" is what makes this practical: Overpass inlines each way's vertex
coordinates in the response, so there is no second pass to resolve node ids.

Pure Python. No Revit, no .NET, no network - net.py does the transport.
"""

import json
import re

from . import geom

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

DEFAULT_TIMEOUT = 90

# Typical carriageway width by highway class, metres. Used only when the way
# carries no width or lanes tag, which is most of them.
ROAD_WIDTHS = {
    "motorway": 15.0, "motorway_link": 8.0,
    "trunk": 12.0, "trunk_link": 7.0,
    "primary": 10.0, "primary_link": 6.5,
    "secondary": 9.0, "secondary_link": 6.0,
    "tertiary": 8.0, "tertiary_link": 5.5,
    "residential": 6.0, "unclassified": 5.5, "living_street": 5.0,
    "service": 4.0, "pedestrian": 4.0, "track": 3.0,
    "footway": 1.8, "cycleway": 2.0, "path": 1.5, "steps": 1.5,
}

ROAD_GROUPS = {
    "major": ["motorway", "motorway_link", "trunk", "trunk_link", "primary",
              "primary_link", "secondary", "secondary_link", "tertiary",
              "tertiary_link"],
    "streets": ["motorway", "motorway_link", "trunk", "trunk_link", "primary",
                "primary_link", "secondary", "secondary_link", "tertiary",
                "tertiary_link", "residential", "unclassified", "living_street",
                "service", "pedestrian"],
    "all": sorted(ROAD_WIDTHS.keys()),
}

DEFAULT_LEVEL_HEIGHT = 3.2
DEFAULT_BUILDING_HEIGHT = 8.0

_NUM = re.compile(r"^\s*([-+]?[0-9]*[.,]?[0-9]+)\s*(.*)$")
_FEET_INCHES = re.compile(r"^\s*([0-9]+)\s*'\s*([0-9]*(?:\.[0-9]+)?)\s*\"?\s*$")


# ------------------------------------------------------------- tag parsing

def parse_length(value):
    """An OSM length tag in metres. -> float, or None if unparseable.

    Handles the shapes that actually occur: "12", "12 m", "12m", "40 ft",
    "40'", "12'6\"", and comma decimals like "3,5".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    m = _FEET_INCHES.match(text)
    if m:
        feet = float(m.group(1))
        inches = float(m.group(2)) if m.group(2) else 0.0
        return (feet + inches / 12.0) * 0.3048

    m = _NUM.match(text)
    if not m:
        return None
    try:
        number = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    unit = m.group(2).strip().lower()
    if unit in ("", "m", "meter", "metre", "meters", "metres"):
        return number
    if unit in ("ft", "feet", "foot", "'"):
        return number * 0.3048
    if unit in ("km",):
        return number * 1000.0
    if unit in ("cm",):
        return number / 100.0
    return None


def parse_int(value):
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def building_height(tags, level_height=DEFAULT_LEVEL_HEIGHT,
                    default_height=DEFAULT_BUILDING_HEIGHT):
    """Best available height for a building, in metres. -> (height, source).

    source is one of "height", "levels", "default" so the run report can say
    how much of the skyline is real data and how much is a guess.
    """
    for key in ("height", "building:height"):
        h = parse_length(tags.get(key))
        if h and h > 0.0:
            return h, "height"

    levels = parse_int(tags.get("building:levels"))
    if levels and levels > 0:
        roof_levels = parse_int(tags.get("roof:levels")) or 0
        return (levels + roof_levels) * level_height, "levels"

    return default_height, "default"


def road_width(tags, klass):
    """Carriageway width in metres, from tags where plausible.

    Tagged widths are sanity-bounded against the class default before being
    believed. This is not theoretical: Rue Montesquieu in Paris, a lane barely
    wide enough for one car, is tagged width=99. Trusting that puts a 99 m
    ribbon through the middle of the site.
    """
    base = ROAD_WIDTHS.get(klass, 5.0)

    w = parse_length(tags.get("width"))
    if w and 0.5 <= w <= base * 3.0:
        return w

    lanes = parse_int(tags.get("lanes"))
    if lanes and 1 <= lanes <= 12:
        return lanes * 3.2

    return base


# ------------------------------------------------------------ query building

def _bbox_clause(bbox):
    """OGC (west, south, east, north) -> Overpass (south, west, north, east)."""
    west, south, east, north = bbox
    return "(%.7f,%.7f,%.7f,%.7f)" % (south, west, north, east)


def build_query(bbox, layers, road_group="streets", timeout=DEFAULT_TIMEOUT):
    """Overpass QL for every requested layer in one union. -> str.

    layers is any of: buildings, roads, water, green, rail.
    """
    bb = _bbox_clause(bbox)
    parts = []

    if "buildings" in layers:
        parts.append('way["building"]%s;' % bb)
        parts.append('relation["building"]["type"="multipolygon"]%s;' % bb)

    if "roads" in layers:
        classes = ROAD_GROUPS.get(road_group, ROAD_GROUPS["streets"])
        parts.append('way["highway"~"^(%s)$"]%s;' % ("|".join(classes), bb))

    if "water" in layers:
        parts.append('way["natural"="water"]%s;' % bb)
        parts.append('relation["natural"="water"]["type"="multipolygon"]%s;' % bb)
        parts.append('way["waterway"="riverbank"]%s;' % bb)

    if "green" in layers:
        parts.append('way["leisure"~"^(park|garden|pitch)$"]%s;' % bb)
        parts.append('way["landuse"~"^(grass|forest|meadow|recreation_ground)$"]%s;' % bb)
        parts.append('way["natural"~"^(wood|scrub|grassland)$"]%s;' % bb)

    if "rail" in layers:
        parts.append('way["railway"~"^(rail|light_rail|subway|tram)$"]%s;' % bb)

    if not parts:
        raise ValueError("No layers requested")

    return "[out:json][timeout:%d];\n(\n  %s\n);\nout geom;" % (
        timeout, "\n  ".join(parts))


# ---------------------------------------------------------------- parsing

def _ring_local(geometry, frame):
    """Overpass inline geometry -> local metres."""
    out = []
    for node in geometry:
        try:
            out.append(frame.to_local(node["lon"], node["lat"]))
        except (KeyError, TypeError):
            continue
    return out


def _classify(tags):
    """Which layer an element belongs to. -> str or None."""
    if tags.get("building"):
        return "buildings"
    if tags.get("highway"):
        return "roads"
    if tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
        return "water"
    if tags.get("railway"):
        return "rail"
    if (tags.get("leisure") or tags.get("landuse")
            or tags.get("natural") in ("wood", "scrub", "grassland")):
        return "green"
    return None


def _relation_rings(element, frame):
    """Assemble a multipolygon relation. -> (outer_rings, inner_rings)."""
    outer_ways, inner_ways = [], []
    for member in element.get("members", []):
        if member.get("type") != "way":
            continue
        pts = _ring_local(member.get("geometry") or [], frame)
        if len(pts) < 2:
            continue
        if member.get("role") == "inner":
            inner_ways.append(pts)
        else:
            outer_ways.append(pts)
    return geom.stitch_ways(outer_ways), geom.stitch_ways(inner_ways)


def parse(response, frame, options=None):
    """Overpass JSON -> features in local metres, grouped by layer.

    response may be a JSON string or an already-decoded dict.

    -> {"buildings": [...], "roads": [...], "water": [...],
        "green": [...], "rail": [...], "stats": {...}}
    """
    opts = dict(options or {})
    simplify_tol = float(opts.get("simplify_m", 0.3))
    min_area = float(opts.get("min_building_area_m2", 4.0))
    level_height = float(opts.get("level_height_m", DEFAULT_LEVEL_HEIGHT))
    default_height = float(opts.get("default_height_m", DEFAULT_BUILDING_HEIGHT))

    if not isinstance(response, dict):
        response = json.loads(response)

    out = {"buildings": [], "roads": [], "water": [], "green": [], "rail": []}
    stats = {"elements": 0, "skipped": 0,
             "height_from_tag": 0, "height_from_levels": 0, "height_default": 0}

    for element in response.get("elements", []):
        stats["elements"] += 1
        tags = element.get("tags") or {}
        layer = _classify(tags)
        if layer is None:
            stats["skipped"] += 1
            continue

        name = tags.get("name") or ""
        etype = element.get("type")

        if layer in ("roads", "rail"):
            pts = _ring_local(element.get("geometry") or [], frame)
            pts = geom.clean_polyline(pts, simplify_tol=simplify_tol)
            if not pts:
                stats["skipped"] += 1
                continue
            klass = tags.get("highway") or tags.get("railway") or "unclassified"
            out[layer].append({
                "id": element.get("id"),
                "name": name,
                "class": klass,
                "width": road_width(tags, klass),
                "points": pts,
                "bridge": bool(tags.get("bridge")),
                "tunnel": bool(tags.get("tunnel")),
                "layer": parse_int(tags.get("layer")) or 0,
            })
            continue

        # Area layers: buildings, water, green.
        if etype == "relation":
            outers, inners = _relation_rings(element, frame)
        else:
            outers, inners = [_ring_local(element.get("geometry") or [], frame)], []

        cleaned_outers = []
        for ring in outers:
            ring = geom.clean_ring(ring, simplify_tol=simplify_tol,
                                   min_area=min_area if layer == "buildings" else 1.0)
            if ring:
                cleaned_outers.append(geom.ensure_ccw(ring))
        if not cleaned_outers:
            stats["skipped"] += 1
            continue

        cleaned_inners = []
        for ring in inners:
            ring = geom.clean_ring(ring, simplify_tol=simplify_tol, min_area=1.0)
            if ring:
                cleaned_inners.append(geom.ensure_cw(ring))

        for outer, holes in geom.assign_holes(cleaned_outers, cleaned_inners):
            feature = {
                "id": element.get("id"),
                "name": name,
                "outer": outer,
                "holes": holes,
            }
            if layer == "buildings":
                height, source = building_height(tags, level_height, default_height)
                feature["height"] = height
                feature["height_source"] = source
                feature["kind"] = tags.get("building") or "yes"
                stats["height_from_tag" if source == "height" else
                      "height_from_levels" if source == "levels" else
                      "height_default"] += 1
            else:
                feature["kind"] = (tags.get("natural") or tags.get("landuse")
                                   or tags.get("leisure") or tags.get("waterway") or "")
            out[layer].append(feature)

    out["stats"] = stats
    return out
