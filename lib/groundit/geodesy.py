"""Coordinate maths: WGS84 lat/lon, web mercator tiles, local ENU metres.

Pure Python. Runs identically under IronPython 2.7 and CPython 3.

Two conventions are used throughout and they are NOT the same order, so every
function says which one it takes:

    bbox    (west, south, east, north)  degrees - OGC / GeoJSON order
    overpass bbox is (south, west, north, east) - converted in overpass.py only

Tiles are the standard 256 px "slippy map" scheme (web mercator, EPSG:3857),
z/x/y with y counted from the north.
"""

import math

# WGS84 semi-major axis. Web mercator treats the earth as a sphere of this
# radius, which is what the tile schemes assume, so use it for tile maths.
EARTH_R = 6378137.0

# Ground resolution of one pixel at zoom 0 on the equator, for 256 px tiles.
# 2 * pi * EARTH_R / 256.
EQUATOR_M_PER_PIXEL = 2.0 * math.pi * EARTH_R / 256.0

METRES_PER_FOOT = 0.3048
FEET_PER_METRE = 1.0 / METRES_PER_FOOT

# Terrarium tiles exist from z0 to z15. Past z15 the server 404s, and below
# about z10 the data is too coarse to be worth putting under a building.
MIN_ZOOM = 8
MAX_ZOOM = 15


# ---------------------------------------------------------------- tile maths

def lonlat_to_tile_xy(lon, lat, z):
    """Fractional tile coordinates for a lon/lat at zoom z. -> (x, y) floats."""
    lat = max(-85.05112878, min(85.05112878, lat))
    n = float(2 ** z)
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def tile_xy_to_lonlat(x, y, z):
    """Lon/lat of a fractional tile coordinate. -> (lon, lat) degrees.

    Integer x, y gives the NORTH-WEST corner of tile (x, y).
    """
    n = float(2 ** z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def tile_range(bbox, z):
    """Inclusive integer tile range covering bbox. -> (x0, y0, x1, y1).

    y0 is the northern row (smaller y), y1 the southern.
    """
    west, south, east, north = bbox
    x0f, y0f = lonlat_to_tile_xy(west, north, z)
    x1f, y1f = lonlat_to_tile_xy(east, south, z)
    x0, y0 = int(math.floor(x0f)), int(math.floor(y0f))
    x1, y1 = int(math.floor(x1f)), int(math.floor(y1f))
    limit = 2 ** z - 1
    return (max(0, x0), max(0, y0), min(limit, x1), min(limit, y1))


def tile_count(bbox, z):
    """How many tiles a bbox needs at zoom z."""
    x0, y0, x1, y1 = tile_range(bbox, z)
    return (x1 - x0 + 1) * (y1 - y0 + 1)


def metres_per_pixel(lat, z):
    """Ground size of one tile pixel at this latitude and zoom, in metres."""
    return EQUATOR_M_PER_PIXEL * math.cos(math.radians(lat)) / float(2 ** z)


def zoom_for_pixel_size(lat, target_m):
    """Smallest zoom whose pixels are at least as fine as target_m. -> int.

    Clamped to the range the terrain tile server actually serves.
    """
    z = MIN_ZOOM
    while z < MAX_ZOOM and metres_per_pixel(lat, z) > target_m:
        z += 1
    return z


# --------------------------------------------------------------- bbox helpers

def bbox_centre(bbox):
    """-> (lon, lat) of the bbox centre."""
    west, south, east, north = bbox
    return (west + east) / 2.0, (south + north) / 2.0


def bbox_size_m(bbox):
    """Approximate ground size of a bbox. -> (width_m, height_m).

    Uses the centre latitude for the east-west scale, which is what the local
    frame does too, so the two agree.
    """
    west, south, east, north = bbox
    _, clat = bbox_centre(bbox)
    width = math.radians(east - west) * EARTH_R * math.cos(math.radians(clat))
    height = math.radians(north - south) * EARTH_R
    return abs(width), abs(height)


def normalise_bbox(bbox):
    """Sort a bbox into (west, south, east, north) regardless of drag direction."""
    a, b, c, d = [float(v) for v in bbox]
    return (min(a, c), min(b, d), max(a, c), max(b, d))


def bbox_is_sane(bbox, max_span_m=20000.0):
    """-> (ok, message). Guards against empty and absurd requests."""
    west, south, east, north = bbox
    if not (-180.0 <= west < east <= 180.0):
        return False, "Longitude bounds are out of order or out of range."
    if not (-85.0 <= south < north <= 85.0):
        return False, "Latitude bounds are out of order or outside web mercator coverage."
    w, h = bbox_size_m(bbox)
    if w < 20.0 or h < 20.0:
        return False, "That area is smaller than 20 m across. Draw a bigger box."
    if w > max_span_m or h > max_span_m:
        return False, ("That area is %.1f x %.1f km. The limit is %.0f km a side - "
                       "OSM and the terrain server will both time out on more."
                       % (w / 1000.0, h / 1000.0, max_span_m / 1000.0))
    return True, ""


# ----------------------------------------------------------------- local frame

class LocalFrame(object):
    """A local east-north-up tangent plane in metres, centred on a lon/lat.

    An equirectangular projection about the origin: east scales by cos(lat0),
    north is a straight arc length. Over a few kilometres this is sub-metre
    against a proper transverse mercator, which is far below both DEM accuracy
    (about 30 m posts) and OSM positional accuracy, so the extra machinery of
    a real UTM projection would buy nothing here.

    Deliberately NOT a map projection with a datum: the output is a local
    engineering grid whose origin the caller pins to the Revit project.
    """

    def __init__(self, lon0, lat0):
        self.lon0 = float(lon0)
        self.lat0 = float(lat0)
        self._kx = math.radians(1.0) * EARTH_R * math.cos(math.radians(self.lat0))
        self._ky = math.radians(1.0) * EARTH_R

    @classmethod
    def for_bbox(cls, bbox):
        lon0, lat0 = bbox_centre(bbox)
        return cls(lon0, lat0)

    def to_local(self, lon, lat):
        """lon/lat degrees -> (east_m, north_m) from the frame origin."""
        return ((lon - self.lon0) * self._kx, (lat - self.lat0) * self._ky)

    def to_lonlat(self, east_m, north_m):
        """(east_m, north_m) -> lon/lat degrees. Inverse of to_local."""
        return (self.lon0 + east_m / self._kx, self.lat0 + north_m / self._ky)

    def to_dict(self):
        return {"lon0": self.lon0, "lat0": self.lat0}

    @classmethod
    def from_dict(cls, d):
        return cls(d["lon0"], d["lat0"])

    def __repr__(self):
        return "LocalFrame(lon0=%.6f, lat0=%.6f)" % (self.lon0, self.lat0)


def rotate(east_m, north_m, angle_rad):
    """Rotate a local point about the frame origin, counter-clockwise.

    Used to align the site to the host project's True North angle.
    """
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return (east_m * c - north_m * s, east_m * s + north_m * c)


def m_to_ft(v):
    return v * FEET_PER_METRE


def ft_to_m(v):
    return v * METRES_PER_FOOT
