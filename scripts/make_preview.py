#!/usr/bin/env python3
"""Draw the README's pictures from the keys' own renderer.

Each action is painted in a representative live state and laid out as a deck,
and each is also written on its own in every state worth showing, so the
pictures on the repository page are the same art a deck shows rather than
mock-ups that drift from it. Run after changing how a key draws; the output is
committed.
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dev.gsr.sdPlugin"))

from gsrdeck import render                          # noqa: E402

OUT_SVG = os.path.join(ROOT, ".github", "preview.svg")
OUT_PNG = os.path.join(ROOT, ".github", "preview.png")
KEYS_DIR = os.path.join(ROOT, ".github", "keys")
COLUMNS = 3
GAP = 24
PAD = 32
BG = "#0e0f11"


def keys():
    render.set_theme("default")
    return [
        render.replay_key(True, 120, fill=0.62, age=75, storage="ram"),
        render.save_key("30s", True),
        render.record_key(True, elapsed=754, byte_count=1_480_000_000),
        render.stream_key(True, "Twitch", elapsed=3725),
        render.screenshot_key("Region"),
        render.status_key(["2m buffer · ram", "DP-1 · 60 fps",
                           "hevc · 40 Mbps",
                           f"2 audio · {render.size(640 << 20)} RAM"],
                          "REPLAY", "good"),
    ]


def states():
    """action -> [(state, svg)], in the order the README shows them."""
    render.set_theme("default")
    clip = os.path.expanduser("~/Videos/Replay_2026-09-28_21-14-03.mp4")
    shot = os.path.expanduser("~/Pictures/Screenshot_2026-09-28_21-15-40.png")
    off = "gsr-ui not running"
    return {
        "replay": [
            ("idle", render.replay_key(False, 120)),
            ("filling", render.replay_key(True, 120, fill=0.62, age=75)),
            ("full", render.replay_key(True, 120, fill=1.0, age=900)),
            ("unavailable", render.replay_key(False, 0, unavailable=True,
                                              note=off)),
        ],
        "save": [
            ("ready", render.save_key("30s", True)),
            ("signal", render.save_key("30s*", True)),
            ("saved", render.save_key("30s", True, flash=clip)),
            ("off", render.save_key("30s", False, note="replay is off")),
        ],
        "record": [
            ("idle", render.record_key(False)),
            ("recording", render.record_key(True, 754, 1_480_000_000)),
            ("paused", render.record_key(True, 754, 1_480_000_000,
                                         paused=True)),
            ("in-replay", render.record_key(True, 42, 96_000_000,
                                            in_replay=True)),
        ],
        "stream": [
            ("idle", render.stream_key(False, "Twitch")),
            ("live", render.stream_key(True, "Twitch", 3725)),
        ],
        "screenshot": [
            ("ready", render.screenshot_key("Region")),
            ("saved", render.screenshot_key("Region", flash=shot)),
        ],
        "status": [
            ("idle", render.status_key(["nothing is recording",
                                        "replay 2m · ram",
                                        "stream Twitch", "gsr-ui up"],
                                       "IDLE", "idle")),
            ("replay", keys()[5]),
        ],
    }


def compose(svgs):
    rows = -(-len(svgs) // COLUMNS)
    width = PAD * 2 + COLUMNS * render.SIZE + (COLUMNS - 1) * GAP
    height = PAD * 2 + rows * render.SIZE + (rows - 1) * GAP
    parts = []
    for index, svg in enumerate(svgs):
        x = PAD + (index % COLUMNS) * (render.SIZE + GAP)
        y = PAD + (index // COLUMNS) * (render.SIZE + GAP)
        # Nest each key as its own <svg>, positioned in the grid.
        parts.append(re.sub(r"^<svg ", f'<svg x="{x}" y="{y}" ', svg, count=1))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" rx="28" fill="{BG}"/>'
        + "".join(parts) + "</svg>"
    )


def rasterise(svg_path, png_path):
    """Render at 2x for high-density screens, keeping the aspect ratio."""
    for command in (
        ["rsvg-convert", "-z", "2", "-o", png_path, svg_path],
        ["magick", "-background", "none", "-density", "192", svg_path,
         png_path],
    ):
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


def main():
    svg = compose(keys())
    os.makedirs(os.path.dirname(OUT_SVG), exist_ok=True)
    with open(OUT_SVG, "w", encoding="utf-8") as handle:
        handle.write(svg)
    if not rasterise(OUT_SVG, OUT_PNG):
        print("no rasteriser found (install librsvg or imagemagick)")
        return 1
    os.remove(OUT_SVG)
    print(OUT_PNG)

    os.makedirs(KEYS_DIR, exist_ok=True)
    for action, variants in states().items():
        for state, key in variants:
            base = os.path.join(KEYS_DIR, f"{action}-{state}")
            with open(base + ".svg", "w", encoding="utf-8") as handle:
                handle.write(key)
            if not rasterise(base + ".svg", base + ".png"):
                return 1
            os.remove(base + ".svg")
            print(base + ".png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
