"""Polygon and polyline cleanup for OSM geometry.

OSM footprints are drawn by people, not by CAD, and Revit is far fussier than
a map renderer. Raw rings routinely carry duplicate vertices, hairline
segments below Revit's short-curve tolerance, and near-collinear runs of ten
points along one wall. Feed those straight to CurveLoop and Revit throws, or
silently drops the element - which is how naive importers end up with half
the buildings missing.

Everything here works on lists of (x, y) tuples in local metres. Rings are
held OPEN (no repeated closing vertex) and closed only at the point of use.

Pure Python. No Revit, no .NET.
"""

import math

# Revit's ShortCurveTolerance is 1/256 ft, about 1.19 mm. Anything at or under
# it is rejected outright, so clean to a comfortable multiple rather than to
# the edge of the cliff.
REVIT_SHORT_CURVE_M = 0.0012
MIN_SEGMENT_M = 0.05

# How far a vertex may sit off the line through its neighbours before it is
# considered to be carrying real shape information.
COLLINEAR_TOL_M = 0.02


# A mitred corner runs away to infinity as a turn approaches 180 degrees. Cap
# the extension at this multiple of the half-width and bevel beyond it, which
# is what every stroke renderer does and for the same reason.
MITRE_LIMIT = 2.5


def offset_polyline(points, half_width, mitre_limit=MITRE_LIMIT):
    """Offset a polyline to both sides. -> (left, right) point lists.

    Both lists have one point per input vertex, so left[i], right[i] and
    points[i] correspond and a ribbon can be built as quads between
    consecutive stations.

    Corners are mitred: each interior vertex is pushed along the bisector of
    its two segment normals, scaled by 1/cos(half-angle) so the offset edges
    actually meet. Without that scaling the ribbon pinches at every bend.
    Near-reversals (a hairpin, or a doubled-back way) would send the mitre to
    infinity, so the scale is capped.

    Input must already be free of zero-length segments - run clean_polyline
    over it first.
    """
    n = len(points)
    if n < 2 or half_width <= 0:
        return list(points), list(points)

    # Unit direction and left normal of each segment.
    dirs, normals = [], []
    for i in range(n - 1):
        dx = points[i + 1][0] - points[i][0]
        dy = points[i + 1][1] - points[i][1]
        length = math.hypot(dx, dy)
        if length <= 0:
            # clean_polyline should have removed this; stay standing anyway.
            dx, dy, length = 1.0, 0.0, 1.0
        ux, uy = dx / length, dy / length
        dirs.append((ux, uy))
        normals.append((-uy, ux))       # left of travel

    left, right = [], []
    for i in range(n):
        if i == 0:
            nx, ny = normals[0]
            scale = 1.0
        elif i == n - 1:
            nx, ny = normals[-1]
            scale = 1.0
        else:
            n0x, n0y = normals[i - 1]
            n1x, n1y = normals[i]
            mx, my = n0x + n1x, n0y + n1y
            mlen = math.hypot(mx, my)
            if mlen < 1e-9:
                # A true reversal: no bisector exists. Use the incoming
                # normal and accept the overlap rather than emitting NaNs.
                nx, ny = n0x, n0y
                scale = 1.0
            else:
                nx, ny = mx / mlen, my / mlen
                # cos of half the turn angle, via the bisector against a normal
                cos_half = nx * n0x + ny * n0y
                scale = 1.0 / cos_half if cos_half > 1e-6 else mitre_limit
                scale = min(scale, mitre_limit)

        px, py = points[i]
        ox, oy = nx * half_width * scale, ny * half_width * scale
        left.append((px + ox, py + oy))
        right.append((px - ox, py - oy))
    return left, right


def bbox_of(pts):
    """-> (minx, miny, maxx, maxy)."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def signed_area(ring):
    """Shoelace area of an open ring. Positive is counter-clockwise."""
    n = len(ring)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def is_ccw(ring):
    return signed_area(ring) > 0.0


def ensure_ccw(ring):
    """Return the ring wound counter-clockwise."""
    return ring if is_ccw(ring) else list(reversed(ring))


def ensure_cw(ring):
    """Return the ring wound clockwise. Used for extrusion inner loops."""
    return ring if not is_ccw(ring) else list(reversed(ring))


def centroid(ring):
    """Area centroid of an open ring, falling back to the vertex mean."""
    a = signed_area(ring)
    if abs(a) < 1e-9:
        n = float(len(ring)) or 1.0
        return (sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n)
    cx = cy = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    return (cx / (6.0 * a), cy / (6.0 * a))


def perimeter(pts, closed=False):
    total = 0.0
    for i in range(len(pts) - 1):
        total += math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
    if closed and len(pts) > 2:
        total += math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1])
    return total


def drop_repeated_last(pts):
    """OSM closes ways by repeating the first node. Strip that."""
    if len(pts) > 1:
        dx = pts[0][0] - pts[-1][0]
        dy = pts[0][1] - pts[-1][1]
        if math.hypot(dx, dy) < MIN_SEGMENT_M:
            return list(pts[:-1])
    return list(pts)


def dedupe(pts, min_seg=MIN_SEGMENT_M, closed=False):
    """Drop vertices that sit on top of their predecessor."""
    if not pts:
        return []
    out = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_seg:
            out.append(p)
    # On a closed ring the last vertex must also clear the first.
    if closed and len(out) > 2:
        if math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) < min_seg:
            out.pop()
    return out


def _perp_distance(p, a, b):
    """Perpendicular distance from p to the segment ab (not the infinite line)."""
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    px, py = p[0], p[1]
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-18:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / denom
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def drop_collinear(pts, tol=COLLINEAR_TOL_M, closed=False):
    """Remove vertices that add no shape, e.g. mid-wall nodes shared with a road."""
    n = len(pts)
    if n < 3:
        return list(pts)
    out = []
    rng = range(n) if closed else range(1, n - 1)
    if not closed:
        out.append(pts[0])
    for i in rng:
        prev = pts[(i - 1) % n]
        cur = pts[i]
        nxt = pts[(i + 1) % n]
        if _perp_distance(cur, prev, nxt) > tol:
            out.append(cur)
    if not closed:
        out.append(pts[-1])
    return out if len(out) >= (3 if closed else 2) else list(pts)


def simplify(pts, tol):
    """Douglas-Peucker, iterative so deep polylines cannot blow the stack.

    IronPython's default recursion limit is low enough that a long coastline
    way would kill the recursive form, and a stack overflow inside Revit takes
    Revit with it.
    """
    n = len(pts)
    if n < 3 or tol <= 0.0:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        dmax, imax = -1.0, -1
        for k in range(i + 1, j):
            d = _perp_distance(pts[k], pts[i], pts[j])
            if d > dmax:
                dmax, imax = d, k
        if dmax > tol and imax > 0:
            keep[imax] = True
            stack.append((i, imax))
            stack.append((imax, j))
    return [p for p, k in zip(pts, keep) if k]


def simplify_ring(ring, tol):
    """Douglas-Peucker on a closed ring.

    Run on the ring reopened as a polyline that returns to its start, then
    drop the duplicate. Slightly anchors the shape at whichever vertex OSM
    happened to list first, which is standard practice and invisible at
    building scale.
    """
    if len(ring) < 4 or tol <= 0.0:
        return list(ring)
    out = simplify(list(ring) + [ring[0]], tol)
    return drop_repeated_last(out)


def clean_ring(ring, min_seg=MIN_SEGMENT_M, simplify_tol=0.0, min_area=1.0):
    """Full cleanup pipeline for a footprint ring. -> ring, or None if junk.

    None means "do not try to build this", and the caller should count it as
    a skip rather than let Revit raise.
    """
    ring = drop_repeated_last([(float(p[0]), float(p[1])) for p in ring])
    ring = dedupe(ring, min_seg, closed=True)
    if len(ring) < 3:
        return None
    if simplify_tol > 0.0:
        ring = simplify_ring(ring, simplify_tol)
        ring = dedupe(ring, min_seg, closed=True)
    ring = drop_collinear(ring, COLLINEAR_TOL_M, closed=True)
    if len(ring) < 3:
        return None
    if abs(signed_area(ring)) < min_area:
        return None
    return ring


def clean_polyline(pts, min_seg=MIN_SEGMENT_M, simplify_tol=0.0, min_length=1.0):
    """Full cleanup pipeline for an open polyline. -> pts, or None if junk."""
    pts = dedupe([(float(p[0]), float(p[1])) for p in pts], min_seg)
    if len(pts) < 2:
        return None
    if simplify_tol > 0.0:
        pts = simplify(pts, simplify_tol)
        pts = dedupe(pts, min_seg)
    if len(pts) < 2 or perimeter(pts) < min_length:
        return None
    return pts


def point_in_ring(pt, ring):
    """Ray casting point-in-polygon. Used to pair holes with their outer ring."""
    x, y = pt[0], pt[1]
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi + 1e-18) + xi:
                inside = not inside
        j = i
    return inside


def assign_holes(outers, inners):
    """Pair inner rings to the outer ring that contains them.

    -> list of (outer, [inners]). Inners matching no outer are dropped, which
    happens when a multipolygon is clipped by the bbox.
    """
    paired = [(o, []) for o in outers]
    for inner in inners:
        if not inner:
            continue
        probe = inner[0]
        best = None
        best_area = None
        for idx, (outer, _holes) in enumerate(paired):
            if point_in_ring(probe, outer):
                area = abs(signed_area(outer))
                # Smallest containing ring wins, for nested multipolygons.
                if best_area is None or area < best_area:
                    best, best_area = idx, area
        if best is not None:
            paired[best][1].append(inner)
    return paired


def stitch_ways(ways):
    """Join unordered way fragments into rings, for OSM multipolygon members.

    Relation members arrive as arbitrary fragments in arbitrary directions;
    a ring is only recoverable by matching endpoints. -> list of closed rings.
    """
    remaining = [list(w) for w in ways if w and len(w) >= 2]
    rings = []
    tol = 0.5                            # metres; OSM shares exact nodes anyway

    def close_enough(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1]) <= tol

    while remaining:
        chain = remaining.pop(0)
        progressed = True
        while progressed and not close_enough(chain[0], chain[-1]):
            progressed = False
            for i, frag in enumerate(remaining):
                if close_enough(chain[-1], frag[0]):
                    chain.extend(frag[1:])
                elif close_enough(chain[-1], frag[-1]):
                    chain.extend(list(reversed(frag))[1:])
                elif close_enough(chain[0], frag[-1]):
                    chain = frag[:-1] + chain
                elif close_enough(chain[0], frag[0]):
                    chain = list(reversed(frag))[:-1] + chain
                else:
                    continue
                remaining.pop(i)
                progressed = True
                break
        if len(chain) >= 4 and close_enough(chain[0], chain[-1]):
            rings.append(drop_repeated_last(chain))
    return rings
