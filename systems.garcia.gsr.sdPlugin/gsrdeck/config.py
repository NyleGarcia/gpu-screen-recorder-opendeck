"""Read GPU Screen Recorder's own configuration.

Two files matter, both plain "key value" text, one pair per line:

    ~/.config/gpu-screen-recorder/config      written by gpu-screen-recorder-gtk
    ~/.config/gpu-screen-recorder/config_ui   written by gsr-ui, the overlay

The Flathub build keeps the same two files in its own config directory,
~/.var/app/com.dec05eba.gpu_screen_recorder/config/gpu-screen-recorder/, and
that is looked in as well.

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

FLATPAK_APP = "com.dec05eba.gpu_screen_recorder"
# A package install first, then the Flathub app, whose sandbox moves
# XDG_CONFIG_HOME under ~/.var/app.
CONFIG_DIRS = (
    os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "gpu-screen-recorder"),
    os.path.join(os.path.expanduser("~/.var/app"), FLATPAK_APP, "config",
                 "gpu-screen-recorder"),
)
UI_CONFIG = "config_ui"
GTK_CONFIG = "config"

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


def _locate(name):
    """Where the file called `name` is, or "" if no install has written one.

    With both a package install and the Flathub app on one machine, the file
    written last is the one in use: a config left behind by the install that
    was replaced would otherwise win for ever.
    """
    newest, newest_at = "", None
    for directory in CONFIG_DIRS:
        path = os.path.join(directory, name)
        try:
            written_at = os.stat(path).st_mtime
        except OSError:
            continue
        if newest_at is None or written_at > newest_at:
            newest, newest_at = path, written_at
    return newest


def _load(name):
    now = time.monotonic()
    if name not in _cache or now - _cache_at.get(name, 0.0) >= TTL:
        path = _locate(name)
        _cache[name] = _parse(path) if path else {}
        _cache_at[name] = now
    return _cache[name]


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
