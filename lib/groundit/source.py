"""Fetch orchestration: a validated request in, a populated site out.

This is the only module that knows the order things happen in, and it is
deliberately free of both Revit and UI code so the whole download path can be
exercised headless.

Progress is reported through a callback rather than a pyRevit ProgressBar for
the same reason. Pass progress(fraction, message); it may be None.
"""

from . import geodesy, net, overpass, sitespec, tiles

# Above this many terrain tiles the download stops being "runs for a bit" and
# starts being a coffee break, so drop a zoom level instead.
MAX_TILES = 24


def _report(progress, fraction, message):
    if progress:
        progress(max(0.0, min(1.0, fraction)), message)


def choose_zoom(bbox, target_m, max_tiles=MAX_TILES):
    """Finest zoom meeting target_m that still fits the tile budget. -> int."""
    _, clat = geodesy.bbox_centre(bbox)
    zoom = geodesy.zoom_for_pixel_size(clat, target_m)
    while zoom > geodesy.MIN_ZOOM and geodesy.tile_count(bbox, zoom) > max_tiles:
        zoom -= 1
    return zoom


def estimate(request):
    """What a request will cost, for the confirmation dialog. -> dict."""
    bbox = geodesy.normalise_bbox(request["bbox"])
    width_m, height_m = geodesy.bbox_size_m(bbox)
    out = {
        "width_m": width_m,
        "height_m": height_m,
        "tiles": 0,
        "zoom": None,
        "points": 0,
    }
    if "terrain" in request.get("layers", []):
        terrain = request.get("terrain") or {}
        zoom = choose_zoom(bbox, float(terrain.get("target_m", 15.0)))
        cols, rows = tiles.grid_dims_for_budget(
            bbox, int(terrain.get("max_points", 10000)))
        out["zoom"] = zoom
        out["tiles"] = geodesy.tile_count(bbox, zoom)
        out["points"] = cols * rows
        out["ground_m"] = geodesy.metres_per_pixel(geodesy.bbox_centre(bbox)[1], zoom)
    return out


def fetch_terrain(bbox, frame, terrain_opts, progress=None, base=0.0, span=0.5):
    """Download and decode terrain tiles. -> TerrainGrid.

    base/span let the caller place this inside a wider progress range.
    """
    target_m = float(terrain_opts.get("target_m", 15.0))
    max_points = int(terrain_opts.get("max_points", 10000))

    zoom = choose_zoom(bbox, target_m)
    x0, y0, x1, y1 = geodesy.tile_range(bbox, zoom)
    coords = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]

    mosaic = tiles.TerrainMosaic(zoom)
    total = float(len(coords)) or 1.0
    for index, (x, y) in enumerate(coords):
        _report(progress, base + span * 0.9 * (index / total),
                "Terrain tile %d of %d" % (index + 1, len(coords)))
        url = tiles.TERRARIUM_URL % (zoom, x, y)
        try:
            mosaic.add_tile(x, y, net.fetch(url, timeout=30, retries=2))
        except Exception:
            # A missing tile is a hole, not a failure: the mosaic clamps to
            # its neighbours and the site still builds.
            continue

    if not len(mosaic):
        raise net.NetworkError(
            "No terrain tiles could be downloaded.\n\n"
            "The elevation service may be unreachable from this network.")

    _report(progress, base + span * 0.95, "Sampling terrain")
    cols, rows = tiles.grid_dims_for_budget(bbox, max_points)
    grid = tiles.TerrainGrid(frame, bbox, cols, rows).fill(mosaic)
    _report(progress, base + span, "Terrain ready")
    return grid


def fetch_features(bbox, frame, request, progress=None, base=0.5, span=0.5):
    """Download and parse OSM. -> (features_by_layer, stats)."""
    wanted = [x for x in request.get("layers", []) if x != "terrain"]
    if not wanted:
        return {}, {}

    feature_opts = request.get("features") or {}
    query = overpass.build_query(
        bbox, wanted, request.get("road_group", "streets"))

    _report(progress, base + span * 0.1, "Asking OpenStreetMap")
    raw, _endpoint = net.fetch_first(
        overpass.ENDPOINTS, body=net.form_body({"data": query}), timeout=120)

    _report(progress, base + span * 0.7, "Reading OpenStreetMap data")
    text = bytes(raw).decode("utf-8", "replace")
    parsed = overpass.parse(text, frame, {
        "simplify_m": float(feature_opts.get("simplify_m", 0.3)),
        "min_building_area_m2": float(feature_opts.get("min_building_area_m2", 4.0)),
        "level_height_m": float(feature_opts.get("level_height_m", 3.2)),
        "default_height_m": float(feature_opts.get("default_height_m", 8.0)),
    })

    stats = parsed.pop("stats", {})

    max_buildings = int(feature_opts.get("max_buildings", 4000))
    buildings = parsed.get("buildings") or []
    if len(buildings) > max_buildings:
        # Keep the biggest: if we must lose context, lose the sheds first.
        from . import geom
        buildings.sort(key=lambda b: abs(geom.signed_area(b["outer"])), reverse=True)
        stats["buildings_dropped"] = len(buildings) - max_buildings
        parsed["buildings"] = buildings[:max_buildings]

    _report(progress, base + span, "OpenStreetMap ready")
    return parsed, stats


def fetch_site(request, progress=None):
    """Run a validated request end to end. -> site dict.

    {"request", "bbox", "frame", "terrain", "features", "stats", "datum_m"}
    """
    ok, message = sitespec.validate_request(request)
    if not ok:
        raise ValueError(message)

    bbox = geodesy.normalise_bbox(request["bbox"])
    frame = geodesy.LocalFrame.for_bbox(bbox)
    layers = request.get("layers", [])

    wants_terrain = "terrain" in layers
    wants_features = any(x != "terrain" for x in layers)
    split = 0.5 if (wants_terrain and wants_features) else 1.0

    grid = None
    if wants_terrain:
        grid = fetch_terrain(bbox, frame, request.get("terrain") or {},
                             progress, base=0.0, span=split)

    features, stats = ({}, {})
    if wants_features:
        features, stats = fetch_features(
            bbox, frame, request, progress,
            base=(split if wants_terrain else 0.0),
            span=(1.0 - split if wants_terrain else 1.0))

    datum = sitespec.datum_offset(grid, request.get("datum", "centre"))

    site = {
        "request": request,
        "bbox": bbox,
        "frame": frame,
        "terrain": grid,
        "features": features,
        "stats": stats,
        "datum_m": datum,
    }

    if grid is not None and (request.get("features") or {}).get("drape", True):
        _drape(site)

    return site


def _drape(site):
    """Attach a terrain height to every feature so nothing floats or buries.

    Buildings get the LOWEST terrain height under their footprint, so a block
    on a slope sits on the ground at its downhill corner rather than hovering
    at its uphill one. Roads get a height per vertex, following the ground.
    """
    grid = site["terrain"]
    datum = site["datum_m"]
    features = site.get("features") or {}

    for key in ("buildings", "water", "green"):
        for feature in features.get(key) or []:
            heights = [grid.sample(x, y) for x, y in feature["outer"]]
            feature["base_z"] = (min(heights) - datum) if heights else 0.0

    for key in ("roads", "rail"):
        for feature in features.get(key) or []:
            feature["z"] = [grid.sample(x, y) - datum for x, y in feature["points"]]
