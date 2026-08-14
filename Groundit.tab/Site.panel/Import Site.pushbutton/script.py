"""Import real-world site context: terrain, buildings and roads.

Opens a map in the browser, downloads elevation and OpenStreetMap data for
the area you draw, builds it into its own Revit model, and links that model
into the current project.

Revit-side: IronPython 2.7, pure ASCII, Python 2 syntax.
"""

__title__ = "Import\nSite"
__author__ = "lewismconte"
__doc__ = ("Pick an area on a map and import terrain, buildings and roads "
           "as a linked site model. Needs an internet connection.")

import os
import traceback

from pyrevit import forms, revit, script

from Autodesk.Revit import DB

from groundit import build, net, picker, sitespec, source, ui
from groundit.shell import open_path

logger = script.get_logger()

doc = revit.doc                          # per run, so rocket mode stays honest


class Cancelled(Exception):
    """The user backed out; not an error."""


def preflight():
    """Fail fast and clearly, before anything slow happens."""
    if doc is None:
        forms.alert("Open a project first.", title="Groundit")
        return False

    if doc.IsFamilyDocument:
        forms.alert("Groundit builds site context for a project.\n\n"
                    "Open a project rather than a family and try again.",
                    title="Groundit")
        return False

    if not os.path.isfile(picker.map_page_path()):
        forms.alert("The map page is missing from the extension:\n\n%s\n\n"
                    "Reinstall Groundit." % picker.map_page_path(),
                    title="Groundit")
        return False

    ok, detail = net.check_reachable()
    if not ok:
        logger.error("Groundit pre-flight failed:\n%s", detail)
        answer = forms.alert(
            "Groundit could not download a test tile.\n\n"
            "This is either no internet access from Revit, or a problem "
            "inside Groundit itself. The details say which.",
            title="Groundit", options=["Show the details", "Cancel"])
        if answer == "Show the details":
            output = script.get_output()
            output.print_md("### Groundit could not reach the elevation service")
            print("Probe URL: %s\n" % net.PROBE_URL)
            print(detail)
            output.print_md(
                "\nIf the errors above mention a **type, import or attribute "
                "failure**, that is a Groundit bug, not your network. If they "
                "mention a **timeout, proxy or certificate**, it is the "
                "connection.")
        return False

    return True


def ask_for_site():
    """Run the browser picker. -> request dict, or None if the user backed out."""
    config = {"name": doc.Title}
    location = build.site_location(doc)
    if location:
        # The project already knows where it is, so start the map there.
        config["lon"], config["lat"] = location
        config["zoom"] = 16

    try:
        session = picker.Picker(config)
    except Exception as exc:
        logger.error(traceback.format_exc())
        forms.alert("Groundit could not open its local map server.\n\n%s\n\n"
                    "Security software blocking local connections is the "
                    "usual cause." % exc, title="Groundit")
        return None

    try:
        # The socket is already bound, so a browser that arrives before the
        # first pump() waits in the accept backlog rather than being refused.
        if not open_path(session.url):
            forms.alert("Could not open your browser.\n\nOpen this address "
                        "manually:\n\n%s" % session.url, title="Groundit")

        outcome = ui.wait(session, logger)
        if outcome == "timeout":
            forms.alert("Timed out waiting for the map.", title="Groundit")
            return None
        if outcome != "submitted":
            return None
        return sitespec.merge_request(session.result)
    finally:
        session.stop()


def download(request):
    """Fetch terrain and OSM with a progress bar. -> site dict."""
    with forms.ProgressBar(title="Groundit: downloading", cancellable=True) as bar:
        def report(fraction, message):
            if bar.cancelled:
                raise Cancelled()
            try:
                bar.title = "Groundit: %s" % message
            except Exception:
                pass
            bar.update_progress(int(fraction * 100), 100)

        return source.fetch_site(request, report)


def build_and_link(site, request):
    """Build the site model, save it, and link it in. -> (path, builder)."""
    app = doc.Application
    builder = build.SiteBuilder(site)

    if request.get("align_true_north"):
        # Site coordinates are true-north-up; the host may not be.
        builder.set_rotation(-build.project_north_angle(doc))

    site_doc = build.create_site_document(app, doc)
    path = build.default_site_path(doc, request.get("name") or "Site")

    with forms.ProgressBar(title="Groundit: building", cancellable=False) as bar:
        def report(fraction, message):
            try:
                bar.title = "Groundit: %s" % message
            except Exception:
                pass
            bar.update_progress(int(fraction * 100), 100)

        transaction = build.quiet_transaction(site_doc, "Groundit site")
        transaction.Start()
        try:
            builder.build_into(site_doc, report)
            transaction.Commit()
        except Exception:
            transaction.RollBack()
            site_doc.Close(False)
            raise

        build.save_document(site_doc, path)
        site_doc.Close(False)

    with revit.Transaction("Groundit: link site", doc=doc):
        build.link_into_host(doc, path)
        if request.get("write_site_location"):
            west, south, east, north = site["bbox"]
            build.set_site_location(doc, (west + east) / 2.0, (south + north) / 2.0)

    return path, builder


def report(site, path, builder):
    summary = builder.summary()
    stats = site.get("stats") or {}
    dropped = stats.get("buildings_dropped")

    lines = ["Site linked into the project.", "", summary]
    if dropped:
        lines.append("")
        lines.append("%d of the smallest buildings were left out to keep the "
                     "model workable." % dropped)
    lines.append("")
    lines.append("Saved to:")
    lines.append(os.path.basename(path))

    if forms.alert("\n".join(lines), title="Groundit",
                   options=["Done", "Open the folder"]) == "Open the folder":
        open_path(os.path.dirname(path))


def main():
    if not preflight():
        return

    request = ask_for_site()
    if request is None:
        return

    valid, message = sitespec.validate_request(request)
    if not valid:
        forms.alert(message, title="Groundit")
        return

    try:
        site = download(request)
    except Cancelled:
        return
    except net.NetworkError as exc:
        forms.alert("Download failed.\n\n%s" % exc, title="Groundit")
        return

    if not site.get("terrain") and not any(site.get("features", {}).values()):
        forms.alert("Nothing was found in that area.\n\n"
                    "Try a bigger box, or turn on more layers.", title="Groundit")
        return

    try:
        path, builder = build_and_link(site, request)
    except Exception as exc:
        logger.error(traceback.format_exc())
        forms.alert("The site downloaded, but building it failed.\n\n%s" % exc,
                    title="Groundit")
        return

    report(site, path, builder)


main()
