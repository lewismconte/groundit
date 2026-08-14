"""Shared Revit-side orchestration for importing and updating a site.

Both buttons do the same core work - build a site document, save it, point a
link at it - and differ only at the ends: Import creates a new link, Update
overwrites the file an existing link already points at. Keeping that middle
in one place is what stops the two drifting apart.

Progress is a plain callback rather than a pyRevit ProgressBar, so this
module owns no UI and the buttons keep control of how they report.

Revit-side: IronPython 2.7, pure ASCII, Python 2 syntax.
"""

from . import build


def build_and_save(host_doc, site, request, path, progress=None):
    """Build the site into a fresh document and save it at path. -> builder.

    Always closes the background document, including on failure: a leaked
    background document holds a file lock and blocks the next update.
    """
    app = host_doc.Application
    builder = build.SiteBuilder(site, {"road_style": request.get("road_style")})

    if request.get("align_true_north"):
        # Site coordinates are true-north-up; the host may not be.
        builder.set_rotation(-build.project_north_angle(host_doc))

    site_doc = build.create_site_document(app, host_doc)
    try:
        transaction = build.quiet_transaction(site_doc, "Groundit site")
        transaction.Start()
        try:
            builder.build_into(site_doc, progress)
            transaction.Commit()
        except Exception:
            transaction.RollBack()
            raise
        build.save_document(site_doc, path)
    finally:
        try:
            site_doc.Close(False)
        except Exception:
            pass

    # Written only after a successful save, so a sidecar never describes a
    # file that was not actually produced.
    build.write_sidecar(path, request, {"bbox": site.get("bbox")})
    return builder


def apply_site_location(host_doc, site, request):
    """Push the picked centre back to the host's project location, if asked."""
    if not request.get("write_site_location"):
        return
    west, south, east, north = site["bbox"]
    build.set_site_location(host_doc, (west + east) / 2.0, (south + north) / 2.0)


def update_existing(host_doc, link, site, request, progress=None):
    """Rebuild the file an existing link points at. -> (ok, message, builder).

    Order matters: Revit holds the linked file open, so the link has to be
    unloaded before the .rvt can be overwritten, and reloaded afterwards.
    Reloading in place is the whole point - it preserves the link's position,
    workset, phase and any view overrides the user has set up.

    If the rebuild fails after unloading, the link is reloaded from the old
    file so the user is never left with a silently unloaded site.
    """
    link_type = link["type"]
    path = link["path"]

    was_loaded = build.unload_link(link_type)
    try:
        builder = build_and_save(host_doc, site, request, path, progress)
    except Exception:
        if was_loaded:
            build.reload_link(link_type)    # put the old site back
        raise

    ok, message = build.reload_link(link_type)
    return ok, message, builder
