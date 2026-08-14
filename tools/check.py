# -*- coding: utf-8 -*-
"""Pre-commit checks for the Groundit extension.

The three checks from the pyRevit field guide, plus a Python 2 syntax sweep,
run without opening Revit:

  1. Every Revit-side .py is pure ASCII and parses. A single em-dash anywhere
     on the import chain crashes IronPython 2.7 with a SyntaxError, and the
     traceback points at the import, not the character.
  2. The panel bundle.yaml layout names match real button folders.
  3. The lib package imports headless, proving no module pulls in RevitAPI at
     import time.

Run with CPython 3:  python tools/check.py
"""

import ast
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Never on the IronPython import chain, so exempt from the ASCII rule.
# tools/ is CPython-only. tests/ declares its encoding and deliberately holds
# non-ASCII: one of the tests exists to prove an accented OSM name survives
# the parse, which it cannot do in pure ASCII.
EXEMPT_DIRS = ("tools", "tests")

def lib_modules():
    """Every module in lib/groundit, discovered rather than listed.

    This used to be a hardcoded list, which meant a newly added module was
    silently exempt from the import check - exactly the module most likely
    to have an eager RevitAPI import in it.
    """
    package = os.path.join(ROOT, "lib", "groundit")
    names = ["groundit"]
    for entry in sorted(os.listdir(package)):
        if entry.endswith(".py") and entry != "__init__.py":
            names.append("groundit." + entry[:-3])
    return names



failures = []


def revit_side_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            if rel.split("/")[0] in EXEMPT_DIRS:
                continue
            yield path, rel


def check_ascii_and_compile():
    print("[1] ASCII + compile sweep")
    bad_ascii, bad_syntax = [], []
    count = 0
    for path, rel in revit_side_files():
        count += 1
        raw = open(path, "rb").read()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            line = raw[:exc.start].count(b"\n") + 1
            snippet = raw[max(0, exc.start - 30):exc.start + 20]
            bad_ascii.append("%s:%d  %r" % (rel, line, snippet))
        try:
            ast.parse(raw)
        except SyntaxError as exc:
            bad_syntax.append("%s  %s" % (rel, exc))

    print("    %d Revit-side files" % count)
    for label, problems in (("non-ASCII", bad_ascii), ("syntax", bad_syntax)):
        if problems:
            failures.append(label)
            print("    FAIL (%s):" % label)
            for item in problems:
                print("      " + item)
    if not bad_ascii and not bad_syntax:
        print("    OK - pure ASCII, all parse")


def check_python2_syntax():
    """Flag Python 3 only constructs that IronPython 2.7 cannot parse."""
    print("[2] Python 2 compatibility sweep")
    problems = []
    for path, rel in revit_side_files():
        tree = ast.parse(open(path, "rb").read())
        for node in ast.walk(tree):
            kind = type(node).__name__
            if kind == "JoinedStr":
                problems.append("%s:%d f-string" % (rel, node.lineno))
            elif kind in ("AnnAssign",):
                problems.append("%s:%d variable annotation" % (rel, node.lineno))
            elif kind == "Nonlocal":
                problems.append("%s:%d nonlocal" % (rel, node.lineno))
            elif kind in ("AsyncFunctionDef", "Await", "AsyncFor", "AsyncWith"):
                problems.append("%s:%d async" % (rel, node.lineno))
            elif kind == "NamedExpr":
                problems.append("%s:%d walrus" % (rel, node.lineno))
            elif kind in ("FunctionDef",):
                args = node.args
                if getattr(args, "kwonlyargs", None) or getattr(args, "posonlyargs", None):
                    problems.append("%s:%d keyword-only/positional-only args"
                                    % (rel, node.lineno))
                if node.returns is not None or any(a.annotation for a in args.args):
                    problems.append("%s:%d annotation in signature" % (rel, node.lineno))
    if problems:
        failures.append("py2")
        print("    FAIL:")
        for item in problems:
            print("      " + item)
    else:
        print("    OK - no Python 3 only syntax")


def check_bundles():
    print("[3] Ribbon bundle.yaml layout")
    ok = True
    for dirpath, dirnames, filenames in os.walk(ROOT):
        if not dirpath.endswith(".panel") and not dirpath.endswith(".pulldown"):
            continue
        bundle = os.path.join(dirpath, "bundle.yaml")
        if not os.path.isfile(bundle):
            continue
        layout = []
        inside = False
        for line in open(bundle, "r"):
            stripped = line.strip()
            if stripped.startswith("layout:"):
                inside = True
                continue
            if inside:
                if stripped.startswith("- "):
                    layout.append(stripped[2:].strip().strip('"').strip("'"))
                elif stripped and not stripped.startswith("#"):
                    break
        folders = set()
        for entry in os.listdir(dirpath):
            for suffix in (".pushbutton", ".pulldown", ".stack", ".splitbutton"):
                if entry.endswith(suffix):
                    folders.add(entry[:-len(suffix)])
        missing = [x for x in layout if x not in ("---", ">>>") and x not in folders]
        unlisted = [x for x in folders if x not in layout]
        rel = os.path.relpath(dirpath, ROOT)
        print("    %s -> %s" % (rel, layout))
        if missing:
            ok = False
            print("      FAIL: layout names with no folder: %s" % missing)
        if unlisted:
            print("      note: folders not in layout (will fall to the end): %s" % unlisted)
    if ok:
        print("    OK - every layout name matches a folder")
    else:
        failures.append("bundle")


def check_headless_import():
    print("[4] Headless lib import (no eager RevitAPI)")
    sys.path.insert(0, os.path.join(ROOT, "lib"))
    modules = lib_modules()
    broken = []
    for name in modules:
        try:
            __import__(name)
        except Exception as exc:
            broken.append("%s  %s: %s" % (name, type(exc).__name__, exc))
    if broken:
        failures.append("import")
        print("    FAIL:")
        for item in broken:
            print("      " + item)
    else:
        print("    OK - all %d modules import with no Revit present" % len(modules))


def main():
    print("Groundit checks - %s\n" % ROOT)
    check_ascii_and_compile()
    check_python2_syntax()
    check_bundles()
    check_headless_import()
    print("\n%s" % ("FAILED: " + ", ".join(failures) if failures else "All checks passed."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
