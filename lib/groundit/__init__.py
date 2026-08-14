"""Groundit - real-world site context for Revit.

Package layout, and which world each module runs in:

    geodesy.py   pure  - lat/lon, slippy tiles, local ENU frame
    tiles.py     pure  - PNG decode, terrarium elevation, mosaic sampling
    geom.py      pure  - polygon cleanup and simplification
    overpass.py  pure  - OSM query building and parsing
    sitespec.py  pure  - the neutral site spec + UTF-8 safe JSON io
    net.py       mixed - HTTP via .NET in Revit, urllib headless
    picker.py    mixed - local map picker server + browser handoff
    build.py     Revit - turns a site spec into a linked site model
    shell.py     mixed - .NET-8-safe shell open
    theme.py     Revit - dark/light Revit theming for WPF dialogs

Everything marked "pure" imports nothing from Revit or .NET and is testable
headless under CPython 3 as well as IronPython 2.7. Keep it that way: the pure
core is where all the maths lives, and being able to run it outside Revit is
the whole reason the maths is trustworthy.

Revit-side files are pure ASCII and Python 2 syntax throughout.
"""

__version__ = "0.1.0"

# User-Agent sent on every outbound request. Overpass and Nominatim both
# throttle or reject anonymous clients, and their usage policies ask for a
# contactable identifier, so this is not optional politeness.
USER_AGENT = "Groundit/%s (pyRevit site importer; +https://github.com/lewismconte/groundit)" % __version__
