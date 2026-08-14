"""The site spec: the contract between the map picker and the Revit builder.

Keeping this a plain dict with an explicit schema is what lets the map picker,
the fetch layer and the Revit builder be developed and tested separately. The
picker writes one of these; build.py consumes one; neither knows about the
other.

All JSON io here is explicitly UTF-8. This is not belt-and-braces: OSM place
names are full of non-ASCII, and letting IronPython fall back to the platform
codec is exactly the bug that silently produced empty files in Blendit.
"""

import io
import json

SPEC_VERSION = "0.1.0"

ALL_LAYERS = ["terrain", "buildings", "roads", "water", "green", "rail"]

# How ways are drawn. Ribbons are surfaces of the real carriageway width laid
# on the terrain; centrelines are single curves carrying a width parameter,
# which stay handy for tracing over and are much lighter in a dense city.
ROAD_STYLES = ("centrelines", "ribbons", "both")

# What elevation becomes zero in the Revit model.
DATUM_MODES = {
    "centre": "Terrain at the centre of the site sits at project zero",
    "min": "The lowest point of the site sits at project zero",
    "sealevel": "Keep true heights above sea level",
}


def default_request():
    """A complete, valid request with sensible defaults."""
    return {
        "version": SPEC_VERSION,
        "name": "Site",
        "bbox": None,                          # [west, south, east, north]
        "layers": ["terrain", "buildings", "roads"],
        "road_group": "streets",
        "road_style": "ribbons",               # centrelines | ribbons | both
        "terrain": {
            "max_points": 10000,
            "target_m": 15.0,
        },
        "features": {
            "level_height_m": 3.2,
            "default_height_m": 8.0,
            "simplify_m": 0.3,
            "min_building_area_m2": 4.0,
            "max_buildings": 4000,
            "drape": True,                     # sit features on the terrain
        },
        "datum": "centre",
        "align_true_north": True,
        "write_site_location": False,          # push lat/long back to the host
    }


def merge_request(overrides):
    """Deep-merge user overrides onto the defaults. -> request dict."""
    req = default_request()
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(req.get(key), dict):
            req[key].update(value)
        else:
            req[key] = value
    return req


def validate_request(req):
    """-> (ok, message). Message is safe to show a user verbatim."""
    from . import geodesy

    if not isinstance(req, dict):
        return False, "The site request was not readable."

    bbox = req.get("bbox")
    if not bbox or len(bbox) != 4:
        return False, "No area was selected. Draw a box on the map first."
    try:
        bbox = geodesy.normalise_bbox(bbox)
    except (TypeError, ValueError):
        return False, "The selected area had unreadable coordinates."

    ok, message = geodesy.bbox_is_sane(bbox)
    if not ok:
        return False, message

    layers = req.get("layers") or []
    if not layers:
        return False, "No layers were selected. Pick at least one."
    unknown = [x for x in layers if x not in ALL_LAYERS]
    if unknown:
        return False, "Unknown layer(s): %s" % ", ".join(unknown)

    if req.get("datum") not in DATUM_MODES:
        return False, "Unknown datum mode %r." % req.get("datum")

    if req.get("road_style") not in ROAD_STYLES:
        return False, "Unknown road style %r." % req.get("road_style")

    return True, ""


def datum_offset(grid, mode):
    """The elevation in metres that should become project zero."""
    if grid is None:
        return 0.0
    if mode == "min":
        return grid.min_elev
    if mode == "sealevel":
        return 0.0
    # "centre": sample the middle of the grid.
    return grid.sample((grid.e0 + grid.e1) / 2.0, (grid.n0 + grid.n1) / 2.0)


# ----------------------------------------------------------------- json io

def write_json(path, obj):
    """UTF-8 JSON, non-ASCII preserved rather than escaped or crashed on."""
    with io.open(path, "w", encoding="utf-8") as handle:
        text = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)
        if not isinstance(text, type(u"")):
            text = text.decode("utf-8")
        handle.write(text)


def read_json(path):
    """Read UTF-8 JSON written by write_json."""
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.loads(handle.read())


# ------------------------------------------------------------------ summary

def summarise(site):
    """Human-readable account of what a fetch produced. -> str.

    Shown after the run, and deliberately honest about guessed heights: a
    skyline built mostly from a 3.2 m default is worth knowing about before
    anyone puts it in front of a client.
    """
    lines = []
    grid = site.get("terrain")
    if grid is not None:
        lines.append("Terrain   %d x %d points, %.0f m to %.0f m above sea level"
                     % (grid.cols, grid.rows, grid.min_elev, grid.max_elev))

    features = site.get("features") or {}
    buildings = features.get("buildings") or []
    if buildings:
        stats = site.get("stats") or {}
        tagged = stats.get("height_from_tag", 0)
        levels = stats.get("height_from_levels", 0)
        guessed = stats.get("height_default", 0)
        lines.append("Buildings %d  (heights: %d measured, %d from storey counts, "
                     "%d assumed)" % (len(buildings), tagged, levels, guessed))

    roads = features.get("roads") or []
    if roads:
        from . import geom
        total = sum(geom.perimeter(r["points"]) for r in roads)
        lines.append("Roads     %d ways, %.1f km" % (len(roads), total / 1000.0))

    for key, label in (("water", "Water"), ("green", "Green"), ("rail", "Rail")):
        items = features.get(key) or []
        if items:
            lines.append("%-9s %d" % (label, len(items)))

    skipped = (site.get("stats") or {}).get("skipped", 0)
    if skipped:
        lines.append("")
        lines.append("%d element(s) skipped as unusable geometry." % skipped)

    return "\n".join(lines) if lines else "Nothing was found in that area."
