# -*- coding: ascii -*-
"""Refresh a linked Groundit site in place.

Re-downloads the terrain and OpenStreetMap data for a site that is already
linked, rebuilds the .rvt at the same path, and reloads the link. Reloading
rather than re-linking is the point: the link keeps its position, workset,
phase and any view-specific overrides.

Two modes. "Same area" reuses the stored request exactly, which is what you
want when OSM has been edited or a building has gone up. "Change the area"
reopens the map picker pre-filled with the stored settings, so a site can be
widened or have layers added without starting over.

Revit-side: IronPython 2.7, pure ASCII, Python 2 syntax.
"""

import os
import traceback

from pyrevit import forms, revit, script

from groundit import build, net, picker, pipeline, sitespec, source, ui
from groundit.shell import open_path

__title__ = "Update\nSite"
__author__ = "lewismconte"
__doc__ = ("Re-download a linked Groundit site and reload it in place, "
           "keeping its position and any view overrides.")

doc = revit.doc
logger = script.get_logger()


def choose_link(links):
    """Pick which site to update. -> link dict, or None."""
    if len(links) == 1:
        return links[0]

    labels = {}
    for link in links:
        stamp = link.get("written") or "date unknown"
        label = "%s  (built %s)" % (link["name"], stamp)
        labels[label] = link
    chosen = forms.SelectFromList.show(
        sorted(labels.keys()), title="Which site should Groundit update?",
        button_name="Update this one", multiselect=False)
    return labels.get(chosen) if chosen else None


def choose_mode(link):
    """Same settings, or reopen the picker? -> "same" | "change" | None."""
    answer = forms.alert(
        "Update '%s'?\n\nBuilt %s." % (link["name"],
                                       link.get("written") or "at an unknown date"),
        title="Groundit", options=["Same area, fresh data",
                                   "Change the area or layers",
                                   "Cancel"])
    if answer == "Same area, fresh data":
        return "same"
    if answer == "Change the area or layers":
        return "change"
    return None


def repick(stored):
    """Reopen the map picker pre-filled from a stored request. -> request/None."""
    location = build.site_location(doc)
    bbox = stored.get("bbox")
    config = {
        "name": stored.get("name") or "Site",
        "request": stored,
        "bbox": bbox,
    }
    if location:
        config["lon"], config["lat"] = location

    session = picker.Picker(config)
    try:
        if not open_path(session.url):
            forms.alert("Could not open your browser.\n\nOpen this address "
                        "manually:\n\n%s" % session.url, title="Groundit")
        outcome = ui.wait(session, logger)
        if outcome != "submitted":
            return None
        return sitespec.merge_request(session.result)
    finally:
        session.stop()


def main():
    if not doc:
        forms.alert("Open a project first.", title="Groundit")
        return

    links = build.find_groundit_links(doc)
    if not links:
        forms.alert("No Groundit sites are linked into this project.\n\n"
                    "Use Import Site to bring one in first.", title="Groundit")
        return

    link = choose_link(links)
    if link is None:
        return

    if not link["exists"]:
        forms.alert("The site model this link points at is missing:\n\n%s\n\n"
                    "Groundit can rebuild it at that path."
                    % link["path"], title="Groundit")

    mode = choose_mode(link)
    if mode is None:
        return

    stored = sitespec.merge_request(link.get("request") or {})
    if mode == "change":
        request = repick(stored)
        if request is None:
            return
    else:
        request = stored

    valid, message = sitespec.validate_request(request)
    if not valid:
        forms.alert("That site cannot be rebuilt:\n\n%s\n\n"
                    "Its saved settings may predate this version of Groundit. "
                    "Use 'Change the area or layers' to set it up again."
                    % message, title="Groundit")
        return

    ok, detail = net.check_reachable()
    if not ok:
        logger.error("Groundit pre-flight failed:\n%s", detail)
        forms.alert("Groundit could not download a test tile, so there is no "
                    "point starting.\n\nThe link has been left alone.",
                    title="Groundit")
        return

    try:
        with forms.ProgressBar(title="Groundit: downloading",
                               cancellable=False) as bar:
            def report(fraction, message):
                try:
                    bar.title = "Groundit: %s" % message
                except Exception:
                    pass
                bar.update_progress(int(fraction * 100), 100)

            site = source.fetch_site(request, report)
    except Exception as exc:
        logger.error(traceback.format_exc())
        forms.alert("Groundit could not download the site data.\n\n%s\n\n"
                    "The link has been left alone." % exc, title="Groundit")
        return

    try:
        with forms.ProgressBar(title="Groundit: rebuilding",
                               cancellable=False) as bar:
            def report(fraction, message):
                try:
                    bar.title = "Groundit: %s" % message
                except Exception:
                    pass
                bar.update_progress(int(fraction * 100), 100)

            with revit.Transaction("Groundit: update site", doc=doc):
                ok, message, builder = pipeline.update_existing(
                    doc, link, site, request, report)
                pipeline.apply_site_location(doc, site, request)
    except Exception as exc:
        logger.error(traceback.format_exc())
        forms.alert("Groundit could not rebuild the site.\n\n%s\n\n"
                    "The existing link has been left as it was."
                    % exc, title="Groundit")
        return

    lines = ["Updated '%s'." % link["name"], "", builder.summary()]
    if not ok:
        lines.append("")
        lines.append("The model was rebuilt, but Revit did not reload the "
                     "link cleanly: %s" % message)
        lines.append("Reload it from Manage > Manage Links.")

    forms.alert("\n".join(lines), title="Groundit")
    if os.path.isfile(link["path"]):
        logger.debug("Groundit updated %s", link["path"])


if __name__ == "__main__":
    main()
