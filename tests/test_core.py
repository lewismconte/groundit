# -*- coding: utf-8 -*-
"""Regression tests for the pure geo core.

No pytest, no dependencies: run it with any Python 3, or with IronPython to
prove the core behaves the same inside Revit.

    python tests/test_core.py
    python tests/test_core.py --online     # also hit the real services

The offline tests build their own PNGs so the decoder is exercised on all five
scanline filter types, not just the ones a given terrain tile happens to use.
"""

import math
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from groundit import geodesy, geom, overpass, sitespec, source, tiles  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print("  %s %s%s" % ("ok  " if condition else "FAIL", name,
                         "" if condition else "   <- " + str(detail)))


# ------------------------------------------------------------ png fixtures

def encode_png(width, height, rgb, filter_type=0):
    """Minimal PNG writer, used only to feed the decoder known input."""
    stride = width * 3
    raw = bytearray()
    prev = bytearray(stride)
    for row in range(height):
        line = bytearray(rgb[row * stride:(row + 1) * stride])
        out = bytearray(line)
        if filter_type == 1:
            for i in range(stride - 1, 2, -1):
                out[i] = (line[i] - line[i - 3]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                out[i] = (line[i] - prev[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - 3] if i >= 3 else 0
                out[i] = (line[i] - ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                a = line[i - 3] if i >= 3 else 0
                b = prev[i]
                c = prev[i - 3] if i >= 3 else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                out[i] = (line[i] - pr) & 0xFF
        raw.append(filter_type)
        raw.extend(out)
        prev = line

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def test_png_decoder():
    print("PNG decoder")
    width, height = 17, 11           # deliberately not a round number
    rgb = bytearray()
    for y in range(height):
        for x in range(width):
            rgb.extend([(x * 13 + y * 7) & 0xFF, (x * 5) & 0xFF, (y * 29) & 0xFF])

    for filter_type in range(5):
        png = encode_png(width, height, rgb, filter_type)
        w, h, out = tiles.decode_png_pure(png)
        check("filter type %d round-trips" % filter_type,
              (w, h) == (width, height) and bytes(out) == bytes(rgb))


def test_terrarium():
    print("Terrarium encoding")
    check("zero point", abs(tiles.elevation_from_rgb(128, 0, 0) - 0.0) < 1e-9)
    check("+1 m", abs(tiles.elevation_from_rgb(128, 1, 0) - 1.0) < 1e-9)
    check("-1 m", abs(tiles.elevation_from_rgb(127, 255, 0) + 1.0) < 1e-9)
    # A metre of relief must survive the fractional blue channel.
    check("sub-metre precision",
          abs(tiles.elevation_from_rgb(128, 10, 128) - 10.5) < 1e-9)


def test_geodesy():
    print("Geodesy")
    for lon, lat in ((-122.43, 37.79), (0.0, 51.5), (151.2, -33.87), (13.4, 52.5)):
        worst = 0.0
        for z in (8, 12, 15, 19):
            x, y = geodesy.lonlat_to_tile_xy(lon, lat, z)
            back = geodesy.tile_xy_to_lonlat(x, y, z)
            worst = max(worst, abs(back[0] - lon), abs(back[1] - lat))
        check("tile round-trip at %.1f,%.1f" % (lon, lat), worst < 1e-9, worst)

    frame = geodesy.LocalFrame(-122.44, 37.75)
    east, north = frame.to_local(-122.43, 37.76)
    back = frame.to_lonlat(east, north)
    check("frame round-trip", abs(back[0] + 122.43) < 1e-9 and abs(back[1] - 37.76) < 1e-9)

    # One degree of latitude is about 111 km everywhere.
    _, one_degree = geodesy.LocalFrame(0.0, 0.0).to_local(0.0, 1.0)
    check("degree of latitude is 111 km", abs(one_degree - 111319.5) < 1.0, one_degree)

    # Rotating by a full turn is the identity.
    rx, ry = geodesy.rotate(100.0, 50.0, 2 * math.pi)
    check("full rotation is identity", abs(rx - 100) < 1e-6 and abs(ry - 50) < 1e-6)

    # A quarter turn takes east to north.
    rx, ry = geodesy.rotate(100.0, 0.0, math.pi / 2)
    check("quarter turn east to north", abs(rx) < 1e-6 and abs(ry - 100) < 1e-6)

    ok, _ = geodesy.bbox_is_sane((-0.001, 51.5, 0.001, 51.502))
    check("sane bbox accepted", ok)
    ok, _ = geodesy.bbox_is_sane((-1.0, 51.0, 1.0, 52.0))
    check("huge bbox rejected", not ok)
    ok, _ = geodesy.bbox_is_sane((0.0, 51.5, 0.00001, 51.50001))
    check("tiny bbox rejected", not ok)
    check("bbox normalised from any drag direction",
          geodesy.normalise_bbox((1.0, 2.0, -1.0, -2.0)) == (-1.0, -2.0, 1.0, 2.0))


def test_geom():
    print("Geometry cleanup")
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    check("area of a 10 m square", abs(geom.signed_area(square) - 100.0) < 1e-9)
    check("counter-clockwise detected", geom.is_ccw(square))
    check("winding can be forced", not geom.is_ccw(geom.ensure_cw(square)))

    # A mid-wall node adds no shape and should go.
    with_extra = [(0, 0), (5, 0), (10, 0), (10, 10), (0, 10)]
    check("collinear vertex dropped",
          len(geom.drop_collinear(with_extra, closed=True)) == 4)

    # A hairline segment would be rejected by Revit outright.
    hairline = [(0, 0), (10, 0), (10.0001, 0.0001), (10, 10), (0, 10)]
    cleaned = geom.clean_ring(hairline)
    shortest = min(math.hypot(cleaned[i][0] - cleaned[i - 1][0],
                              cleaned[i][1] - cleaned[i - 1][1])
                   for i in range(len(cleaned)))
    check("hairline segment removed", shortest > geom.REVIT_SHORT_CURVE_M * 5, shortest)

    check("closing duplicate dropped",
          len(geom.drop_repeated_last(square + [(0, 0)])) == 4)
    check("degenerate ring rejected", geom.clean_ring([(0, 0), (1, 0), (2, 0)]) is None)
    check("sliver rejected by area",
          geom.clean_ring([(0, 0), (10, 0), (10, 0.01), (0, 0.01)]) is None)

    # Douglas-Peucker keeps the corner and drops the noise.
    noisy = [(i, 0.001 * (i % 2)) for i in range(50)] + [(49, 20)]
    check("simplify collapses a straight run", len(geom.simplify(noisy, 0.1)) <= 4)

    # Holes must be matched to the ring that contains them.
    outer = [(0, 0), (100, 0), (100, 100), (0, 100)]
    hole = [(10, 10), (20, 10), (20, 20), (10, 20)]
    far = [(500, 500), (510, 500), (510, 510)]
    paired = geom.assign_holes([outer], [hole, far])
    check("hole assigned to its outer", len(paired[0][1]) == 1)
    check("hole outside every outer dropped", far not in paired[0][1])
    check("point in ring", geom.point_in_ring((50, 50), outer)
          and not geom.point_in_ring((150, 50), outer))

    # Relation members arrive as unordered, arbitrarily directed fragments.
    fragments = [[(0, 0), (10, 0)], [(10, 10), (10, 0)], [(0, 10), (10, 10)],
                 [(0, 0), (0, 10)]]
    rings = geom.stitch_ways(fragments)
    check("fragments stitched into a ring",
          len(rings) == 1 and len(rings[0]) == 4, rings)


def test_tags():
    print("OSM tag parsing")
    for value, expected in (("12", 12.0), ("12 m", 12.0), ("12m", 12.0),
                            ("40 ft", 12.192), ("40'", 12.192), ("3,5", 3.5),
                            ("15 km", 15000.0), ("junk", None), (None, None)):
        got = overpass.parse_length(value)
        ok = (got is None and expected is None) or \
             (got is not None and expected is not None and abs(got - expected) < 1e-6)
        check("length %r" % value, ok, got)

    check("feet and inches", abs(overpass.parse_length("12'6\"") - 3.81) < 1e-6)

    height, source_of = overpass.building_height({"height": "24"})
    check("height tag preferred", abs(height - 24.0) < 1e-9 and source_of == "height")
    height, source_of = overpass.building_height({"building:levels": "5"})
    check("levels fall back to 3.2 m each",
          abs(height - 16.0) < 1e-9 and source_of == "levels")
    height, source_of = overpass.building_height({"building:levels": "5",
                                                  "roof:levels": "1"})
    check("roof levels counted", abs(height - 19.2) < 1e-9)
    height, source_of = overpass.building_height({})
    check("untagged building gets the default", source_of == "default")

    # OSM contains real nonsense; a 99 m wide lane must not be believed.
    check("absurd width rejected",
          overpass.road_width({"width": "99"}, "living_street") == 5.0)
    check("plausible width kept",
          abs(overpass.road_width({"width": "8"}, "residential") - 8.0) < 1e-9)
    check("lanes used when width absent",
          abs(overpass.road_width({"lanes": "4"}, "primary") - 12.8) < 1e-9)
    check("absurd lane count rejected",
          overpass.road_width({"lanes": "99"}, "residential") == 6.0)


def test_overpass_parse():
    print("Overpass parsing")
    frame = geodesy.LocalFrame(0.0, 51.5)

    def node(lon, lat):
        return {"lon": lon, "lat": lat}

    response = {"elements": [
        {"type": "way", "id": 1, "tags": {"building": "yes", "height": "20",
                                          "name": u"Café Nord"},
         "geometry": [node(0, 51.5), node(0.001, 51.5),
                      node(0.001, 51.5005), node(0, 51.5005), node(0, 51.5)]},
        {"type": "way", "id": 2, "tags": {"highway": "residential", "name": "Test St"},
         "geometry": [node(0, 51.5), node(0.002, 51.5008)]},
        {"type": "relation", "id": 3, "tags": {"building": "yes", "type": "multipolygon"},
         "members": [
             {"type": "way", "role": "outer", "geometry": [
                 node(0.01, 51.5), node(0.012, 51.5), node(0.012, 51.5012),
                 node(0.01, 51.5012), node(0.01, 51.5)]},
             {"type": "way", "role": "inner", "geometry": [
                 node(0.0105, 51.5003), node(0.0115, 51.5003),
                 node(0.0115, 51.5009), node(0.0105, 51.5009), node(0.0105, 51.5003)]},
         ]},
        {"type": "way", "id": 4, "tags": {"amenity": "bench"},
         "geometry": [node(0, 51.5)]},
    ]}

    parsed = overpass.parse(response, frame)
    check("two buildings parsed", len(parsed["buildings"]) == 2,
          len(parsed["buildings"]))
    check("one road parsed", len(parsed["roads"]) == 1)
    check("untagged element skipped", parsed["stats"]["skipped"] >= 1)

    named = [b for b in parsed["buildings"] if b["name"]]
    check("non-ASCII name survives", named and named[0]["name"] == u"Café Nord")

    courtyard = [b for b in parsed["buildings"] if b["holes"]]
    check("relation courtyard became a hole", len(courtyard) == 1)
    if courtyard:
        building = courtyard[0]
        check("outer ring is counter-clockwise", geom.is_ccw(building["outer"]))
        check("hole is clockwise", not geom.is_ccw(building["holes"][0]))
        check("hole sits inside its outer",
              geom.point_in_ring(building["holes"][0][0], building["outer"]))


def test_grid_and_spec():
    print("Grid and site spec")
    bbox = (-122.4450, 37.7500, -122.4350, 37.7570)
    cols, rows = tiles.grid_dims_for_budget(bbox, 8000)
    check("grid fits the budget", cols * rows <= 8000, cols * rows)
    check("grid keeps the bbox aspect",
          abs((cols / float(rows)) - (geodesy.bbox_size_m(bbox)[0]
                                      / geodesy.bbox_size_m(bbox)[1])) < 0.05)

    # A flat synthetic mosaic must sample back flat, and the grid must know it.
    mosaic = tiles.TerrainMosaic(14)
    flat = bytearray()
    for _ in range(256 * 256):
        flat.extend([128, 100, 0])            # exactly 100 m everywhere
    x0, y0, _x1, _y1 = geodesy.tile_range(bbox, 14)
    for x in range(x0, _x1 + 1):
        for y in range(y0, _y1 + 1):
            mosaic.tiles[(x, y)] = flat
            gx0, gy0 = x * 256, y * 256
            mosaic._gx0 = gx0 if mosaic._gx0 is None else min(mosaic._gx0, gx0)
            mosaic._gy0 = gy0 if mosaic._gy0 is None else min(mosaic._gy0, gy0)
            mosaic._gx1 = gx0 + 255 if mosaic._gx1 is None else max(mosaic._gx1, gx0 + 255)
            mosaic._gy1 = gy0 + 255 if mosaic._gy1 is None else max(mosaic._gy1, gy0 + 255)

    frame = geodesy.LocalFrame.for_bbox(bbox)
    grid = tiles.TerrainGrid(frame, bbox, 20, 20).fill(mosaic)
    check("flat terrain samples flat",
          abs(grid.min_elev - 100.0) < 1e-6 and abs(grid.max_elev - 100.0) < 1e-6)
    check("datum centre zeroes the site",
          abs(sitespec.datum_offset(grid, "centre") - 100.0) < 1e-6)
    check("datum min zeroes the low point",
          abs(sitespec.datum_offset(grid, "min") - 100.0) < 1e-6)
    check("datum sealevel keeps true height",
          abs(sitespec.datum_offset(grid, "sealevel")) < 1e-9)
    check("points shift by the datum",
          abs(grid.points(100.0)[0][2]) < 1e-6)

    ok, _ = sitespec.validate_request(sitespec.merge_request({"bbox": list(bbox)}))
    check("valid request accepted", ok)
    ok, message = sitespec.validate_request(sitespec.merge_request({"bbox": None}))
    check("missing bbox rejected", not ok and "Draw a box" in message, message)
    ok, _ = sitespec.validate_request(sitespec.merge_request(
        {"bbox": list(bbox), "layers": []}))
    check("empty layer list rejected", not ok)
    ok, _ = sitespec.validate_request(sitespec.merge_request(
        {"bbox": list(bbox), "datum": "nonsense"}))
    check("unknown datum rejected", not ok)
    ok, _ = sitespec.validate_request(sitespec.merge_request(
        {"bbox": list(bbox), "layers": ["terrain", "unicorns"]}))
    check("unknown layer rejected", not ok)

    estimate = source.estimate(sitespec.merge_request({"bbox": list(bbox)}))
    check("estimate reports tiles and points",
          estimate["tiles"] > 0 and estimate["points"] > 0, estimate)
    check("zoom respects the tile budget",
          geodesy.tile_count(bbox, estimate["zoom"]) <= source.MAX_TILES)


def test_offset_polyline():
    """Ribbon offsetting.

    The invariant that actually matters is perpendicular distance: every
    offset vertex must sit exactly half-width from BOTH segments meeting at
    it. Comparing against hand-computed corner coordinates is easy to get
    wrong (left-of-travel is the inner side of a left turn), the distance
    check is not.
    """
    print("Road ribbon offsetting")
    from groundit import geom

    def perp(p, a, b):
        vx, vy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(vx, vy)
        return abs((p[0] - a[0]) * vy - (p[1] - a[1]) * vx) / length

    def width(l, r, i):
        return math.hypot(l[i][0] - r[i][0], l[i][1] - r[i][1])

    straight = [(0, 0), (10, 0), (20, 0)]
    left, right = geom.offset_polyline(straight, 3.0)
    check("straight run keeps constant width",
          all(abs(width(left, right, i) - 6.0) < 1e-9 for i in range(3)))
    check("straight run stays parallel",
          all(abs(p[1] - 3.0) < 1e-9 for p in left))

    def bend_at(angle_deg):
        angle = math.radians(angle_deg)
        return [(0, 0), (10, 0),
                (10 + 10 * math.cos(angle), 10 * math.sin(angle))]

    # An exact mitre needs a 1/cos(turn/2) extension, which runs away as the
    # turn approaches a reversal. Below the limit the corner is exact; above
    # it the corner is deliberately bevelled instead, so the two regimes get
    # different assertions.
    limit_deg = 2 * math.degrees(math.acos(1.0 / geom.MITRE_LIMIT))
    check("mitre limit bites beyond a sensible turn",
          130.0 < limit_deg < 136.0, limit_deg)

    for angle_deg in (30, 60, 90, 120):
        bend = bend_at(angle_deg)
        left, right = geom.offset_polyline(bend, 3.0)
        good = True
        for side in (left, right):
            for a, b in ((bend[0], bend[1]), (bend[1], bend[2])):
                if abs(perp(side[1], a, b) - 3.0) > 1e-6:
                    good = False
        check("mitre at %d deg sits half-width from both legs" % angle_deg, good)

    for angle_deg in (150, 170):
        bend = bend_at(angle_deg)
        left, right = geom.offset_polyline(bend, 3.0)
        reach = max(math.hypot(side[1][0] - bend[1][0], side[1][1] - bend[1][1])
                    for side in (left, right))
        check("turn at %d deg is bevelled, not spiked" % angle_deg,
              reach <= 3.0 * geom.MITRE_LIMIT + 1e-9, reach)

    hairpin = [(0, 0), (10, 0), (0, 0.5)]
    left, right = geom.offset_polyline(hairpin, 3.0)
    finite = all(v == v and abs(v) < 1e6 for p in left + right for v in p)
    check("hairpin stays finite", finite)
    check("hairpin mitre is capped",
          width(left, right, 1) <= 6.0 * geom.MITRE_LIMIT + 1e-9,
          width(left, right, 1))

    check("single point is handled",
          geom.offset_polyline([(0, 0)], 3.0) == ([(0, 0)], [(0, 0)]))
    check("zero width is handled",
          geom.offset_polyline(straight, 0.0)[0] == straight)

    # Correspondence: one offset vertex per input vertex, so quads can be
    # built between consecutive stations without index juggling.
    path = [(0, 0), (5, 1), (9, 6), (14, 6), (20, 2)]
    left, right = geom.offset_polyline(path, 2.0)
    check("one offset vertex per input vertex",
          len(left) == len(path) and len(right) == len(path))

    # Wide offsets on tight bends may self-intersect; that is acceptable for
    # a context mesh, but it must not produce NaNs or explode.
    tight = [(0, 0), (2, 0), (2.5, 1.5), (0.2, 1.8)]
    left, right = geom.offset_polyline(tight, 8.0)
    check("wide offset on a tight path stays finite",
          all(v == v and abs(v) < 1e6 for p in left + right for v in p))


def test_ribbon_quads():
    """Ribbon placement.

    Regression: the first ribbon implementation never applied the site
    rotation, so roads came in unrotated while terrain and buildings were
    rotated to project north - the roads sat at an angle to everything else.
    It also added a lift converted to feet onto a value in metres.
    """
    print("Ribbon placement")
    from groundit import build, geodesy

    line = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)]
    flat = lambda p: 0.0                                    # noqa: E731
    identity = lambda x, y: (x, y)                          # noqa: E731

    quads = build.ribbon_quads(line, 3.0, flat, identity)
    check("one quad per segment", len(quads) == 2, len(quads))
    check("each quad has four corners", all(len(q) == 4 for q in quads))

    # Rotation must reach the output. Compare against rotating by hand.
    angle = math.radians(37.0)
    rotate = lambda x, y: geodesy.rotate(x, y, angle)        # noqa: E731
    turned = build.ribbon_quads(line, 3.0, flat, rotate)

    good = True
    for plain_quad, turned_quad in zip(quads, turned):
        for (px, py, _pz), (tx, ty, _tz) in zip(plain_quad, turned_quad):
            ex, ey = geodesy.rotate(px, py, angle)
            if abs(ex - tx) > 1e-9 or abs(ey - ty) > 1e-9:
                good = False
    check("rotation is applied to ribbon points", good)
    check("rotation actually moved them",
          abs(turned[0][0][0] - quads[0][0][0]) > 1e-6)

    # Sampling must happen BEFORE placement: a sampler keyed to unrotated
    # coordinates must see unrotated coordinates.
    seen = []

    def spy(p):
        seen.append(p)
        return 0.0

    build.ribbon_quads(line, 3.0, spy, rotate)
    check("terrain is sampled in unrotated space",
          all(abs(p[1]) <= 3.0 + 1e-9 for p in seen), seen[:3])

    # Lift is metres, matching _xyz. A feet conversion would treble it.
    lifted = build.ribbon_quads(line, 3.0, flat, identity, 0.12)
    check("lift is applied in metres",
          all(abs(c[2] - 0.12) < 1e-9 for q in lifted for c in q),
          lifted[0][0])

    # Height comes from the sampler, per corner, not one value per road.
    slope = lambda p: p[0] * 0.1                             # noqa: E731
    sloped = build.ribbon_quads(line, 3.0, slope, identity)
    check("each corner carries its own sampled height",
          abs(sloped[0][0][2] - 0.0) < 1e-9
          and abs(sloped[0][1][2] - 1.0) < 1e-9, sloped[0])

    check("degenerate input yields nothing",
          build.ribbon_quads([(0.0, 0.0)], 3.0, flat, identity) == []
          and build.ribbon_quads(line, 0.0, flat, identity) == [])


def test_sidecar():
    """The record that makes an update possible.

    Written beside the .rvt so a site's settings can be read back without
    opening the site document. Runs headless because these functions touch
    only json and os, never the Revit API.
    """
    print("Site sidecar")
    import shutil
    import tempfile
    from groundit import build

    folder = tempfile.mkdtemp(prefix="groundit_test_")
    try:
        rvt = os.path.join(folder, "Harbour_20260814_1030.rvt")
        check("sidecar sits beside the model",
              build.sidecar_path(rvt) == os.path.join(
                  folder, "Harbour_20260814_1030.groundit.json"))

        check("missing sidecar reads as None", build.read_sidecar(rvt) is None)

        request = sitespec.merge_request({
            "name": "Harbour",
            "bbox": [-71.062, 42.354, -71.048, 42.364],
            "layers": ["terrain", "buildings", "roads", "water"],
            "road_style": "centrelines",
            "datum": "min",
        })
        build.write_sidecar(rvt, request, {"bbox": request["bbox"]})
        payload = build.read_sidecar(rvt)

        check("sidecar round-trips", payload is not None)
        check("request survives intact",
              payload["request"]["road_style"] == "centrelines"
              and payload["request"]["datum"] == "min"
              and payload["request"]["layers"] == ["terrain", "buildings",
                                                   "roads", "water"])
        check("bbox survives intact", payload["bbox"] == request["bbox"])
        check("records what wrote it", payload["tool"] == "groundit")
        check("records the spec version",
              payload["spec_version"] == sitespec.SPEC_VERSION)
        check("records when", bool(payload.get("written")))

        # A restored request must still validate, or update would dead-end.
        ok, message = sitespec.validate_request(
            sitespec.merge_request(payload["request"]))
        check("restored request is still valid", ok, message)

        # Someone else's json next to a .rvt must not be mistaken for ours.
        other = os.path.join(folder, "NotOurs.rvt")
        sitespec.write_json(build.sidecar_path(other), {"tool": "something-else"})
        check("foreign sidecar is ignored", build.read_sidecar(other) is None)

        # A corrupt sidecar must not raise; it just means "not updatable".
        broken = os.path.join(folder, "Broken.rvt")
        handle = open(build.sidecar_path(broken), "w")
        handle.write("{ this is not json")
        handle.close()
        check("corrupt sidecar reads as None", build.read_sidecar(broken) is None)

        # Non-ASCII names are the norm in OSM; the sidecar must survive them.
        accented = os.path.join(folder, "Munchen.rvt")
        request["name"] = u"M\xfcnchen Hauptbahnhof"
        build.write_sidecar(accented, request)
        back = build.read_sidecar(accented)
        check("non-ascii name survives",
              back["request"]["name"] == u"M\xfcnchen Hauptbahnhof",
              back["request"]["name"])
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_transport():
    """A broken transport must fall through, not end the run.

    Regression: the first Revit run failed pre-flight with "cannot reach the
    elevation service" on a machine with working internet, because the .NET
    types could not be resolved and there was no fallback and no detail.
    """
    print("Transport fallback")
    from groundit import net

    report = net.transport_report()
    check("transport report names both transports",
          ".NET transport" in report and "urllib transport" in report, report)

    # Preference depends on the runtime: .NET first inside Revit, urllib first
    # headless. Assert the contract, not one environment's answer.
    order = [label for label, _impl in net._transports()]
    check("both transports are in the chain",
          sorted(order) == [".NET", "urllib"], order)
    check("preferred transport matches what resolved",
          order[0] == (".NET" if net._dotnet_types() else "urllib"), order)

    calls = []
    real_dotnet, real_urllib = net._dotnet_fetch, net._urllib_fetch
    try:
        def broken(url, body=None, timeout=60):
            calls.append("broken")
            raise AttributeError("no attribute 'WebRequest'")

        def working(url, body=None, timeout=60):
            calls.append("working")
            return bytearray(b"ok")

        # Break whichever transport is preferred, so the fallback is the one
        # under test either way.
        if order[0] == ".NET":
            net._dotnet_fetch, net._urllib_fetch = broken, working
        else:
            net._urllib_fetch, net._dotnet_fetch = broken, working

        result = net.fetch("https://example.invalid/x", retries=0)
        check("falls through a broken transport", bytes(result) == b"ok")
        check("the preferred transport was tried first",
              calls == ["broken", "working"], calls)

        # Now break both, and confirm the error explains itself.
        def also_broken(url, body=None, timeout=60):
            raise IOError("connection refused")

        net._urllib_fetch = also_broken
        net._dotnet_fetch = broken
        try:
            net.fetch("https://example.invalid/x", retries=0)
            check("total failure raises", False)
        except net.NetworkError as exc:
            text = str(exc)
            check("error names the underlying causes",
                  "AttributeError" in text and "connection refused" in text, text)

        ok, detail = net.check_reachable("https://example.invalid/x", timeout=1)
        check("check_reachable reports why", not ok and len(detail) > 20)
    finally:
        net._dotnet_fetch, net._urllib_fetch = real_dotnet, real_urllib


def test_picker_pump():
    """The threadless serve path Revit uses.

    Two regressions live here. Revit 2027 crashed during the wait when the
    server ran on a background thread beside a hand-built WPF dialog, so the
    server is now pumped inline. And stop() used to call shutdown()
    unconditionally, which blocks forever unless serve_forever() is running -
    freezing Revit in the caller's finally block, right after a good pick.
    """
    print("Picker pump path")
    import json
    import threading
    from groundit import picker
    try:
        from urllib.request import urlopen, Request
    except ImportError:
        from urllib2 import urlopen, Request

    session = picker.Picker({"name": "Project1", "lon": -71.06, "lat": 42.36})
    check("no thread is started", session._thread is None)

    got = {}

    def client():
        try:
            got["page"] = urlopen(session.url, timeout=10).getcode()
            body = json.dumps({"bbox": [-71.07, 42.35, -71.05, 42.37],
                               "layers": ["terrain"], "datum": "centre"}).encode()
            request = Request(
                "http://127.0.0.1:%d/api/submit?t=%s" % (session.port, session.token),
                data=body, headers={"Content-Type": "application/json"})
            got["submit"] = urlopen(request, timeout=10).getcode()
        except Exception as exc:
            got["error"] = str(exc)

    thread = threading.Thread(target=client)
    thread.daemon = True
    thread.start()

    status, pumps = "waiting", 0
    while status == "waiting" and pumps < 150:
        status = session.pump(0.1)
        pumps += 1

    # The handler records the result before it finishes writing the response,
    # so the loop can exit before the client has seen its status code. Let the
    # client finish before asserting on what it got.
    thread.join(5.0)

    check("browser reached a threadless server", got.get("page") == 200, got)
    check("submit accepted", got.get("submit") == 200, got)
    check("request came through intact",
          session.result and session.result["bbox"][0] == -71.07, session.result)
    check("poll reports submitted", status == "submitted", status)

    # The regression: this must return promptly, not block forever.
    done = threading.Event()

    def stopper():
        session.stop()
        done.set()

    stopper_thread = threading.Thread(target=stopper)
    stopper_thread.daemon = True
    stopper_thread.start()
    check("stop() returns without a serve_forever loop", done.wait(5.0))

    # A rejected token must not be accepted as a result.
    guarded = picker.Picker({})
    try:
        rogue = threading.Thread(target=lambda: _post_bad_token(guarded))
        rogue.daemon = True
        rogue.start()
        guarded.pump(0.5)
        check("wrong token rejected", guarded.result is None, guarded.result)
    finally:
        guarded.stop()


def _post_bad_token(session):
    import json
    try:
        from urllib.request import urlopen, Request
    except ImportError:
        from urllib2 import urlopen, Request
    try:
        request = Request("http://127.0.0.1:%d/api/submit?t=wrong" % session.port,
                          data=json.dumps({"bbox": [0, 0, 1, 1]}).encode(),
                          headers={"Content-Type": "application/json"})
        urlopen(request, timeout=5)
    except Exception:
        pass                            # a 403 is the point


def test_online():
    print("Online (real services)")
    from groundit import net
    check("elevation service reachable", net.reachable())

    request = sitespec.merge_request({
        "bbox": [-122.4450, 37.7500, -122.4350, 37.7570],
        "layers": ["terrain", "buildings", "roads"],
        "terrain": {"max_points": 2000, "target_m": 20.0},
    })
    site = source.fetch_site(request)
    grid = site["terrain"]
    check("terrain downloaded", grid is not None and grid.point_count() > 0)
    # Twin Peaks really is this steep; a flat result means a decode bug.
    check("terrain has real relief", (grid.max_elev - grid.min_elev) > 50.0,
          grid.max_elev - grid.min_elev)
    check("elevations are plausible for San Francisco",
          0 < grid.min_elev < 300 and 0 < grid.max_elev < 400,
          (grid.min_elev, grid.max_elev))
    check("buildings downloaded", len(site["features"]["buildings"]) > 100)
    check("roads downloaded", len(site["features"]["roads"]) > 10)
    check("buildings draped onto terrain",
          all("base_z" in b for b in site["features"]["buildings"]))
    check("roads have a height per vertex",
          all(len(r["z"]) == len(r["points"]) for r in site["features"]["roads"]))


def main():
    print("Groundit core tests\n")
    test_png_decoder()
    test_terrarium()
    test_geodesy()
    test_geom()
    test_tags()
    test_overpass_parse()
    test_grid_and_spec()
    test_offset_polyline()
    test_ribbon_quads()
    test_sidecar()
    test_transport()
    test_picker_pump()
    if "--online" in sys.argv:
        test_online()
    else:
        print("Online tests skipped (pass --online to run them)")

    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
