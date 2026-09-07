"""Draw the keys.

OpenDeck renders SVG -- it bundles resvg -- so a key is generated rather than
picked from a set of static PNGs. That matters here because almost everything
worth showing is continuous: how full a replay buffer is, how long a recording
has run, how big its file has become. Baking that into images would need one
file per second.

Everything is drawn on a 144x144 grid, the Stream Deck key size, and scaled by
the host for other panels.
"""

import base64
import os

SIZE = 144

# One accent per meaning, used for the glyph, the bar and the border alike, so
# a key reads at a glance from across a desk rather than needing to be read.
# A theme is the whole palette, not a single hue: recolouring only the accent
# leaves a light theme's text unreadable on a dark ground.
THEMES = {
    "default": {
        "name": "Default",
        "live": "#3ba7f0", "hot": "#e5484d", "good": "#3ecf8e",
        "idle": "#6b7280", "bg": "#17191c", "text": "#eceff2",
        "dim": "#8b929b", "track": "#2b2f35",
    },
    "amber": {
        "name": "Amber",
        "live": "#f0a83b", "hot": "#e5484d", "good": "#ffd166",
        "idle": "#6b6154", "bg": "#1a1713", "text": "#f6eee2",
        "dim": "#a2957f", "track": "#332b21",
    },
    "violet": {
        "name": "Violet",
        "live": "#b07cf0", "hot": "#f2568f", "good": "#6ee7d3",
        "idle": "#6b6480", "bg": "#17141f", "text": "#efeaf7",
        "dim": "#9a92ad", "track": "#2c2740",
    },
    "mono": {
        "name": "Monochrome",
        "live": "#e8ecf1", "hot": "#c9ced6", "good": "#ffffff",
        "idle": "#4d545c", "bg": "#141618", "text": "#f2f4f7",
        "dim": "#828a94", "track": "#2a2e33",
    },
    "contrast": {
        # Deliberately not subtle. Chosen for a deck under stage lighting,
        # where the default's mid-tones disappear.
        "name": "High contrast",
        "live": "#00d4ff", "hot": "#ff2d55", "good": "#00ff88",
        "idle": "#8a8a8a", "bg": "#000000", "text": "#ffffff",
        "dim": "#c8c8c8", "track": "#3a3a3a",
    },
    "light": {
        "name": "Light",
        "live": "#0b74d1", "hot": "#c4262e", "good": "#0f8f5e",
        "idle": "#8a9199", "bg": "#f4f6f8", "text": "#14181c",
        "dim": "#5b636b", "track": "#d3d9df",
    },
}

DEFAULT_THEME = "default"
THEME = dict(THEMES[DEFAULT_THEME])


def set_theme(name):
    """Swap the palette every key is drawn from. Returns the name in use.

    Module state rather than a parameter threaded through every function:
    drawing happens only on the plugin's event-loop thread, so there is no
    second caller to race with, and an unknown name falls back rather than
    leaving keys half-drawn in a palette that does not exist.
    """
    global THEME
    THEME = dict(THEMES.get(name) or THEMES[DEFAULT_THEME])
    return THEME["name"]


def theme_choices():
    return [{"id": key, "label": value["name"]}
            for key, value in THEMES.items()]


FONT = "Liberation Sans, DejaVu Sans, Helvetica, Arial, sans-serif"

# 24x24 glyph paths, translated and scaled into place by _glyph().
_DOT = "M12 6.6a5.4 5.4 0 1 1 0 10.8 5.4 5.4 0 0 1 0-10.8z"
_REPLAY_ARC = "M20 12a8 8 0 1 1-2.6-5.9"
_REPLAY_TIP = "M19.9 3.4v3.9h-3.9"
_SAVE_STEM = "M12 3.6v10.4"
_SAVE_TIP = "M7.4 9.6 12 14.2l4.6-4.6"
_TRAY = "M4.4 16.6v2.6a1.4 1.4 0 0 0 1.4 1.4h12.4a1.4 1.4 0 0 0 1.4-1.4v-2.6"
_WAVE_NEAR = "M8.8 15.2a4.6 4.6 0 0 1 0-6.4M15.2 8.8a4.6 4.6 0 0 1 0 6.4"
_WAVE_FAR = "M5.4 18.6a9.4 9.4 0 0 1 0-13.2M18.6 5.4a9.4 9.4 0 0 1 0 13.2"
_CAMERA_BODY = ("M3.4 8.4h3.8l1.6-2.4h6.4l1.6 2.4h3.8a1.4 1.4 0 0 1 1.4 1.4"
                "v8.4a1.4 1.4 0 0 1-1.4 1.4H3.4A1.4 1.4 0 0 1 2 18.2V9.8a1.4"
                " 1.4 0 0 1 1.4-1.4z")
_CAMERA_LENS = "M12 10.6a3.6 3.6 0 1 1 0 7.2 3.6 3.6 0 0 1 0-7.2z"
_GAUGE_ARC = "M4 17a8.6 8.6 0 1 1 16 0"
_GAUGE_NEEDLE = "M12 17 16.2 10.6"
_PAUSE_L = (8.2, 5.6, 2.9, 12.8, 1.2)
_PAUSE_R = (12.9, 5.6, 2.9, 12.8, 1.2)

# Each glyph is drawn on a 24x24 grid: filled paths, stroked paths, and
# rounded rectangles, which is what a pause bar wants and what a path would
# only express clumsily.
GLYPHS = {
    "record": {"fill": [_DOT], "stroke": [], "rects": []},
    "replay": {"fill": [], "stroke": [_REPLAY_ARC, _REPLAY_TIP], "rects": []},
    "save": {"fill": [], "stroke": [_SAVE_STEM, _SAVE_TIP, _TRAY],
             "rects": []},
    "stream": {"fill": [_DOT], "stroke": [_WAVE_NEAR, _WAVE_FAR],
               "rects": []},
    "camera": {"fill": [], "stroke": [_CAMERA_BODY, _CAMERA_LENS],
               "rects": []},
    "status": {"fill": [], "stroke": [_GAUGE_ARC, _GAUGE_NEEDLE],
               "rects": []},
    "pause": {"fill": [], "stroke": [], "rects": [_PAUSE_L, _PAUSE_R]},
}


def _escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _fit(text, limit):
    """Truncate to a width the key can actually show, with an ellipsis."""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _wrap(text, per_line, max_lines=2):
    """Break a name across lines rather than cutting it short."""
    words, lines, current = str(text).split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= per_line or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if not lines:
        return [""]
    consumed = len(" ".join(lines).split())
    if consumed < len(words):
        lines[-1] = _fit(lines[-1] + " " + words[consumed], per_line)
    return [_fit(line, per_line) for line in lines]


def _text_block(lines, centre_y, size, colour, weight="600", x=SIZE / 2,
                anchor="middle"):
    """One or two centred lines, kept vertically centred as a block."""
    step = size + 4
    top = centre_y - (len(lines) - 1) * step / 2
    return "".join(
        f'<text x="{x}" y="{top + i * step:.1f}" fill="{colour}" '
        f'font-size="{size}" font-family="{FONT}" font-weight="{weight}" '
        f'text-anchor="{anchor}">{_escape(line)}</text>'
        for i, line in enumerate(lines)
    )


def _glyph(kind, colour, x, y, scale, slashed=False):
    """One icon, drawn at (x, y) with its 24-unit grid scaled by `scale`."""
    shape = GLYPHS.get(kind, GLYPHS["status"])
    stroke_width = 2.0
    parts = [f'<g transform="translate({x},{y}) scale({scale})" '
             f'fill="none" stroke="{colour}" stroke-width="{stroke_width}" '
             f'stroke-linecap="round" stroke-linejoin="round">']
    for path in shape["fill"]:
        parts.append(f'<path d="{path}" fill="{colour}" stroke="none"/>')
    for rect in shape["rects"]:
        rx, ry, rw, rh, radius = rect
        parts.append(f'<rect x="{rx}" y="{ry}" width="{rw}" height="{rh}" '
                     f'rx="{radius}" fill="{colour}" stroke="none"/>')
    for path in shape["stroke"]:
        parts.append(f'<path d="{path}"/>')
    if slashed:
        # A slash through the glyph, doubled in the background colour
        # underneath so it reads as a cut rather than as one more stroke of
        # the icon itself.
        parts.append(f'<path d="M3.6 3.6 20.4 20.4" stroke="{THEME["bg"]}" '
                     f'stroke-width="{stroke_width + 2.2}"/>')
        parts.append(f'<path d="M3.6 3.6 20.4 20.4" stroke="{colour}"/>')
    parts.append("</g>")
    return "".join(parts)


def _bar(value, colour, y, width=None, height=8):
    """A progress bar across the key, with the unfilled part left visible."""
    left = 16
    width = SIZE - 32 if width is None else width
    filled = max(0.0, min(1.0, value)) * width
    track = (f'<rect x="{left}" y="{y}" width="{width}" height="{height}" '
             f'rx="{height / 2}" fill="{THEME["track"]}"/>')
    if filled <= 0.5:
        # A zero-width rounded rect renders as a dot; nothing is clearer.
        return track
    return track + (
        f'<rect x="{left}" y="{y}" width="{filled:.1f}" height="{height}" '
        f'rx="{height / 2}" fill="{colour}"/>'
    )


def _corner(text, colour, y=34, size=16, weight="600"):
    return (f'<text x="{SIZE - 13}" y="{y}" fill="{colour}" '
            f'font-size="{size}" font-family="{FONT}" font-weight="{weight}" '
            f'text-anchor="end">{_escape(text)}</text>')


def _document(body, accent):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}" viewBox="0 0 {SIZE} {SIZE}">'
        f'<rect width="{SIZE}" height="{SIZE}" rx="20" fill="{THEME["bg"]}"/>'
        f'<rect x="1.5" y="1.5" width="{SIZE - 3}" height="{SIZE - 3}" '
        f'rx="18.5" fill="none" stroke="{accent}" stroke-width="3" '
        f'stroke-opacity="0.55"/>'
        f'{body}</svg>'
    )


def data_uri(svg):
    """OpenDeck accepts image/svg+xml; base64 avoids any quoting question."""
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


# ------------------------------------------------------------- formatting
def clock(seconds):
    """h:mm:ss once an hour has passed, mm:ss before that.

    A recording key is read at a glance, and "01:04:12" is only worth the
    extra characters once there is an hour to show.
    """
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, second = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{second:02d}"
    return f"{minutes:02d}:{second:02d}"


def duration(seconds):
    """A replay length as someone would say it: 30s, 5m, 1h."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes}m" if not rest else f"{minutes}m{rest:02d}"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours}h" if not minutes else f"{hours}h{minutes:02d}"


def size(byte_count):
    """A file size in the largest unit that keeps it under four digits."""
    value = float(max(0, byte_count))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if value >= 10 or unit == "B" \
                else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.0f}GB"


# -------------------------------------------------------------------- keys
def file_label(path):
    """A saved file's name in the two lines a key has room for.

    gpu-screen-recorder names files "Replay_2026-09-07_12-06-08.mp4", which is
    28 characters -- truncated to one line it becomes "Replay_2026-0…", which
    identifies nothing. Split on the underscores it puts there, the kind and
    the time are what tell two saves apart, and both fit.
    """
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    parts = stem.split("_")
    if len(parts) >= 3:
        return [_fit(parts[0], 14), parts[-1].replace("-", ":")]
    return _wrap(stem, 14)


def unavailable_key(glyph, headline, hint):
    """A key whose recorder is not there, which is a state, not an error."""
    body = (
        _glyph(glyph, THEME["idle"], 50, 22, 1.75, slashed=True)
        + _text_block(_wrap(headline, 13), 100, 19, THEME["dim"])
        + _text_block([_fit(hint, 18)], 124, 13, THEME["dim"], weight="400")
    )
    return _document(body, THEME["idle"])


def replay_key(on, seconds, fill=0.0, age=0.0, storage="ram",
              unavailable=False, note=""):
    """The replay buffer: whether it is running, how long, how full.

    The fill bar is shown only while the buffer is still filling. Once it has
    been up longer than its own length every save gives the full clip, and a
    permanently full bar is one more thing on the key that never changes.
    """
    if unavailable:
        return unavailable_key("replay", "Replay",
                               note or "gsr-ui is not running")
    if not on:
        body = (
            _glyph("replay", THEME["idle"], 11, 11, 1.3, slashed=True)
            + _corner("OFF", THEME["idle"], size=19, weight="bold")
            + _text_block(["Replay"], 88, 24, THEME["text"])
            + _text_block([f"{duration(seconds)} buffer" if seconds
                           else "press to start"], 116, 14, THEME["dim"],
                          weight="400")
        )
        return _document(body, THEME["idle"])

    accent = THEME["good"]
    filling = fill < 0.999
    body = (
        _glyph("replay", accent, 11, 11, 1.3)
        + _corner("ON", accent, size=19, weight="bold")
        + _text_block([duration(seconds)], 84, 30, THEME["text"])
        + _text_block([f"buffer · {storage.upper()}"], 108, 13, THEME["dim"],
                      weight="400")
    )
    if filling:
        body += _bar(fill, accent, 120, height=7)
        body += _text_block([f"{clock(age)} in"], 137, 11, THEME["dim"],
                            weight="400")
    else:
        body += _text_block([f"up {clock(age)}"], 130, 13, THEME["dim"],
                            weight="400")
    return _document(body, accent)


def save_key(length_label, ready, flash="", pending=False, note=""):
    """The Save Replay key: how much it will save, and what it just saved.

    After a save the filename replaces the hint for a few seconds. Saving is
    otherwise completely invisible -- the buffer keeps running, no window
    opens -- so without the flash the key feels like it did nothing.
    """
    if pending:
        accent = THEME["live"]
        body = (
            _glyph("save", accent, 11, 11, 1.3)
            + _corner(length_label, THEME["dim"])
            + _text_block(["Saving…"], 88, 23, THEME["text"])
            + _bar(1.0, accent, 112, height=7)
        )
        return _document(body, accent)
    if flash:
        accent = THEME["good"]
        body = (
            _glyph("save", accent, 11, 11, 1.3)
            + _corner("SAVED", accent, weight="bold")
            + _text_block(file_label(flash) or ["saved"], 90, 16,
                          THEME["text"])
            + _text_block(["✓"], 126, 20, accent)
        )
        return _document(body, accent)
    if not ready:
        return unavailable_key("save", "Save replay",
                               note or "replay is off")
    accent = THEME["live"]
    body = (
        _glyph("save", accent, 11, 11, 1.3)
        + _corner(length_label, accent, weight="bold")
        + _text_block(["Save"], 90, 26, THEME["text"])
        + _text_block(["replay"], 116, 15, THEME["dim"], weight="400")
    )
    return _document(body, accent)


def record_key(active, elapsed=0.0, byte_count=0, paused=False,
               in_replay=False, unavailable=False, note=""):
    """The recording key: whether it is rolling, for how long, how big.

    The size matters more than it looks: a recording that has stopped
    growing is a recording that has silently failed, and the number is the
    only place that shows.
    """
    if unavailable:
        return unavailable_key("record", "Record",
                               note or "gsr-ui is not running")
    if not active:
        body = (
            _glyph("record", THEME["idle"], 11, 11, 1.3)
            + _corner("OFF", THEME["idle"], size=19, weight="bold")
            + _text_block(["Record"], 90, 24, THEME["text"])
            + _text_block(["during replay" if in_replay else "press to start"],
                          118, 13, THEME["dim"], weight="400")
        )
        return _document(body, THEME["idle"])

    accent = THEME["live"] if paused else THEME["hot"]
    body = (
        _glyph("pause" if paused else "record", accent, 11, 11, 1.3)
        + _corner("PAUSED" if paused else "REC", accent, size=17,
                  weight="bold")
        + _text_block([clock(elapsed)], 86, 29, THEME["text"])
        + _text_block([size(byte_count) if byte_count else "starting…"],
                      112, 15, THEME["dim"], weight="400")
        + (_text_block(["in replay"], 132, 12, THEME["dim"], weight="400")
           if in_replay else "")
    )
    return _document(body, accent)


def stream_key(live, service="", elapsed=0.0, unavailable=False, note=""):
    """The streaming key. The service is where it goes, not what it is."""
    if unavailable:
        return unavailable_key("stream", "Stream",
                               note or "gsr-ui is not running")
    if not live:
        body = (
            _glyph("stream", THEME["idle"], 11, 11, 1.3, slashed=True)
            + _corner("OFF", THEME["idle"], size=19, weight="bold")
            + _text_block(["Stream"], 90, 24, THEME["text"])
            + _text_block([_fit(service or "press to start", 16)], 118, 13,
                          THEME["dim"], weight="400")
        )
        return _document(body, THEME["idle"])
    accent = THEME["hot"]
    body = (
        _glyph("stream", accent, 11, 11, 1.3)
        + _corner("LIVE", accent, size=18, weight="bold")
        + _text_block([_fit(service or "streaming", 11)], 88, 22,
                      THEME["text"])
        + _text_block([clock(elapsed)], 118, 18, THEME["dim"], weight="400")
    )
    return _document(body, accent)


def screenshot_key(mode_label, flash="", unavailable=False, note=""):
    """The screenshot key: which area it takes, and what it just wrote."""
    if unavailable:
        return unavailable_key("camera", "Screenshot",
                               note or "gsr-ui is not running")
    if flash:
        accent = THEME["good"]
        return _document(
            _glyph("camera", accent, 11, 11, 1.3)
            + _corner("SAVED", accent, weight="bold")
            + _text_block(file_label(flash), 92, 16, THEME["text"])
            + _text_block(["✓"], 128, 20, accent),
            accent)
    accent = THEME["live"]
    body = (
        _glyph("camera", accent, 11, 11, 1.3)
        + _text_block(["Screenshot"], 92, 20, THEME["text"])
        + _text_block([mode_label], 118, 14, THEME["dim"], weight="400")
    )
    return _document(body, accent)


def status_key(lines, headline="Idle", accent_name="idle"):
    """A read-only key: what the recorder is, in four short lines.

    Deliberately dense and deliberately not pressable. It exists so a glance
    at the deck answers "is it even capturing the right monitor", which is
    the question that otherwise means opening the overlay mid-game.
    """
    accent = THEME.get(accent_name, THEME["idle"])
    body = _glyph("status", accent, 11, 9, 1.15)
    body += _corner(_fit(headline, 8), accent, y=32, size=17, weight="bold")
    top = 62
    for index, line in enumerate(lines[:4]):
        body += _text_block([_fit(line, 20)], top + index * 19, 14,
                            THEME["text"] if index == 0 else THEME["dim"],
                            weight="600" if index == 0 else "400")
    return _document(body, accent)
