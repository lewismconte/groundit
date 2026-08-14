# Groundit

Import real-world site context into Revit. Draw a box on a map, choose your
layers, and get terrain, buildings and roads back as a **linked site model**.

No API keys. No accounts. No GIS software. No QGIS, no GDAL, no shapefiles.

![ribbon icon](Groundit.tab/Site.panel/Import%20Site.pushbutton/icon.png)

---

## What it does

1. You click **Import Site**. A map opens in your browser, already centred on
   the project if it has a location set.
2. You draw a box around the site, tick the layers you want, and click
   **Import into Revit**.
3. Groundit downloads elevation and OpenStreetMap data for that box, builds it
   into its own Revit model, and links that model into your project.

| Layer | What you get |
|---|---|
| Terrain | A toposolid built from real elevation data |
| Buildings | Massing solids at real heights, with courtyards as openings |
| Roads | Surfaces at real carriageway width, draped on the terrain (or centrelines, or both) |
| Water | Flat areas for rivers, lakes and docks |
| Parks and trees | Flat areas for green space |
| Railways | Same treatment as roads |

The site is built in a **separate model and linked**, not dumped into your
project. Thousands of context elements stay out of your file, the site can be
reloaded or swapped without touching the building, and you can unload it for
a clean drawing set. Files are date-stamped into a `Groundit Sites` folder
next to the host model, so a second run never overwrites a site you have
already linked and adjusted.

### Keeping a site current

**Update Site** re-downloads a site that is already linked and rebuilds it in
place. The link is reloaded rather than replaced, so it keeps its position,
workset, phase and any view overrides you have set up.

Each site model is written with a small `.groundit.json` sidecar recording the
request that produced it, which is how Update knows what to fetch. You can
either refresh the same area with fresh data, or reopen the map with every
control exactly as you left it to widen the area or add layers.

---

## Install

pyRevit recognises an extension by the `.extension` folder **suffix**:

```bash
git clone https://github.com/lewismconte/groundit Groundit.extension
```

Then in pyRevit Settings, add the **parent** folder as a Custom Extension
Directory, and reload.

Requires Revit 2024 or newer (for toposolids; it falls back to a topography
surface if none is available) and an internet connection.

---

## Where the data comes from

Both sources are free, keyless and global. This is what makes the tool a
single button rather than a setup guide.

**Elevation** is the [AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/)
open dataset, served as ordinary PNG tiles in the terrarium encoding:

```
elevation_metres = (R * 256 + G + B / 256) - 32768
```

Resolution is about 30 m globally and better where national datasets exist
(roughly 10 m across the US, from 3DEP). Groundit picks a zoom level to match
the ground resolution you ask for and samples it bilinearly, so the surface is
smooth rather than stepped.

**Buildings, roads and everything else** come from
[OpenStreetMap](https://www.openstreetmap.org) via the
[Overpass API](https://overpass-api.de), in a single query per run.

### Licensing, which actually matters here

OpenStreetMap data is licensed **ODbL**. Geometry derived from it and then
shared carries attribution and share-alike obligations. If a model containing
Groundit's buildings or roads leaves your office, it should credit
`(c) OpenStreetMap contributors`. Worth knowing before it goes in a planning
submission. The elevation data is public domain or equivalent, but crediting
the source is still good practice.

---

## How it is put together

```
   browser (map/index.html)
        |  bbox + layers, over localhost
        v
   pure core          geodesy . tiles . geom . overpass . sitespec . source
        |  a site spec: local metres, no Revit anywhere
        v
   build.py           toposolid, DirectShapes, then link into the host
```

The **pure core** imports nothing from Revit or .NET and runs under CPython 3
as well as IronPython 2.7. That is the whole reason the maths is trustworthy:
the entire download and geometry path can be tested outside Revit, against
real data, in seconds.

```bash
python tests/test_core.py            # 74 offline tests
python tests/test_core.py --online   # plus a real end-to-end fetch
python tools/check.py                # the pyRevit field-guide checks
```

The **map picker** runs in your own browser rather than inside Revit. The
alternative is embedding a map in WPF, which means either the IE11-era
`WebBrowser` control or shipping WebView2 DLLs and loading them from
IronPython. Neither is worth it when every machine already has a good browser.
Bridge, do not embed. The page is plain vanilla JavaScript with no CDN, so a
locked-down office network cannot break the UI.

The estimate shown in the picker (tile count, grid size, ground sample) is
computed by JavaScript that mirrors `geodesy.py` exactly, and the two are
checked against each other. The number on screen is the number that gets
built.

---

## Things it deliberately does

**Cleans OSM geometry before Revit sees it.** Footprints drawn by people carry
duplicate vertices, hairline segments below Revit's short-curve tolerance, and
long collinear runs. Fed straight to `CurveLoop`, Revit throws or silently
drops the element, which is how naive importers end up with half the buildings
missing. Cleanup typically removes about 40% of vertices without changing a
shape.

**Distrusts tagged values.** Rue Montesquieu in Paris, a lane barely wide
enough for one car, is tagged `width=99` in OSM. Widths are sanity-bounded
against their road class before being believed.

**Says how much of the skyline is real.** The run report separates measured
heights from storey-count estimates from outright defaults, because a model
built mostly from a 3.2 m assumption is worth knowing about before it goes in
front of a client.

**Uses Generic Model, not Mass.** Mass has its own visibility toggle that is
off in most views, which produces the classic "it said it worked but I cannot
see anything" bug report.

**Drapes onto the terrain.** Buildings sit at the lowest ground point under
their footprint, so a block on a slope rests on the ground at its downhill
corner rather than hovering at its uphill one.

---

## Limits and known rough edges

- **Road ribbons do not form junctions.** Each way is meshed independently, so
  ribbons overlap at intersections rather than merging into a single road
  surface. It reads correctly in plan and in 3D, but it is not a road network
  you could set out from. Centrelines remain the lighter option in a dense
  city, and `Both` gives you geometry to look at plus lines to trace.
- **Sharp bends are bevelled, not mitred.** Beyond about a 133 degree turn an
  exact mitre would spike off to infinity, so the corner is cut instead.
- **Building heights are only as good as OSM.** Coverage is excellent in some
  cities (San Francisco is about 90% measured) and absent in others.
- **Keep each side under 20 km**, and expect a dense city centre at a few
  kilometres to be thousands of buildings.
- **The local projection is a tangent plane**, not UTM. Sub-metre over a few
  kilometres, which is far below the accuracy of the source data, but it is
  not a survey-grade coordinate system and should not be treated as one.
- **True North rotation is the least-proven part.** Verified working end to end
  on Revit 2027.1, but a project whose True North equals Project North cannot
  reveal a sign error in the rotation. If you import into a project with a
  rotated True North and the site comes in mirrored about it, that is the sign
  to flip in `SiteBuilder.set_rotation`.

---

## Licence

MIT. See [LICENSE](LICENSE).

Built with the patterns in [PYREVIT-DEV-GUIDE.md](../PYREVIT-DEV-GUIDE.md),
alongside [Sendit](https://github.com/lewismconte/sendit) and
[Blendit](https://github.com/lewismconte/blendit).
