# -*- coding: utf-8 -*-
"""Generate ribbon icons for the Groundit extension.

Same conventions as the Sendit generator: render at 8x supersampling and
downscale to 96x96 for crisp edges. pyRevit picks icon.png for the light
theme and icon.dark.png when Revit 2024+ runs its dark UI theme.

CPython 3 with Pillow, not IronPython:  python tools/make_icons.py
"""

import os

from PIL import Image, ImageDraw

S = 8            # supersample factor
SIZE = 96        # final icon size
PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "Groundit.tab", "Site.panel")

PALETTES = {
    # key: (outline, ground, contour, accent, building)
    "light": ("#44505E", "#E9EEF3", "#8B9AA8", "#1F77D0", "#5B6B7A"),
    "dark":  ("#DCE3EA", "#3C444B", "#9FAEBC", "#4DA3FF", "#C3CDD7"),
}


def canvas():
    img = Image.new("RGBA", (SIZE * S, SIZE * S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def sc(*coords):
    """Scale a flat coordinate list by the supersample factor."""
    return [c * S for c in coords]


def save(img, folder, name):
    out = img.resize((SIZE, SIZE), Image.LANCZOS)
    path = os.path.join(PANEL, folder, name)
    folder_path = os.path.dirname(path)
    if not os.path.isdir(folder_path):
        os.makedirs(folder_path)
    out.save(path)
    print("wrote", os.path.normpath(path))


def _wave(y, amplitude, phase, x0=16, x1=80, steps=32):
    """A smooth contour line across the plate. -> flat coordinate list."""
    import math
    points = []
    for i in range(steps + 1):
        t = i / float(steps)
        x = x0 + (x1 - x0) * t
        points.extend([x, y + amplitude * math.sin(t * math.pi * 1.6 + phase)])
    return points


def import_site_icon(theme):
    """Contour lines inside a selection frame: pick an area of ground.

    Kept deliberately spare. This sits at 32px on the ribbon, where a literal
    little building turns into three grey pixels and reads as dirt.
    """
    outline, ground, contour, accent, _building = PALETTES[theme]
    img, d = canvas()

    # Ground plate.
    d.rounded_rectangle(sc(14, 22, 82, 80), radius=7 * S,
                        fill=ground, outline=outline, width=4 * S)

    # Contours, tighter towards the top so the plate reads as a slope.
    for y, amplitude, phase in ((68, 3.5, 0.0), (56, 4.0, 0.5),
                                (44, 4.0, 1.0), (33, 3.0, 1.5)):
        d.line(sc(*_wave(y, amplitude, phase)), fill=contour,
               width=3 * S, joint="curve")

    # Selection frame: corner brackets outside the plate, so the icon reads as
    # "choose this area" rather than "here is a map".
    w = 5 * S
    arm = 15
    for (cx, cy, dx, dy) in ((6, 14, 1, 1), (90, 14, -1, 1),
                             (6, 88, 1, -1), (90, 88, -1, -1)):
        d.line(sc(cx, cy, cx + arm * dx, cy), fill=accent, width=w)
        d.line(sc(cx, cy, cx, cy + arm * dy), fill=accent, width=w)

    save(img, "Import Site.pushbutton",
         "icon.png" if theme == "light" else "icon.dark.png")


def main():
    for theme in ("light", "dark"):
        import_site_icon(theme)


if __name__ == "__main__":
    main()
