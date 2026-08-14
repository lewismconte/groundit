"""Turn a fetched site into a Revit model, and link it into the host.

The site is built in its OWN document and then linked, rather than dumped into
the host. That keeps thousands of context elements out of the project, lets
the site be reloaded or swapped without touching the building, and matches
how site context is handled in practice.

Everything here is Revit-side: IronPython 2.7, pure ASCII, Python 2 syntax.
The RevitAPI imports are guarded so the module still imports headless, which
is what makes the field guide's import test possible.

Coordinates arrive as local metres from geodesy.LocalFrame, with the frame
origin at the centre of the picked area. That origin lands on the host's
project origin, so a site fetched around a project sits where you expect.
"""

import math

from . import geodesy, geom

try:
    from Autodesk.Revit import DB
except Exception:
    DB = None                           # headless import; nothing below runs

try:
    from System.Collections.Generic import List
except Exception:
    List = None


# Extruded thickness for the flat layers, in metres. Thin enough to read as a
# surface, thick enough that Revit will not reject it as a sliver.
FLAT_THICKNESS_M = 0.15

# Road ribbons sit this far above the terrain. A surface laid exactly on the
# toposolid z-fights with it in shaded views, which reads as a rendering bug.
ROAD_LIFT_M = 0.12

# Above this many ribbon quads in one site, mesh building starts to dominate
# the run. Dense cities with footpaths enabled can exceed it easily, so warn
# rather than grind.
RIBBON_QUAD_BUDGET = 60000

# Revit's shortest permitted curve, in feet. This is Application
# .ShortCurveTolerance, which is an INSTANCE property on the application
# object, not a static on DB - reaching for DB.Application.ShortCurveTolerance
# raises, and it would do so inside the per-building try/except, quietly
# skipping every single building. Hold the known value and refresh it from a
# live document when we have one.
SHORT_CURVE_FT = 1.0 / 256.0


def refresh_tolerance(doc):
    """Take the real short-curve tolerance from a live document."""
    global SHORT_CURVE_FT
    try:
        SHORT_CURVE_FT = doc.Application.ShortCurveTolerance
    except Exception:
        pass

# Buildings go in as Generic Model, not Mass. Mass has its own visibility
# toggle that is OFF in most views, so a Mass import is the classic "it said
# it worked but I cannot see anything" bug report.
BUILDING_CATEGORY = "OST_GenericModel"

LAYER_COLOURS = {
    "buildings": (196, 190, 180),
    "roads": (110, 110, 112),
    "water": (120, 160, 200),
    "green": (150, 180, 130),
    "rail": (90, 90, 95),
    "terrain": (170, 160, 145),
}


# --------------------------------------------------------------- small utils

def _ft(metres):
    return metres * geodesy.FEET_PER_METRE


def _xyz(east_m, north_m, up_m):
    return DB.XYZ(_ft(east_m), _ft(north_m), _ft(up_m))


def _toposolid_class():
    """Toposolid moved around between releases; find it rather than assume.

    Returns (ToposolidClass, ToposolidTypeClass) or (None, None).
    """
    for holder in (DB, getattr(DB, "Architecture", None)):
        if holder is None:
            continue
        cls = getattr(holder, "Toposolid", None)
        typ = getattr(holder, "ToposolidType", None)
        if cls is not None and typ is not None:
            return cls, typ
    return None, None


def _lowest_level(doc):
    levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level))
    if not levels:
        return None
    levels.sort(key=lambda x: x.Elevation)
    return levels[0]


def _category_id(doc, name):
    """ElementId for a BuiltInCategory name, if DirectShape will accept it."""
    bic = getattr(DB.BuiltInCategory, name, None)
    if bic is None:
        return None
    eid = DB.ElementId(bic)
    try:
        if not DB.DirectShape.IsValidCategoryId(eid, doc):
            return None
    except Exception:
        return None
    return eid


def _get_material(doc, key, cache):
    """Find or make a simple shaded material for a layer. -> ElementId."""
    if key in cache:
        return cache[key]
    name = "Groundit %s" % key.capitalize()
    found = None
    for mat in DB.FilteredElementCollector(doc).OfClass(DB.Material):
        if mat.Name == name:
            found = mat
            break
    if found is None:
        try:
            mid = DB.Material.Create(doc, name)
            found = doc.GetElement(mid)
            r, g, b = LAYER_COLOURS.get(key, (180, 180, 180))
            found.Color = DB.Color(r, g, b)
            found.Shininess = 16
        except Exception:
            cache[key] = DB.ElementId.InvalidElementId
            return cache[key]
    cache[key] = found.Id
    return found.Id


def _set_comment(element, text):
    try:
        param = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if param and not param.IsReadOnly:
            param.Set(text)
    except Exception:
        pass


def _curve_loop(ring, z_m):
    """A planar CurveLoop from a cleaned ring. -> CurveLoop, or None.

    Returns None rather than raising: one unbuildable footprint should cost
    one building, not the whole import.
    """
    try:
        loop = DB.CurveLoop()
        count = len(ring)
        for i in range(count):
            a = ring[i]
            b = ring[(i + 1) % count]
            p0 = _xyz(a[0], a[1], z_m)
            p1 = _xyz(b[0], b[1], z_m)
            if p0.DistanceTo(p1) < SHORT_CURVE_FT:
                continue
            loop.Append(DB.Line.CreateBound(p0, p1))
        return loop if not loop.IsOpen() else None
    except Exception:
        return None


def _extrusion(outer, holes, base_z_m, height_m, material_id=None):
    """Extruded solid from an outer ring and its holes. -> Solid, or None.

    The material rides along in SolidOptions rather than being set on the
    element afterwards: DirectShape has no dependable material parameter, and
    the geometry itself is the right place to carry it.
    """
    if height_m <= 0.01:
        return None
    outer_loop = _curve_loop(outer, base_z_m)
    if outer_loop is None:
        return None
    loops = List[DB.CurveLoop]()
    loops.Add(outer_loop)
    for hole in holes or []:
        hole_loop = _curve_loop(hole, base_z_m)
        if hole_loop is not None:
            loops.Add(hole_loop)
    try:
        if material_id is not None and material_id != DB.ElementId.InvalidElementId:
            options = DB.SolidOptions(material_id, DB.ElementId.InvalidElementId)
            return DB.GeometryCreationUtilities.CreateExtrusionGeometry(
                loops, DB.XYZ.BasisZ, _ft(height_m), options)
        return DB.GeometryCreationUtilities.CreateExtrusionGeometry(
            loops, DB.XYZ.BasisZ, _ft(height_m))
    except Exception:
        return None


def _direct_shape(doc, category_id, geometry, name, comment=None):
    """Create a DirectShape holding the given geometry. -> element, or None."""
    try:
        shape = DB.DirectShape.CreateElement(doc, category_id)
        items = List[DB.GeometryObject]()
        for item in geometry:
            items.Add(item)
        shape.SetShape(items)
        if name:
            shape.Name = name[:200]
        if comment:
            _set_comment(shape, comment[:250])
        return shape
    except Exception:
        return None


def ribbon_quads(points, half_width, sample_z, place, lift_m=0.0):
    """Quads for one road ribbon. -> list of 4 (x, y, z) tuples, in METRES.

    Deliberately pure: no Revit types, so the ordering that actually matters
    here can be tested headless. That ordering is:

      1. offset and sample in UNROTATED local metres, because the terrain
         grid lives in that space;
      2. only then apply `place` to rotate points into the host's frame.

    Sampling after rotating reads the terrain at the wrong spot, and skipping
    `place` entirely leaves the roads unrotated while every other layer is
    rotated - which is exactly the bug this function exists to prevent.
    """
    if len(points) < 2 or half_width <= 0:
        return []

    from . import geom as _geom
    left, right = _geom.offset_polyline(points, half_width)

    # Heights sampled in unrotated space, before `place` moves anything.
    lz = [sample_z(p) + lift_m for p in left]
    rz = [sample_z(p) + lift_m for p in right]

    placed_left = [place(x, y) for x, y in left]
    placed_right = [place(x, y) for x, y in right]

    quads = []
    for i in range(len(points) - 1):
        quads.append([
            (placed_left[i][0], placed_left[i][1], lz[i]),
            (placed_left[i + 1][0], placed_left[i + 1][1], lz[i + 1]),
            (placed_right[i + 1][0], placed_right[i + 1][1], rz[i + 1]),
            (placed_right[i][0], placed_right[i][1], rz[i]),
        ])
    return quads


def _tessellated_face(corners, material_id=None):
    """One quad for a TessellatedShapeBuilder. -> TessellatedFace, or None.

    Degenerate quads are dropped rather than passed on: a face with a
    zero-length edge makes the whole Build() fail, taking the entire road
    with it, so one bad corner must not cost the element.
    """
    try:
        for i in range(len(corners)):
            if corners[i].DistanceTo(corners[(i + 1) % len(corners)]) <= SHORT_CURVE_FT:
                return None
        points = List[DB.XYZ]()
        for corner in corners:
            points.Add(corner)
        if material_id is not None:
            return DB.TessellatedFace(points, material_id)
        return DB.TessellatedFace(points, DB.ElementId.InvalidElementId)
    except Exception:
        return None


def _mesh_shape(doc, category_id, faces, name, comment=None):
    """DirectShape from tessellated faces. -> element, or None.

    Built as an open shell (a surface, not a solid) and with Salvage
    fallback, so a road whose quads do not form a watertight body still
    comes through as geometry instead of vanishing.
    """
    try:
        builder = DB.TessellatedShapeBuilder()
        builder.OpenConnectedFaceSet(False)
        for face in faces:
            builder.AddFace(face)
        builder.CloseConnectedFaceSet()
        builder.Target = DB.TessellatedShapeBuilderTarget.Mesh
        builder.Fallback = DB.TessellatedShapeBuilderFallback.Salvage
        builder.Build()
        result = builder.GetBuildResult()
        objects = result.GetGeometricalObjects()
        if not objects or objects.Count == 0:
            return None

        shape = DB.DirectShape.CreateElement(doc, category_id)
        shape.SetShape(objects)
        if name:
            shape.Name = name[:200]
        if comment:
            _set_comment(shape, comment[:250])
        return shape
    except Exception:
        return None


# ------------------------------------------------------------------- builder

class SiteBuilder(object):
    """Builds one site document from a fetched site.

    Counts everything it skips. A silent partial import is worse than a slow
    one, so the caller reports these back to the user.
    """

    def __init__(self, site, options=None):
        self.site = site
        self.options = dict(options or {})
        self.counts = {"terrain": 0, "buildings": 0, "roads": 0,
                       "water": 0, "green": 0, "rail": 0}
        self.skipped = {"buildings": 0, "roads": 0, "water": 0,
                        "green": 0, "rail": 0}
        self.notes = []
        self._materials = {}
        self._rotation = 0.0

    # -- coordinate handling -------------------------------------------------

    def set_rotation(self, angle_rad):
        """Rotate the whole site to match the host's project north."""
        self._rotation = float(angle_rad or 0.0)

    def _place(self, east_m, north_m):
        if not self._rotation:
            return east_m, north_m
        return geodesy.rotate(east_m, north_m, self._rotation)

    def _ring(self, ring):
        return [self._place(x, y) for x, y in ring]

    # -- layers --------------------------------------------------------------

    def build_terrain(self, doc):
        grid = self.site.get("terrain")
        if grid is None:
            return
        datum = self.site.get("datum_m", 0.0)

        points = List[DB.XYZ]()
        for east, north, up in grid.points(datum):
            px, py = self._place(east, north)
            points.Add(_xyz(px, py, up))

        toposolid_cls, toposolid_type_cls = _toposolid_class()
        level = _lowest_level(doc)

        if toposolid_cls is not None and level is not None:
            types = list(DB.FilteredElementCollector(doc).OfClass(toposolid_type_cls))
            if types:
                try:
                    solid = toposolid_cls.Create(doc, points, types[0].Id, level.Id)
                    self.counts["terrain"] = grid.point_count()
                    _set_comment(solid, "Groundit terrain, %d points" % grid.point_count())
                    return
                except Exception as exc:
                    self.notes.append("Toposolid failed (%s); using a topography "
                                      "surface instead." % exc)

        # Pre-2024, or no toposolid type in the template.
        try:
            surface = DB.Architecture.TopographySurface.Create(doc, points)
            self.counts["terrain"] = grid.point_count()
            _set_comment(surface, "Groundit terrain, %d points" % grid.point_count())
        except Exception as exc:
            self.notes.append("Could not build terrain: %s" % exc)

    def build_buildings(self, doc):
        features = (self.site.get("features") or {}).get("buildings") or []
        if not features:
            return
        category = _category_id(doc, BUILDING_CATEGORY) or _category_id(doc, "OST_Mass")
        if category is None:
            self.notes.append("No usable category for buildings.")
            return
        material = _get_material(doc, "buildings", self._materials)

        for feature in features:
            solid = _extrusion(self._ring(feature["outer"]),
                               [self._ring(h) for h in feature.get("holes") or []],
                               feature.get("base_z", 0.0),
                               feature.get("height", 8.0),
                               material)
            if solid is None:
                self.skipped["buildings"] += 1
                continue
            name = feature.get("name") or "Building %s" % feature.get("id")
            comment = "OSM %s | h=%.1fm (%s)" % (
                feature.get("id"), feature.get("height", 0.0),
                feature.get("height_source", "?"))
            shape = _direct_shape(doc, category, [solid], name, comment)
            if shape is None:
                self.skipped["buildings"] += 1
                continue
            self.counts["buildings"] += 1

    def build_flat_areas(self, doc, key):
        features = (self.site.get("features") or {}).get(key) or []
        if not features:
            return
        category = _category_id(doc, "OST_Site") or _category_id(doc, "OST_GenericModel")
        if category is None:
            return
        material = _get_material(doc, key, self._materials)

        for feature in features:
            solid = _extrusion(self._ring(feature["outer"]),
                               [self._ring(h) for h in feature.get("holes") or []],
                               feature.get("base_z", 0.0) - FLAT_THICKNESS_M * 0.5,
                               FLAT_THICKNESS_M, material)
            if solid is None:
                self.skipped[key] += 1
                continue
            shape = _direct_shape(doc, category, [solid],
                                  feature.get("name") or key.capitalize())
            if shape is None:
                self.skipped[key] += 1
                continue
            self.counts[key] += 1

    def build_lines(self, doc, key):
        """Roads and railways as 3D polylines that follow the terrain.

        Curves rather than ribbons: a ribbon needs a mitred offset of every
        polyline, and in a dense city the segment count runs to tens of
        thousands of solids. Centrelines carrying a width parameter give the
        same context at a fraction of the weight, and can be fattened later.
        """
        features = (self.site.get("features") or {}).get(key) or []
        if not features:
            return
        category = (_category_id(doc, "OST_Roads") if key == "roads" else None) \
            or _category_id(doc, "OST_GenericModel")
        if category is None:
            return

        for feature in features:
            points = self._ring(feature["points"])
            heights = feature.get("z") or [0.0] * len(points)
            curves = []
            for i in range(len(points) - 1):
                a = _xyz(points[i][0], points[i][1], heights[i])
                b = _xyz(points[i + 1][0], points[i + 1][1], heights[i + 1])
                try:
                    if a.DistanceTo(b) > SHORT_CURVE_FT:
                        curves.append(DB.Line.CreateBound(a, b))
                except Exception:
                    continue
            if not curves:
                self.skipped[key] += 1
                continue
            comment = "OSM %s | %s | width %.1fm" % (
                feature.get("id"), feature.get("class"), feature.get("width", 0.0))
            shape = _direct_shape(doc, category, curves,
                                  feature.get("name") or feature.get("class"), comment)
            if shape is None:
                self.skipped[key] += 1
                continue
            self.counts[key] += 1

    def build_ribbons(self, doc, key):
        """Roads and railways as surfaces of real width, draped on the terrain.

        A mesh rather than an extrusion, because a road that follows the
        ground is not planar and CreateExtrusionGeometry needs a planar loop.
        TessellatedShapeBuilder takes arbitrary quads directly, which is both
        the correct shape and far cheaper than mitring solids together.

        Each carriageway edge is draped independently rather than by offsetting
        the centreline height: on a cambered or side-sloping road those differ
        by enough to see.
        """
        features = (self.site.get("features") or {}).get(key) or []
        if not features:
            return
        category = (_category_id(doc, "OST_Roads") if key == "roads" else None) \
            or _category_id(doc, "OST_GenericModel")
        if category is None:
            return

        material = _get_material(doc, key, self._materials)
        quads_built = 0

        for feature in features:
            points = geom.clean_polyline(feature["points"], min_length=2.0)
            if not points:
                self.skipped[key] += 1
                continue

            half = max(0.5, float(feature.get("width") or 4.0)) / 2.0
            # ROAD_LIFT_M is metres because _xyz takes metres. Converting it
            # to feet here would triple the lift.
            quads = ribbon_quads(points, half, self._drape, self._place,
                                 ROAD_LIFT_M)

            faces = []
            for quad in quads:
                face = _tessellated_face([_xyz(x, y, z) for x, y, z in quad],
                                         material)
                if face is not None:
                    faces.append(face)

            if not faces:
                self.skipped[key] += 1
                continue

            comment = "OSM %s | %s | width %.1fm" % (
                feature.get("id"), feature.get("class"), half * 2.0)
            shape = _mesh_shape(doc, category, faces,
                                feature.get("name") or feature.get("class"),
                                comment)
            if shape is None:
                self.skipped[key] += 1
                continue
            quads_built += len(faces)
            self.counts[key] += 1

        if quads_built > RIBBON_QUAD_BUDGET:
            self.notes.append(
                "%s produced %d surfaces. Turning road detail down, or "
                "switching to centrelines, will make this site much lighter."
                % (key.title(), quads_built))

    def _drape(self, point):
        """Terrain height in metres at an UNROTATED local point.

        Unrotated because the terrain grid is built in frame coordinates and
        only rotated on its way into Revit. Datum already applied. Falls back
        to the datum plane when terrain was not requested, so ribbons still
        come in flat rather than not at all.
        """
        grid = self.site.get("terrain")
        if grid is None:
            return 0.0
        return grid.sample(point[0], point[1]) - self.site.get("datum_m", 0.0)

    # -- orchestration -------------------------------------------------------

    def build_into(self, doc, progress=None):
        """Populate an open, empty document with the site."""
        refresh_tolerance(doc)
        # "centrelines" | "ribbons" | "both". Ribbons look right; centrelines
        # stay useful for tracing over, so both is a legitimate answer.
        road_style = self.options.get("road_style") or self.site.get(
            "request", {}).get("road_style") or "ribbons"

        def build_ways(key):
            if road_style in ("centrelines", "both"):
                self.build_lines(doc, key)
            if road_style in ("ribbons", "both"):
                self.build_ribbons(doc, key)

        steps = [
            ("terrain", lambda: self.build_terrain(doc)),
            ("buildings", lambda: self.build_buildings(doc)),
            ("roads", lambda: build_ways("roads")),
            ("rail", lambda: build_ways("rail")),
            ("water", lambda: self.build_flat_areas(doc, "water")),
            ("green", lambda: self.build_flat_areas(doc, "green")),
        ]
        total = float(len(steps))
        for index, (name, action) in enumerate(steps):
            if progress:
                progress(index / total, "Building %s" % name)
            action()
        if progress:
            progress(1.0, "Site built")

    def summary(self):
        lines = []
        for key in ("terrain", "buildings", "roads", "rail", "water", "green"):
            made = self.counts.get(key, 0)
            if not made:
                continue
            missed = self.skipped.get(key, 0)
            label = "%d terrain points" % made if key == "terrain" \
                else "%d %s" % (made, key)
            lines.append("  " + label + (" (%d skipped)" % missed if missed else ""))
        return "\n".join(lines) + ("\n" + "\n".join(self.notes) if self.notes else "")


# ---------------------------------------------------------- document + link

def quiet_transaction(doc, name):
    """A Transaction that clears warnings instead of stopping to ask.

    Importing a few thousand context elements will trip Revit warnings
    (overlapping geometry, tiny elements). Without this, a long import parks
    behind a modal warning dialog and the user thinks it hung.
    """
    transaction = DB.Transaction(doc, name)
    try:
        options = transaction.GetFailureHandlingOptions()
        options.SetFailuresPreprocessor(_WarningSwallower())
        options.SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(options)
    except Exception:
        pass
    return transaction


if DB is not None:
    class _WarningSwallower(DB.IFailuresPreprocessor):
        def PreprocessFailures(self, accessor):
            try:
                accessor.DeleteAllWarnings()
            except Exception:
                pass
            return DB.FailureProcessingResult.Continue
else:
    _WarningSwallower = None


def default_site_path(host_doc, label=None):
    """Where to save the site model. -> path.

    Date-stamped, so a second run never silently overwrites the site someone
    already linked and adjusted.
    """
    import datetime
    import os

    folder = None
    try:
        path = host_doc.PathName
        if path:
            folder = os.path.dirname(path)
    except Exception:
        folder = None
    if not folder or not os.path.isdir(folder):
        folder = os.path.join(os.path.expanduser("~"), "Documents")

    folder = os.path.join(folder, "Groundit Sites")
    if not os.path.isdir(folder):
        os.makedirs(folder)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    clean = "".join(c for c in (label or "Site") if c.isalnum() or c in " -_").strip()
    return os.path.join(folder, "%s_%s.rvt" % (clean or "Site", stamp))


def sidecar_path(rvt_path):
    """Where a site model's request is remembered. -> path.

    Kept beside the .rvt rather than inside it. The request has to be
    readable without opening the site document (which is exactly what an
    update needs to decide), and a plain JSON file is also something a user
    can inspect, diff or hand-edit.
    """
    import os
    base = os.path.splitext(rvt_path)[0]
    return base + ".groundit.json"


def write_sidecar(rvt_path, request, extra=None):
    """Record what produced a site model, next to the model."""
    import datetime

    from . import sitespec
    payload = {
        "tool": "groundit",
        "spec_version": sitespec.SPEC_VERSION,
        "written": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "request": request,
    }
    payload.update(extra or {})
    sitespec.write_json(sidecar_path(rvt_path), payload)


def read_sidecar(rvt_path):
    """Read a site model's stored request. -> payload dict, or None."""
    import os

    from . import sitespec
    path = sidecar_path(rvt_path)
    if not os.path.isfile(path):
        return None
    try:
        payload = sitespec.read_json(path)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("tool") != "groundit":
        return None
    return payload


def link_target_path(link_type):
    """Absolute path a RevitLinkType points at. -> path, or None."""
    try:
        reference = link_type.GetExternalFileReference()
        model_path = reference.GetAbsolutePath()
        path = DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path)
        return path or None
    except Exception:
        return None


def find_groundit_links(host_doc):
    """Every linked site this tool produced. -> list of dicts.

    Identified by the sidecar, not by name: a link the user renamed is still
    ours, and a link that merely happens to be called "Site" is not.
    """
    import os

    found = []
    try:
        collector = DB.FilteredElementCollector(host_doc).OfClass(DB.RevitLinkType)
    except Exception:
        return found

    for link_type in collector:
        path = link_target_path(link_type)
        if not path:
            continue
        payload = read_sidecar(path)
        if payload is None:
            continue
        try:
            name = link_type.Name
        except Exception:
            name = os.path.basename(path)
        found.append({
            "type": link_type,
            "type_id": link_type.Id,
            "path": path,
            "name": name,
            "exists": os.path.isfile(path),
            "request": payload.get("request") or {},
            "written": payload.get("written"),
        })
    return found


def unload_link(link_type):
    """Release the file so it can be overwritten. -> True if it was loaded."""
    try:
        link_type.Unload(None)
        return True
    except Exception:
        return False


def reload_link(link_type):
    """Reload a link in place, preserving its placement. -> (ok, message)."""
    try:
        result = link_type.Reload()
        try:
            status = result.LoadResult
            if status is not None and str(status) not in ("LinkLoaded", "Loaded"):
                return False, "Revit reported the reload as %s." % status
        except Exception:
            pass                        # older API surface; the call succeeded
        return True, ""
    except Exception as exc:
        return False, str(exc)


def project_north_angle(doc):
    """Host rotation from true north, in radians. 0 if unavailable."""
    try:
        position = doc.ActiveProjectLocation.GetProjectPosition(DB.XYZ.Zero)
        return position.Angle
    except Exception:
        return 0.0


def site_location(doc):
    """Host project lat/lon in degrees. -> (lon, lat) or None."""
    try:
        location = doc.SiteLocation
        lat = math.degrees(location.Latitude)
        lon = math.degrees(location.Longitude)
        if abs(lat) < 1e-9 and abs(lon) < 1e-9:
            return None                 # unset sites read as 0,0 in the Atlantic
        return lon, lat
    except Exception:
        return None


def set_site_location(doc, lon, lat):
    """Write a picked location back to the host. Caller owns the transaction."""
    try:
        doc.SiteLocation.Latitude = math.radians(lat)
        doc.SiteLocation.Longitude = math.radians(lon)
        return True
    except Exception:
        return False


def create_site_document(app, host_doc):
    """A new, empty project document to build the site in. -> Document."""
    template = None
    try:
        candidate = app.DefaultProjectTemplate
        if candidate and str(candidate).strip():
            template = candidate
    except Exception:
        template = None

    if template:
        try:
            return app.NewProjectDocument(template)
        except Exception:
            pass

    units = DB.UnitSystem.Metric
    try:
        if host_doc.DisplayUnitSystem == DB.UnitSystem.Imperial:
            units = DB.UnitSystem.Imperial
    except Exception:
        pass
    return app.NewProjectDocument(units)


def save_document(doc, path):
    """Save a background document to path, overwriting."""
    options = DB.SaveAsOptions()
    options.OverwriteExistingFile = True
    try:
        options.Compact = True
    except Exception:
        pass
    doc.SaveAs(path, options)


def link_into_host(host_doc, path):
    """Link a saved site model into the host at origin. -> RevitLinkInstance.

    Caller owns the transaction. Origin-to-origin is correct here because the
    site was built around the picked area's centre, which is the point the
    user aimed at.
    """
    model_path = DB.ModelPathUtils.ConvertUserVisiblePathToModelPath(path)
    options = DB.RevitLinkOptions(False)
    result = DB.RevitLinkType.Create(host_doc, model_path, options)
    return DB.RevitLinkInstance.Create(host_doc, result.ElementId,
                                       DB.ImportPlacement.Origin)
