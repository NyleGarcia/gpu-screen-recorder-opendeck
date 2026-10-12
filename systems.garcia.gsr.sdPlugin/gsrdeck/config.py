"""Read GPU Screen Recorder's own configuration.

Two files matter, both plain "key value" text, one pair per line:

    ~/.config/gpu-screen-recorder/config      written by gpu-screen-recorder-gtk
    ~/.config/gpu-screen-recorder/config_ui   written by gsr-ui, the overlay

They are read, never written. The plugin does not own any of these settings --
the replay length, the save directories, the streaming service are chosen in
the recorder's own UI -- and a key that showed a different buffer length from
the overlay would be worse than a key that showed nothing.

The value is what a key falls back to when nothing is running: with no
process to inspect there is no other way to say "replay is off, and it would
be 60 seconds if you turned it on".
"""

import os
import time

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "gpu-screen-recorder")
UI_CONFIG = os.path.join(CONFIG_DIR, "config_ui")
GTK_CONFIG = os.path.join(CONFIG_DIR, "config")

# Re-read at most this often. The files change when someone touches the
# overlay, which is rare compared to the once-a-second key refresh.
TTL = 5.0

_cache = {}
_cache_at = {}


def _parse(path):
    """One file as a dict. A key repeated across lines keeps the last value.

    Repeats are real -- audio tracks are one line each -- but nothing here
    needs the list, and keeping the last value means a caller never has to
    handle two shapes for one key.
    """
    values = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition(" ")
                if key:
                    values[key] = value
    except OSError:
        return {}
    return values


def _load(path):
    now = time.monotonic()
    if now - _cache_at.get(path, 0.0) >= TTL:
        _cache[path] = _parse(path)
        _cache_at[path] = now
    return _cache[path]


def ui():
    """gsr-ui's config, which is what a machine running the overlay uses."""
    return _load(UI_CONFIG)


def gtk():
    return _load(GTK_CONFIG)


def get(key, default=""):
    """A key from whichever config has it, preferring the overlay's."""
    value = ui().get(key)
    if value is None:
        value = gtk().get(key)
    return default if value is None else value


def number(key, default=0):
    try:
        return int(float(get(key, "")))
    except (TypeError, ValueError):
        return default


def replay_seconds():
    """The configured replay length, for when no recorder is running."""
    return number("replay.time", 0)


def replay_storage():
    return get("replay.replay_storage", "ram")


def replay_directory():
    return os.path.expanduser(get("replay.save_directory", ""))


def record_directory():
    return os.path.expanduser(get("record.save_directory", ""))


def screenshot_directory():
    return os.path.expanduser(get("screenshot.save_directory", ""))


def stream_service():
    return get("streaming.service", "")


def invalidate():
    """Forget the cache, so the next read hits the file."""
    _cache.clear()
    _cache_at.clear()
