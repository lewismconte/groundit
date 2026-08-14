"""Revit-side UI: waiting for the browser while the user picks a site.

Revit-side, so IronPython 2.7, pure ASCII, Python 2 syntax.

This deliberately uses **only pyRevit's own dialogs**. An earlier version put
up a hand-built WPF window driven by a DispatcherTimer, with the picker's HTTP
server on a background thread. That is a lot of custom native surface on
Revit's UI thread - a XAML parse, a modal ShowDialog, a dispatcher timer, and
IronPython threads doing socket work alongside it - and Revit 2027 (.NET 10)
crashed during the wait.

What replaced it has none of those moving parts:

  - no background thread: the server is pumped one request at a time from
    this loop, on Revit's own thread;
  - no custom window: pyRevit's ProgressBar is used, which is already proven
    in this environment and pumps Revit's message loop so the UI stays alive;
  - no XAML, no dispatcher timer.

The cost is a slightly plainer dialog. Worth it.

Rocket mode: nothing is cached at module level and no
System.Windows.Application is ever constructed, so repeat runs are clean.
"""

POLL_SECONDS = 0.2

# Long enough that someone can wander off mid-pick, short enough that a
# forgotten run does not hold Revit forever.
DEFAULT_TIMEOUT_S = 1800


def revit_is_dark():
    """-> True if Revit is on its dark theme. False if it cannot be determined."""
    try:
        from Autodesk.Revit.UI import UIThemeManager, UITheme
        return UIThemeManager.CurrentTheme == UITheme.Dark
    except Exception:
        return False


def wait(picker, logger=None, timeout_s=DEFAULT_TIMEOUT_S):
    """Serve the picker until the browser answers. -> outcome string.

    "submitted", "cancelled", or "timeout".

    Runs the HTTP server inline: every pump() call serves at most one request
    and then returns, so the browser is answered without a thread and Revit
    keeps pumping messages between calls.
    """
    from pyrevit import forms

    title = "Groundit: draw your site in the browser, then click Import"
    waited = 0.0
    spin = 0

    with forms.ProgressBar(title=title, cancellable=True) as bar:
        while waited < timeout_s:
            if bar.cancelled:
                return "cancelled"

            try:
                status = picker.pump(POLL_SECONDS)
            except Exception:
                # A failure serving one request must not end the pick; the
                # browser will simply retry.
                if logger is not None:
                    import traceback
                    logger.debug("Groundit: request failed\n%s",
                                 traceback.format_exc())
                status = picker.poll()

            if status != "waiting":
                return status

            waited += POLL_SECONDS
            # The wait is genuinely indeterminate, so the bar just has to look
            # alive rather than claim to know how far along it is.
            spin = (spin + 1) % 100
            bar.update_progress(spin, 100)

    return "timeout"
