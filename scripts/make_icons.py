#!/usr/bin/env python3
"""Draw the plugin's icons from the same glyphs the keys are drawn with.

An action's icon in OpenDeck's action list and the image that action paints on
a key are the same picture at two sizes; drawing them from one source is what
stops the list showing a camera for an action that draws a record dot.

PNGs are written next to the SVGs because Elgato's manifest format resolves an
image reference without an extension and OpenDeck's action list is not an SVG
renderer everywhere. Run this after changing a glyph, not on every build --
the output is committed.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "dev.gsr.sdPlugin")
ICONS = os.path.join(BUNDLE, "icons")
sys.path.insert(0, BUNDLE)

from gsrdeck import render                          # noqa: E402

SIZE = 72
# Icons are drawn on the dark palette whatever theme the keys use: they sit in
# OpenDeck's own action list, which is not themed by this plugin.
PALETTE = render.THEMES["default"]

# name -> (glyph, colour)
ICONS_WANTED = {
    "plugin": ("record", PALETTE["hot"]),
    "replay": ("replay", PALETTE["good"]),
    "save": ("save", PALETTE["live"]),
    "record": ("record", PALETTE["hot"]),
    "stream": ("stream", PALETTE["hot"]),
    "screenshot": ("camera", PALETTE["live"]),
    "status": ("status", PALETTE["live"]),
}


def draw(glyph, colour):
    render.set_theme("default")
    # The glyph's 24-unit grid is scaled to fill the icon with a margin, and
    # centred by translating by that margin.
    scale = (SIZE * 0.62) / 24.0
    offset = (SIZE - 24 * scale) / 2.0
    body = render._glyph(glyph, colour, offset, offset, scale)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}" viewBox="0 0 {SIZE} {SIZE}">'
        f'<rect width="{SIZE}" height="{SIZE}" rx="14" '
        f'fill="{PALETTE["bg"]}"/>{body}</svg>'
    )


def rasterise(svg_path, png_path, pixels):
    for command in (
        ["rsvg-convert", "-w", str(pixels), "-h", str(pixels),
         "-o", png_path, svg_path],
        ["magick", "-background", "none", "-density", "384", svg_path,
         "-resize", f"{pixels}x{pixels}", png_path],
    ):
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


def main():
    os.makedirs(ICONS, exist_ok=True)
    missing = False
    for name, (glyph, colour) in ICONS_WANTED.items():
        svg_path = os.path.join(ICONS, f"{name}.svg")
        with open(svg_path, "w", encoding="utf-8") as handle:
            handle.write(draw(glyph, colour))
        for suffix, pixels in (("", SIZE), ("@2x", SIZE * 2)):
            png_path = os.path.join(ICONS, f"{name}{suffix}.png")
            if not rasterise(svg_path, png_path, pixels):
                missing = True
        print(f"wrote {name}")
    if missing:
        print("no rasteriser found (install librsvg or imagemagick); "
              "the SVGs are written but the PNGs are not")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
