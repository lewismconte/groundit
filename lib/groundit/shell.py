"""Open folders, files and URLs with the OS default handler.

On .NET Core / .NET 8 (Revit 2025+) Process.Start(path) defaults
UseShellExecute to False, so it tries to *execute* the target and throws for
folders, URLs and documents. IronPython's os.startfile and webbrowser.open hit
the same wall. The result is an "Open folder" button that silently does
nothing on exactly the Revit versions people are actually running.

Shipped as its own copy inside this extension rather than imported from a
shared library, so Groundit works whether or not anything else is installed.
"""


def open_path(target):
    """Open a folder, file or URL. -> True on success."""
    try:
        from System.Diagnostics import Process, ProcessStartInfo
        psi = ProcessStartInfo(target)
        psi.UseShellExecute = True      # the .NET-8-safe shell open
        Process.Start(psi)
        return True
    except Exception:
        pass
    try:
        import os
        os.startfile(target)            # .NET Framework / fallback
        return True
    except Exception:
        pass
    try:
        import webbrowser
        return bool(webbrowser.open(target))
    except Exception:
        return False
