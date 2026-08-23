"""Projected coordinate reference systems.

The default frame in geodesy.py is a tangent plane: fine for a standalone
context model, useless if the project is set out on a survey grid. This
module projects properly, so an imported site can match a surveyor's data.

Three separate corrections matter, and they are often confused:

  Grid convergence   True north and grid north are not the same direction.
                     The difference reaches about 3 degrees at a zone edge.
                     OSM and terrain data are true-north referenced; a survey
                     grid is not.
  Point scale factor A Transverse Mercator grid compresses distance near the
                     central meridian and stretches it near the zone edge, by
                     up to about 1:2500. Over a 2 km site that is a metre.
  Datum              Tiles and OSM are WGS84. Feeding WGS84 latitude and
                     longitude straight into British National Grid without an
                     OSGB36 transformation puts you about 100 m out.

Projecting handles the first two implicitly - projected coordinates already
carry convergence and scale - so a site built from projected coordinates has
the right shape and orientation with no extra rotation applied. The datum is
a separate step and has to be done deliberately.

Only Transverse Mercator is implemented. That covers UTM, MGA, NZTM, the
British and Irish grids, and the TM half of US State Plane. Lambert Conformal
Conic zones are deliberately absent rather than silently approximated.

Pure Python 2.7 / 3.x. No third-party dependencies, no Revit, no .NET.
"""

import math


# --------------------------------------------------------------- ellipsoids

class Ellipsoid(object):
    """A reference ellipsoid, by semi-major axis and inverse flattening."""

    def __init__(self, name, a, inv_f):
        self.name = name
        self.a = float(a)
        self.inv_f = float(inv_f)
        self.f = 1.0 / self.inv_f
        self.b = self.a * (1.0 - self.f)
        self.e2 = self.f * (2.0 - self.f)       # first eccentricity squared
        self.n = self.f / (2.0 - self.f)        # third flattening


WGS84 = Ellipsoid("WGS 84", 6378137.0, 298.257223563)
GRS80 = Ellipsoid("GRS 1980", 6378137.0, 298.257222101)
AIRY1830 = Ellipsoid("Airy 1830", 6377563.396, 299.3249646)
AIRY_MOD = Ellipsoid("Airy 1830 Modified", 6377340.189, 299.3249646)


# ---------------------------------------------------------- datum transforms

class Helmert(object):
    """A 7-parameter geocentric datum transformation.

    Translations in metres, rotations in arc-seconds, scale in ppm, in the
    position-vector convention. Accurate to a few metres at best: it is a
    rigid-body fit to a distortion field that is not rigid. Where a national
    grid shift file exists (OSTN15, NADCON) that file is authoritative and
    this is an approximation, so accuracy_m records how rough.
    """

    def __init__(self, tx, ty, tz, rx_sec, ry_sec, rz_sec, s_ppm,
                 accuracy_m=5.0, note=""):
        self.tx, self.ty, self.tz = float(tx), float(ty), float(tz)
        to_rad = math.pi / (180.0 * 3600.0)
        self.rx = float(rx_sec) * to_rad
        self.ry = float(ry_sec) * to_rad
        self.rz = float(rz_sec) * to_rad
        self.s = float(s_ppm) * 1e-6
        self.accuracy_m = float(accuracy_m)
        self.note = note

    def apply(self, x, y, z):
        """Transform geocentric cartesian coordinates. -> (x, y, z)."""
        m = 1.0 + self.s
        return (self.tx + m * (x - self.rz * y + self.ry * z),
                self.ty + m * (self.rz * x + y - self.rx * z),
                self.tz + m * (-self.ry * x + self.rx * y + z))


# Ordnance Survey's own published Helmert fit for WGS84 to OSGB36. OS state it
# is good to a few metres; OSTN15 is the exact transformation and needs a
# 20 MB grid file, which is not worth shipping for context massing.
WGS84_TO_OSGB36 = Helmert(-446.448, 125.157, -542.060,
                          -0.1502, -0.2470, -0.8421, 20.4894,
                          accuracy_m=5.0,
                          note="Helmert fit; OSTN15 is the exact transform")


def geodetic_to_cartesian(lon, lat, ellipsoid, height=0.0):
    """Geodetic degrees -> geocentric cartesian metres."""
    phi, lam = math.radians(lat), math.radians(lon)
    sin_phi, cos_phi = math.sin(phi), math.cos(phi)
    nu = ellipsoid.a / math.sqrt(1.0 - ellipsoid.e2 * sin_phi * sin_phi)
    return ((nu + height) * cos_phi * math.cos(lam),
            (nu + height) * cos_phi * math.sin(lam),
            ((1.0 - ellipsoid.e2) * nu + height) * sin_phi)


def cartesian_to_geodetic(x, y, z, ellipsoid):
    """Geocentric cartesian metres -> (lon, lat) degrees.

    Iterative rather than closed-form: it converges in a handful of passes and
    stays readable.
    """
    lam = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-12:                               # on the polar axis
        return math.degrees(lam), (90.0 if z > 0 else -90.0)
    phi = math.atan2(z, p * (1.0 - ellipsoid.e2))
    for _ in range(12):
        sin_phi = math.sin(phi)
        nu = ellipsoid.a / math.sqrt(1.0 - ellipsoid.e2 * sin_phi * sin_phi)
        phi_next = math.atan2(z + ellipsoid.e2 * nu * sin_phi, p)
        if abs(phi_next - phi) < 1e-14:
            phi = phi_next
            break
        phi = phi_next
    return math.degrees(lam), math.degrees(phi)


def shift_datum(lon, lat, from_ellipsoid, to_ellipsoid, helmert):
    """Move a lon/lat between datums. -> (lon, lat) degrees."""
    if helmert is None:
        return lon, lat
    x, y, z = geodetic_to_cartesian(lon, lat, from_ellipsoid)
    nx, ny, nz = helmert.apply(x, y, z)
    return cartesian_to_geodetic(nx, ny, nz, to_ellipsoid)


# ------------------------------------------------------ transverse mercator

def _wrap180(deg):
    """Fold a longitude difference into -180..180."""
    return (deg + 180.0) % 360.0 - 180.0


class TransverseMercator(object):
    """Transverse Mercator via the Kruger n-series.

    Sub-millimetre within about 3 degrees of the central meridian and still
    only millimetres out towards a zone edge, which is well inside anything
    this is used for. The series is the standard one used by Ordnance Survey
    and Geoscience Australia, carried to 4th order in the third flattening.
    """

    def __init__(self, ellipsoid, lon0, lat0, k0, false_east, false_north):
        self.ellipsoid = ellipsoid
        self.lon0 = float(lon0)
        self.lat0 = float(lat0)
        self.k0 = float(k0)
        self.false_east = float(false_east)
        self.false_north = float(false_north)

        n = ellipsoid.n
        n2, n3, n4 = n * n, n ** 3, n ** 4
        self.A = ellipsoid.a / (1.0 + n) * (1.0 + n2 / 4.0 + n4 / 64.0)
        self.alpha = (
            n / 2.0 - 2.0 * n2 / 3.0 + 5.0 * n3 / 16.0 + 41.0 * n4 / 180.0,
            13.0 * n2 / 48.0 - 3.0 * n3 / 5.0 + 557.0 * n4 / 1440.0,
            61.0 * n3 / 240.0 - 103.0 * n4 / 140.0,
            49561.0 * n4 / 161280.0,
        )
        self.beta = (
            n / 2.0 - 2.0 * n2 / 3.0 + 37.0 * n3 / 96.0 - n4 / 360.0,
            n2 / 48.0 + n3 / 15.0 - 437.0 * n4 / 1440.0,
            17.0 * n3 / 480.0 - 37.0 * n4 / 840.0,
            4397.0 * n4 / 161280.0,
        )
        self.delta = (
            2.0 * n - 2.0 * n2 / 3.0 - 2.0 * n3,
            7.0 * n2 / 3.0 - 8.0 * n3 / 5.0,
            56.0 * n3 / 15.0,
            4279.0 * n4 / 630.0,
        )
        # Meridian term for the latitude of origin, subtracted so that lat0
        # lands exactly on the false northing.
        self._xi0 = self._meridian_xi(math.radians(self.lat0))

    def _conformal_t(self, phi):
        e = math.sqrt(self.ellipsoid.e2)
        sin_phi = math.sin(phi)
        return math.sinh(math.atanh(sin_phi) - e * math.atanh(e * sin_phi))

    def _meridian_xi(self, phi):
        """The xi term on the central meridian, where the longitude offset is 0."""
        if abs(phi) < 1e-15:
            return 0.0
        xi_p = math.atan2(self._conformal_t(phi), 1.0)
        xi = xi_p
        for j, a in enumerate(self.alpha, start=1):
            xi += a * math.sin(2.0 * j * xi_p)
        return xi

    def forward(self, lon, lat):
        """Geodetic degrees on THIS system's datum -> (easting, northing)."""
        phi = math.radians(lat)
        d_lam = math.radians(_wrap180(lon - self.lon0))

        t = self._conformal_t(phi)
        xi_p = math.atan2(t, math.cos(d_lam))
        eta_p = math.atanh(math.sin(d_lam) / math.sqrt(1.0 + t * t))

        xi, eta = xi_p, eta_p
        for j, a in enumerate(self.alpha, start=1):
            xi += a * math.sin(2.0 * j * xi_p) * math.cosh(2.0 * j * eta_p)
            eta += a * math.cos(2.0 * j * xi_p) * math.sinh(2.0 * j * eta_p)

        east = self.false_east + self.k0 * self.A * eta
        north = self.false_north + self.k0 * self.A * (xi - self._xi0)
        return east, north

    def inverse(self, east, north):
        """(easting, northing) -> geodetic degrees on THIS system's datum."""
        eta = (east - self.false_east) / (self.k0 * self.A)
        xi = (north - self.false_north) / (self.k0 * self.A) + self._xi0

        xi_p, eta_p = xi, eta
        for j, b in enumerate(self.beta, start=1):
            xi_p -= b * math.sin(2.0 * j * xi) * math.cosh(2.0 * j * eta)
            eta_p -= b * math.cos(2.0 * j * xi) * math.sinh(2.0 * j * eta)

        chi = math.asin(max(-1.0, min(1.0, math.sin(xi_p) / math.cosh(eta_p))))
        phi = chi
        for j, d in enumerate(self.delta, start=1):
            phi += d * math.sin(2.0 * j * chi)

        lam = math.atan2(math.sinh(eta_p), math.cos(xi_p))
        return _wrap180(self.lon0 + math.degrees(lam)), math.degrees(phi)

    # -- derived quantities ----------------------------------------------

    def convergence(self, lon, lat):
        """Angle from grid north to true north at a point, in degrees.

        Found by projecting a short step along the meridian and measuring how
        it leans on the grid. Numerical differentiation rather than another
        hand-transcribed series: fewer coefficients to mistype, and the tests
        check it against the standard analytic approximation.
        """
        step = 1e-6                             # degrees, roughly 0.1 m
        e0, n0 = self.forward(lon, lat - step)
        e1, n1 = self.forward(lon, lat + step)
        # Sign convention: convergence is positive when grid north lies EAST
        # of true north, which is the opposite sense to the raw atan2 of the
        # meridian's lean. Getting this backwards flips every site by twice
        # the convergence, so it is checked against the standard analytic
        # approximation in the tests.
        return -math.degrees(math.atan2(e1 - e0, n1 - n0))

    def scale_factor(self, lon, lat):
        """Point scale factor: grid distance over true distance."""
        step = 1e-6
        e0, n0 = self.forward(lon - step, lat)
        e1, n1 = self.forward(lon + step, lat)
        grid = math.hypot(e1 - e0, n1 - n0)

        phi = math.radians(lat)
        sin_phi = math.sin(phi)
        nu = self.ellipsoid.a / math.sqrt(
            1.0 - self.ellipsoid.e2 * sin_phi * sin_phi)
        true = nu * math.cos(phi) * math.radians(2.0 * step)
        return grid / true if true else 1.0


# ----------------------------------------------------------------- registry

class System(object):
    """A named coordinate reference system, ready to project into."""

    def __init__(self, code, name, projection, datum_shift=None,
                 source_ellipsoid=WGS84, area=""):
        self.code = code
        self.name = name
        self.projection = projection
        self.datum_shift = datum_shift          # WGS84 -> this system's datum
        self.source_ellipsoid = source_ellipsoid
        self.area = area

    @property
    def accuracy_m(self):
        """Worst-case positional error introduced by the datum step."""
        return self.datum_shift.accuracy_m if self.datum_shift else 0.0

    def forward(self, lon, lat):
        """WGS84 degrees -> (easting, northing) in this system."""
        if self.datum_shift is not None:
            lon, lat = shift_datum(lon, lat, self.source_ellipsoid,
                                   self.projection.ellipsoid, self.datum_shift)
        return self.projection.forward(lon, lat)

    def inverse(self, east, north):
        """(easting, northing) -> WGS84 degrees."""
        target_lon, target_lat = self.projection.inverse(east, north)
        if self.datum_shift is None:
            return target_lon, target_lat

        # Invert the Helmert by fixed-point iteration rather than deriving a
        # second parameter set: one authoritative set cannot drift out of
        # step with itself. Converges in three or four passes.
        lon, lat = target_lon, target_lat
        for _ in range(8):
            got_lon, got_lat = shift_datum(
                lon, lat, self.source_ellipsoid,
                self.projection.ellipsoid, self.datum_shift)
            d_lon, d_lat = target_lon - got_lon, target_lat - got_lat
            lon += d_lon
            lat += d_lat
            if abs(d_lon) < 1e-12 and abs(d_lat) < 1e-12:
                break
        return lon, lat

    def convergence(self, lon, lat):
        if self.datum_shift is not None:
            lon, lat = shift_datum(lon, lat, self.source_ellipsoid,
                                   self.projection.ellipsoid, self.datum_shift)
        return self.projection.convergence(lon, lat)

    def scale_factor(self, lon, lat):
        if self.datum_shift is not None:
            lon, lat = shift_datum(lon, lat, self.source_ellipsoid,
                                   self.projection.ellipsoid, self.datum_shift)
        return self.projection.scale_factor(lon, lat)

    def __repr__(self):
        return "System(%s, %s)" % (self.code, self.name)


def utm_zone(lon, lat):
    """Standard UTM zone number for a position, with the Norway exceptions."""
    zone = int(math.floor((_wrap180(lon) + 180.0) / 6.0)) + 1
    if 56.0 <= lat < 64.0 and 3.0 <= lon < 12.0:
        zone = 32                               # widened for south-west Norway
    if 72.0 <= lat < 84.0 and 0.0 <= lon < 42.0:
        # Svalbard: zones 32, 34 and 36 are absorbed by their neighbours.
        zone = 31 if lon < 9.0 else 33 if lon < 21.0 else 35 if lon < 33.0 else 37
    return zone


def utm(lon, lat):
    """The UTM system covering a position. -> System."""
    zone = utm_zone(lon, lat)
    south = lat < 0.0
    lon0 = zone * 6.0 - 183.0
    epsg = (32700 if south else 32600) + zone
    projection = TransverseMercator(
        WGS84, lon0=lon0, lat0=0.0, k0=0.9996,
        false_east=500000.0, false_north=10000000.0 if south else 0.0)
    return System("EPSG:%d" % epsg,
                  "UTM zone %d%s (WGS 84)" % (zone, "S" if south else "N"),
                  projection, area="global")


def mga2020(zone):
    """Map Grid of Australia 2020. -> System.

    GDA2020 is realised on GRS80 and was aligned to ITRF2014 at epoch 2020.0,
    so no datum shift is applied from WGS84. The residual is plate motion
    since 2020 - Australia moves about 7 cm a year - which is well under a
    metre and far below the accuracy of the OSM and terrain data being
    placed.
    """
    projection = TransverseMercator(
        GRS80, lon0=zone * 6.0 - 183.0, lat0=0.0, k0=0.9996,
        false_east=500000.0, false_north=10000000.0)
    return System("EPSG:%d" % (7800 + zone), "MGA2020 zone %d" % zone,
                  projection, area="Australia")


def _registry():
    """Fixed systems, by code. UTM and MGA zones are generated on demand."""
    systems = {}

    # British National Grid. The Helmert datum step dominates the error here.
    systems["EPSG:27700"] = System(
        "EPSG:27700", "British National Grid (OSGB36)",
        TransverseMercator(AIRY1830, lon0=-2.0, lat0=49.0, k0=0.9996012717,
                           false_east=400000.0, false_north=-100000.0),
        datum_shift=WGS84_TO_OSGB36, area="Great Britain")

    # New Zealand Transverse Mercator. NZGD2000 is WGS84-compatible.
    systems["EPSG:2193"] = System(
        "EPSG:2193", "NZGD2000 / New Zealand Transverse Mercator",
        TransverseMercator(GRS80, lon0=173.0, lat0=0.0, k0=0.9996,
                           false_east=1600000.0, false_north=10000000.0),
        area="New Zealand")

    # Irish Transverse Mercator. ETRS89 is WGS84-compatible.
    systems["EPSG:2157"] = System(
        "EPSG:2157", "IRENET95 / Irish Transverse Mercator",
        TransverseMercator(GRS80, lon0=-8.0, lat0=53.5, k0=0.99982,
                           false_east=600000.0, false_north=750000.0),
        area="Ireland")

    for zone in range(49, 57):
        system = mga2020(zone)
        systems[system.code] = system
    return systems


REGISTRY = _registry()

# Lambert Conformal Conic is not implemented, so these must not be silently
# approximated with a Transverse Mercator. Named so the error can say why.
UNSUPPORTED = {
    "lcc": "Lambert Conformal Conic zones (many US State Plane zones, and "
           "several European national grids) need a projection Groundit does "
           "not implement yet.",
}


def resolve(spec, lon, lat):
    """Turn a CRS spec into a System. -> (System, note).

    spec is "auto" or "utm" for the UTM zone covering the site, "none" for no
    projection at all, or an EPSG code such as "EPSG:27700".
    """
    key = (spec or "auto").strip().lower()
    if key in ("none", "local", ""):
        return None, ""
    if key in ("auto", "utm"):
        system = utm(lon, lat)
        return system, "Automatic: %s" % system.name

    code = spec.strip().upper()
    if not code.startswith("EPSG:"):
        code = "EPSG:" + code.lstrip(":")
    system = REGISTRY.get(code)
    if system is None:
        raise ValueError(
            "Groundit does not know %s. Supported: automatic UTM, MGA2020 "
            "zones 49-56, EPSG:27700, EPSG:2193, EPSG:2157." % spec)

    note = ""
    if system.accuracy_m:
        note = ("%s uses a %.0f m datum approximation (%s)."
                % (system.name, system.accuracy_m, system.datum_shift.note))
    return system, note


def describe(system, lon, lat):
    """Facts a user needs before trusting a site. -> dict."""
    if system is None:
        return {"code": "none", "name": "Local tangent plane (no projection)",
                "convergence_deg": 0.0, "scale_factor": 1.0,
                "easting": 0.0, "northing": 0.0, "accuracy_m": 0.0}
    east, north = system.forward(lon, lat)
    return {
        "code": system.code,
        "name": system.name,
        "convergence_deg": system.convergence(lon, lat),
        "scale_factor": system.scale_factor(lon, lat),
        "easting": east,
        "northing": north,
        "accuracy_m": system.accuracy_m,
    }


class ProjectedFrame(object):
    """A drop-in replacement for geodesy.LocalFrame, backed by a projection.

    Same interface, so everything downstream is unchanged: to_local returns
    metres east and north of the site origin. The difference is that those
    metres are GRID metres on a real reference system, carrying its
    convergence and scale factor, rather than a tangent-plane approximation.
    """

    def __init__(self, system, lon0, lat0):
        self.system = system
        self.lon0 = float(lon0)
        self.lat0 = float(lat0)
        self.origin_east, self.origin_north = system.forward(lon0, lat0)

    @classmethod
    def for_bbox(cls, system, bbox):
        west, south, east, north = bbox
        return cls(system, (west + east) / 2.0, (south + north) / 2.0)

    def to_local(self, lon, lat):
        """lon/lat degrees -> (east_m, north_m) from the site origin."""
        east, north = self.system.forward(lon, lat)
        return east - self.origin_east, north - self.origin_north

    def to_lonlat(self, east_m, north_m):
        """(east_m, north_m) -> lon/lat degrees. Inverse of to_local."""
        return self.system.inverse(east_m + self.origin_east,
                                   north_m + self.origin_north)

    def to_dict(self):
        return {"lon0": self.lon0, "lat0": self.lat0,
                "crs": self.system.code,
                "origin_east": self.origin_east,
                "origin_north": self.origin_north}

    def __repr__(self):
        return "ProjectedFrame(%s, lon0=%.6f, lat0=%.6f)" % (
            self.system.code, self.lon0, self.lat0)
