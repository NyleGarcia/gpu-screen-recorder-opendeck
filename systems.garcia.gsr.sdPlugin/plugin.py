"""GPU Screen Recorder plugin for OpenDeck.

One process. It speaks OpenDeck's WebSocket protocol, reads what the recorder
is doing out of /proc, and drives it with its IPC socket, with signals, or
through gsr-ui-cli -- see gsrdeck/control.py for why there are three.

The plugin deliberately owns no recording settings. Which monitor, which
codec, which audio tracks, where files land: all of that is chosen in GPU
Screen Recorder's own UI, and the deck asks that UI to start things rather
than starting a second recorder configured differently. What the deck adds is
what a full-screen overlay cannot: the state visible without leaving the game,
and one physical key that saves the buffer.

Anything that can block runs on a worker thread. Saving a thirty minute buffer
takes as long as writing thirty minutes of video, and the event loop has to
keep answering OpenDeck or every key on the deck stops responding while a save
is in flight.

Nothing here may raise to the top level. OpenDeck does not restart a plugin
that dies -- the keys simply stop responding, with no indication why -- so
every handler is wrapped and every failure is logged and swallowed.
"""

import json
import logging
import math
import os
import queue
import re
import select
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gsrdeck import config, control, i18n, render, state   # noqa: E402
from gsrdeck.i18n import t                           # noqa: E402
from gsrdeck.ws import WebSocket                     # noqa: E402

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "plugin.log")
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG if os.environ.get("GSR_DECK_DEBUG") else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gsr-deck")

REPLAY = "systems.garcia.gsr.replay"
SAVE = "systems.garcia.gsr.save"
RECORD = "systems.garcia.gsr.record"
STREAM = "systems.garcia.gsr.stream"
SCREENSHOT = "systems.garcia.gsr.screenshot"
STATUS = "systems.garcia.gsr.status"
SECTION = "systems.garcia.gsr.section"

# Every dial draws its whole touch strip as one SVG into the single pixmap this
# layout defines. The built-in layouts carry a title and an icon slot that
# OpenDeck paints over the top of anything sent to them.
ENCODER_LAYOUT = "layouts/strip.json"
CANVAS = "canvas"

# Keys are redrawn on this cadence so a recording's clock ticks and a change
# made in the overlay shows up without the deck being touched first.
REFRESH_SECONDS = 1.0
# /proc is scanned at most this often and the result shared by every key, so a
# deck full of recorder keys costs one scan per tick, not one per key.
SCAN_SECONDS = 0.5
# How long "saved ✓ <file>" stays on a key. Long enough to read a filename,
# short enough that the key is back to its normal job before the next save.
FLASH_SECONDS = 4.0
# gsr-ui is asked to start or stop something and does not report back, so the
# key is given this long to show what was asked before it goes back to
# drawing what it can see. Without it a press looks like it did nothing for
# the second or two a recorder takes to appear.
BUSY_SECONDS = 2.5

# What the Save Replay key can be set to. 0 is the whole buffer, which is the
# only length the socket and the signals agree on exactly.
SAVE_LENGTHS = [0, 10, 30, 60, 300, 600, 1800]
# What a Save dial steps through when the recorder has a socket and any
# length saves exactly. Without one the dial steps through the signal presets
# instead, so it never lands on a length it would have to round.
DIAL_LENGTHS = [5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 600, 900, 1200,
                1800, 3600]
# How far one detent moves a section's mark, by default and as offered.
SECTION_STEPS = [1, 5, 10, 15, 30]
SECTION_STEP = 5
# How long a Replay dial stays on a browsed save after the last turn before
# it goes back to showing the buffer.
BROWSE_SECONDS = 6.0
# The replay directory is listed at most this often for the "saved 2m ago"
# line and for browsing; it can hold thousands of files.
SAVES_SECONDS = 2.0

AREA_COMMANDS = {
    "record": {"auto": "toggle-record", "region": "toggle-record-region",
               "window": "toggle-record-window"},
    "screenshot": {"auto": "take-screenshot",
                   "region": "take-screenshot-region",
                   "window": "take-screenshot-window"},
}
AREAS = ("auto", "region", "window")

# gpu-screen-recorder names what it writes after the moment it started, which
# is the only record of when a recording began that survives this plugin being
# restarted mid-recording.
_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})")


def save_length(settings):
    """The number of seconds a Save key is set to, or 0 for everything."""
    try:
        seconds = int(float(settings.get("seconds", 0)))
    except (TypeError, ValueError):
        return 0
    return seconds if seconds in SAVE_LENGTHS or seconds in DIAL_LENGTHS \
        else 0


def dial_lengths(buffer_seconds, exact):
    """The lengths a Save dial turns through, shortest first, whole last.

    Lengths the buffer cannot hold are left out: on a two minute buffer "save
    the last 5m" is the whole buffer with a misleading label.
    """
    steps = DIAL_LENGTHS if exact else sorted(control.PRESETS)
    if buffer_seconds:
        steps = [seconds for seconds in steps if seconds < buffer_seconds]
    return list(steps) + [0]


def section_step(settings):
    """Seconds one detent moves a section's mark."""
    try:
        step = int(float(settings.get("step", SECTION_STEP)))
    except (TypeError, ValueError):
        return SECTION_STEP
    return step if step in SECTION_STEPS else SECTION_STEP


def is_encoder(settings):
    return (settings or {}).get("controller") == "Encoder"


def length_label(seconds, exact=True):
    """What the key prints in its corner.

    Marked when a signal will round it: a key set to five minutes on a
    recorder with no socket saves ten, and finding that out from the length of
    the resulting clip is a bad way to find it out.
    """
    if not seconds:
        return t("key.all")
    label = render.duration(seconds)
    return label if exact else label + "*"


def file_started_at(path):
    """When a recording began, from the timestamp in its own filename."""
    match = _STAMP.search(os.path.basename(path or ""))
    if not match:
        return None
    try:
        stamp = time.mktime((
            int(match.group(1)), int(match.group(2)), int(match.group(3)),
            int(match.group(4)), int(match.group(5)), int(match.group(6)),
            0, 0, -1))
    except (ValueError, OverflowError):
        return None
    return stamp


def opendeck_language(info):
    """The language OpenDeck is set to, from the -info it starts plugins with.

    The system locale is the fallback, for a host that leaves it out.
    """
    try:
        language = (json.loads(info or "{}").get("application")
                    or {}).get("language")
    except (ValueError, AttributeError):
        language = None
    return language or os.environ.get("LC_ALL") or os.environ.get("LANG")


class Plugin:
    def __init__(self, port, uuid, register_event):
        self._ws = WebSocket(port, timeout=1.0)
        self._uuid = uuid
        self._ws.send_json({"event": register_event, "uuid": uuid})
        log.info("registered as %s on port %s", uuid, port)
        # The theme is plugin-wide, not per key: a deck with one key in Amber
        # and the rest in Default looks broken rather than customised. Asked
        # for at startup because global settings are not pushed unprompted.
        self._ws.send_json({"event": "getGlobalSettings", "context": uuid})
        self._contexts = {}
        # context -> the last payload drawn, so a tick that changes nothing
        # sends nothing: the deck redraws on receipt, and a key repainting
        # every second visibly flickers.
        self._drawn = {}
        self._flash = {}
        self._busy = {}
        self._pending = set()
        self._jobs = queue.Queue()
        self._instances = []
        self._scanned_at = 0.0
        self._last_refresh = 0.0
        # Pause is the one thing about a recorder that cannot be read from
        # outside it, so what the deck asked for is remembered, keyed by the
        # process it was asked of. A new recorder starts unpaused.
        self._paused = {}
        # context -> (replay pid, monotonic time of the mark). Keyed by the
        # replay it was dropped in: a mark outlives nothing, and a buffer
        # restarted since holds none of what came before it.
        self._marks = {}
        # context -> (index into recent saves, monotonic time it ends).
        self._browse = {}
        self._saves_cache = (0.0, "", [])
        self._theme = render.DEFAULT_THEME

    # ------------------------------------------------------------ outbound
    def _send(self, event, context, payload=None):
        message = {"event": event, "context": context}
        if payload is not None:
            message["payload"] = payload
        log.debug("-> %s %s", event, str(payload)[:160])
        try:
            self._ws.send_json(message)
        except (OSError, ConnectionError) as exc:
            log.warning("send %s failed: %s", event, exc)

    def _paint(self, context, image):
        """Send an image only when it actually changed."""
        if self._drawn.get(context) == image:
            return
        self._drawn[context] = image
        self._send("setImage", context,
                   {"image": render.data_uri(image), "target": 0})
        # The image carries the text, so any title OpenDeck would draw on top
        # of it is duplication; clearing it keeps the key clean.
        self._send("setTitle", context, {"title": "", "target": 0})

    def _paint_strip(self, context, image):
        """The dial's touch strip, sent only when it changed.

        Encoders get the key image as well: OpenDeck's editor draws a dial
        from it, and with feedback alone every dial would sit there as the
        manifest's static icon.
        """
        if self._drawn.get((context, "strip")) == image:
            return
        self._drawn[(context, "strip")] = image
        self._send("setFeedback", context, {CANVAS: render.data_uri(image)})

    def _forget(self, context):
        """Drop what was last drawn, so the next render is sent regardless."""
        self._drawn.pop(context, None)
        self._drawn.pop((context, "strip"), None)

    def _persist(self, context, settings):
        """Store settings a dial changed, the way an inspector would."""
        self._contexts[context] = settings
        self._send("setSettings", context,
                   {k: v for k, v in settings.items()
                    if k not in ("controller", "action")})

    # --------------------------------------------------------- reading state
    def _scan(self, force=False):
        now = time.monotonic()
        if force or now - self._scanned_at >= SCAN_SECONDS:
            self._scanned_at = now
            self._instances = state.instances(now)
            live = {inst["pid"] for inst in self._instances}
            for pid in [p for p in self._paused if p not in live]:
                del self._paused[pid]
        return self._instances

    def _instance(self, mode):
        return state.pick(self._scan(), mode)

    def _recording(self):
        """The instance that is writing a file right now, and that file.

        A recording during replay and a standalone recording are the same
        thing to the key: both are "video is being written". Which recorder
        holds it decides how it is stopped, not how it is drawn.
        """
        for inst in self._scan():
            if inst.get("recording"):
                return inst, inst["recording"]
        return None, None

    def _recording_elapsed(self, inst, recording):
        """How long the current recording has been running.

        A standalone recorder starts when the recording starts, so its own age
        is the answer. A recording inside a replay does not -- the replay may
        have been up for hours -- so the time in the filename is used, and the
        age of the descriptor is the fallback for a name without one.
        """
        if inst["mode"] == "record":
            return inst.get("age") or 0.0
        started = file_started_at(recording[0])
        if started is not None:
            return max(0.0, time.time() - started)
        return recording[2]

    # ------------------------------------------------------------- drawing
    def _render(self, context):
        settings = self._contexts.get(context) or {}
        action = settings.get("action", STATUS)
        busy_until = self._busy.get(context, 0.0)
        if action == REPLAY:
            self._render_replay(context, settings, busy_until)
        elif action == SAVE:
            self._render_save(context, settings)
        elif action == RECORD:
            self._render_record(context, settings, busy_until)
        elif action == STREAM:
            self._render_stream(context, settings, busy_until)
        elif action == SCREENSHOT:
            self._render_screenshot(context, settings)
        elif action == STATUS:
            self._render_status(context, settings)
        elif action == SECTION:
            self._render_section(context, settings)

    def _flash_path(self, context):
        """The file a recent press produced, while it is still worth showing."""
        path, until = self._flash.get(context, ("", 0.0))
        if until and time.monotonic() >= until:
            del self._flash[context]
            return ""
        return path

    def _render_replay(self, context, settings, busy_until):
        inst = self._instance("replay")
        encoder = is_encoder(settings)
        if inst is None:
            buffer = config.replay_seconds()
            if time.monotonic() < busy_until:
                note, unavailable = t("key.starting"), False
            elif not control.ui_running():
                note, unavailable = t("key.ui_not_running"), True
            else:
                note, unavailable = "", False
            self._paint(context, render.replay_key(
                False, 0 if unavailable else buffer, unavailable=unavailable,
                note=note))
            if encoder:
                self._paint_strip(context, render.replay_strip(
                    False, buffer, browse=self._browsing(context, inst),
                    unavailable=unavailable, note=note))
            return
        fill = state.buffer_fill(inst)
        self._paint(context, render.replay_key(
            True, inst["replay_seconds"], fill, inst["age"],
            inst["replay_storage"]))
        if encoder:
            saves = self._saves(inst)
            last = max(0.0, time.time() - saves[0][1]) if saves else None
            self._paint_strip(context, render.replay_strip(
                True, inst["replay_seconds"], fill, inst["age"],
                inst["replay_storage"], last_save=last,
                browse=self._browsing(context, inst)))

    def _saves(self, inst):
        """The newest videos in the replay directory, listed at most every
        couple of seconds however many dials ask."""
        directory = (inst or {}).get("output") or config.replay_directory()
        listed_at, cached_dir, saves = self._saves_cache
        now = time.monotonic()
        if cached_dir != directory or now - listed_at >= SAVES_SECONDS:
            saves = control.recent_files(directory) if directory else []
            self._saves_cache = (now, directory, saves)
        return saves

    def _browsing(self, context, inst):
        """(path, age, size, index, count) of the save a dial is turned to."""
        index, until = self._browse.get(context, (0, 0.0))
        if time.monotonic() >= until:
            self._browse.pop(context, None)
            return None
        saves = self._saves(inst)
        if not saves:
            return None
        index = min(index, len(saves) - 1)
        path, mtime, byte_count = saves[index]
        return (path, max(0.0, time.time() - mtime), byte_count, index,
                len(saves))

    def _render_save(self, context, settings):
        inst = self._instance("replay")
        seconds = save_length(settings)
        exact = bool(inst and control.socket_alive(inst.get("ipc")))
        if not exact and seconds:
            seconds = control.snap_seconds(seconds)
        label = length_label(seconds, exact or not seconds)
        flash = self._flash_path(context)
        pending = context in self._pending
        self._paint(context, render.save_key(
            label, inst is not None, flash=flash, pending=pending,
            note=t("key.replay_off")))
        if is_encoder(settings):
            buffer = inst["replay_seconds"] if inst else 0
            fraction = min(1.0, seconds / buffer) if seconds and buffer \
                else 1.0
            self._paint_strip(context, render.save_strip(
                label, inst is not None, fraction, flash=flash,
                pending=pending, note=t("key.replay_off")))

    def _section(self, context, inst):
        """What a press on a marked Section would save, or None.

        The length is rounded up, never to the nearest: the point of a mark
        is the moment it was dropped for, and a signalled save rounded down
        cuts exactly that off. Past what the buffer holds, the answer is the
        whole buffer and the key says so.
        """
        mark = self._marks.get(context)
        if mark is None or inst is None or mark[0] != inst["pid"]:
            self._marks.pop(context, None)
            return None
        elapsed = max(0.0, time.monotonic() - mark[1])
        buffer = inst.get("replay_seconds") or 0
        held = min(buffer, inst.get("age") or 0.0) if buffer \
            else inst.get("age") or elapsed
        capped = elapsed >= held
        wanted = int(math.ceil(min(elapsed, held)))
        exact = control.socket_alive(inst.get("ipc"))
        if capped or (buffer and wanted >= buffer):
            seconds = 0
        elif exact:
            seconds = max(1, wanted)
        else:
            seconds = control.cover_seconds(max(1, wanted))
            if buffer and seconds >= buffer:
                seconds = 0
        return {
            "elapsed": elapsed,
            "seconds": seconds,
            "label": length_label(seconds, exact or not seconds),
            "fraction": min(1.0, elapsed / buffer) if buffer else 0.0,
            "capped": capped,
        }

    def _render_section(self, context, settings):
        inst = self._instance("replay")
        section = self._section(context, inst)
        flash = self._flash_path(context)
        pending = context in self._pending
        common = {"ready": inst is not None, "flash": flash,
                  "pending": pending, "note": t("key.replay_off")}
        if section is None:
            self._paint(context, render.section_key(False, **common))
            if is_encoder(settings):
                self._paint_strip(context, render.section_strip(
                    False, **common))
            return
        self._paint(context, render.section_key(
            True, section["elapsed"], section["label"],
            capped=section["capped"], **common))
        if is_encoder(settings):
            self._paint_strip(context, render.section_strip(
                True, section["elapsed"], section["label"],
                section["fraction"], capped=section["capped"], **common))

    def _render_record(self, context, settings, busy_until):
        inst, recording = self._recording()
        encoder = is_encoder(settings)
        if recording is not None:
            args = (True, self._recording_elapsed(inst, recording),
                    recording[1])
            options = {"paused": self._paused.get(inst["pid"], False),
                       "in_replay": inst["mode"] != "record"}
        else:
            host = self._instance("record")
            options = {"in_replay": bool(host and host["mode"] != "record")}
            args = (False,)
            if time.monotonic() < busy_until:
                options["note"] = t("key.starting")
            elif host is None and not control.ui_running():
                options = {"unavailable": True,
                           "note": t("key.ui_not_running")}
        self._paint(context, render.record_key(*args, **options))
        if encoder:
            self._paint_strip(context, render.record_strip(*args, **options))

    def _render_stream(self, context, settings, busy_until):
        inst = self._instance("stream")
        if inst is not None:
            self._paint(context, render.stream_key(
                True, config.stream_service(), inst["age"]))
            return
        if time.monotonic() < busy_until:
            self._paint(context, render.stream_key(
                False, t("key.starting")))
            return
        if not control.ui_running():
            self._paint(context, render.stream_key(
                False, unavailable=True, note=t("key.ui_not_running")))
            return
        self._paint(context, render.stream_key(False, config.stream_service()))

    def _render_screenshot(self, context, settings):
        area = settings.get("area", "auto")
        if not control.ui_running():
            self._paint(context, render.screenshot_key(
                "", unavailable=True, note=t("key.ui_not_running")))
            return
        self._paint(context, render.screenshot_key(
            t(f"key.area.{area if area in AREAS else 'auto'}"),
            flash=self._flash_path(context)))

    def _render_status(self, context, settings):
        wanted = settings.get("source", "auto")
        instances = self._scan()
        inst = None
        if wanted == "auto":
            inst = instances[0] if instances else None
        else:
            inst = state.pick(instances, wanted)
        if inst is None:
            lines = [t("key.status.nothing")]
            if config.replay_seconds():
                lines.append(t("key.status.replay_line",
                               length=render.duration(config.replay_seconds()),
                               storage=config.replay_storage()))
            if config.stream_service():
                lines.append(t("key.status.stream_line",
                               service=config.stream_service()))
            lines.append(t("key.status.ui_up") if control.ui_running()
                         else t("key.status.ui_down"))
            self._paint(context, render.status_key(
                lines, t("key.status.idle"), "idle"))
            return

        headline = {"replay": t("key.status.replay"), "record": t("key.rec"),
                    "stream": t("key.live")}.get(inst["mode"], t("key.on"))
        accent = {"replay": "good", "record": "hot",
                  "stream": "hot"}.get(inst["mode"], "live")
        first = {
            "replay": lambda: t(
                "key.status.buffer",
                length=render.duration(inst["replay_seconds"]),
                storage=inst["replay_storage"]),
            "record": lambda: t("key.status.recording",
                                clock=render.clock(inst["age"])),
            "stream": lambda: t("key.status.live",
                                clock=render.clock(inst["age"])),
        }[inst["mode"]]()
        quality = inst["options"].get("-q", "")
        if inst["bitrate"]:
            quality = f"{inst['bitrate'] / 1000:.0f} Mbps"
        lines = [
            first,
            t("key.status.capture",
              capture=inst["capture"] or t("key.status.unknown"),
              fps=inst["fps"] or "?"),
            f"{inst['codec']}"
            + (f" · {quality}" if quality else ""),
            t("key.status.audio", count=len(inst["audio"]),
              size=render.size(inst["rss_kb"] * 1024)),
        ]
        self._paint(context, render.status_key(lines, headline, accent))

    def _render_all(self):
        for context in list(self._contexts):
            try:
                self._render(context)
            except Exception:                      # noqa: BLE001
                log.exception("render failed for %s", context)

    # ------------------------------------------------------------- actions
    def _run(self, context, work):
        """Do something slow without stalling the deck.

        One thread per press rather than a pool: presses are rare, the work is
        almost all waiting, and a pool would need a queue whose only effect
        would be to make two presses run one after the other -- which is
        exactly what should not happen when the first one is a thirty minute
        save.
        """
        if context in self._pending:
            return
        self._pending.add(context)
        self._forget(context)

        def body():
            try:
                result = work()
            except Exception as exc:               # noqa: BLE001
                log.exception("job failed")
                result = control.Result(False, error=str(exc))
            self._jobs.put((context, result))

        threading.Thread(target=body, daemon=True).start()
        self._render(context)

    def _finish(self, context, result):
        self._pending.discard(context)
        self._forget(context)
        if result.ok:
            self._send("showOk", context)
            if result.path:
                self._flash[context] = (result.path,
                                        time.monotonic() + FLASH_SECONDS)
        else:
            log.warning("%s failed: %s", context, result.error)
            self._send("showAlert", context)
        self._scan(force=True)
        self._render(context)

    def _mark_busy(self, context):
        self._busy[context] = time.monotonic() + BUSY_SECONDS
        self._forget(context)

    def _press(self, context):
        settings = self._contexts.get(context) or {}
        action = settings.get("action")
        if action == REPLAY:
            self._press_replay(context, settings)
        elif action == SAVE:
            self._press_save(context, settings)
        elif action == RECORD:
            if is_encoder(settings):
                # A dial pauses by turning, so its press is always start and
                # stop whatever the key setting says.
                settings = dict(settings, press="toggle")
            self._press_record(context, settings)
        elif action == STREAM:
            self._press_stream(context)
        elif action == SCREENSHOT:
            self._press_screenshot(context, settings)
        elif action == SECTION:
            self._press_section(context)
        # A status key is a readout. Pressing it does nothing on purpose:
        # there is no sensible single action for "the recorder in general",
        # and guessing one is how a glance at the deck turns into a stopped
        # recording.

    def _press_replay(self, context, settings):
        running = self._instance("replay") is not None
        wanted = settings.get("press", "toggle")
        if (wanted == "start" and running) or (wanted == "stop"
                                               and not running):
            self._send("showOk", context)
            return
        self._mark_busy(context)
        self._run(context, lambda: control.ui_cli("toggle-replay"))

    def _press_save(self, context, settings):
        inst = self._instance("replay")
        if inst is None:
            self._send("showAlert", context)
            return
        seconds = save_length(settings)
        restart = settings.get("restart")
        restart = None if restart is None else bool(restart)
        self._run(context,
                  lambda: control.save_replay(inst, seconds, restart))

    def _press_record(self, context, settings):
        inst, recording = self._recording()
        if settings.get("press") == "pause":
            if recording is None:
                self._send("showAlert", context)
                return
            self._paused[inst["pid"]] = not self._paused.get(inst["pid"],
                                                             False)
            self._run(context, lambda: control.toggle_pause(inst))
            return
        if recording is not None:
            # Stop the way it was started. A recording inside a replay is
            # stopped in that recorder; stopping the recorder itself would
            # take the replay buffer down with it.
            if inst["mode"] != "record":
                self._run(context, lambda: control.toggle_recording(inst))
            else:
                self._run(context, lambda: control.stop(inst))
            return
        host = self._instance("record")
        if host is not None and host["mode"] != "record":
            self._mark_busy(context)
            self._run(context, lambda: control.toggle_recording(host))
            return
        command = AREA_COMMANDS["record"].get(settings.get("area", "auto"),
                                              "toggle-record")
        self._mark_busy(context)
        self._run(context, lambda: control.ui_cli(command))

    def _press_section(self, context):
        """First press drops a mark; the next saves from it to now."""
        inst = self._instance("replay")
        if inst is None:
            self._send("showAlert", context)
            return
        if context in self._pending:
            return
        section = self._section(context, inst)
        if section is None:
            self._marks[context] = (inst["pid"], time.monotonic())
            self._forget(context)
            self._render(context)
            return
        del self._marks[context]
        seconds = section["seconds"]
        self._run(context, lambda: control.save_replay(inst, seconds))

    # --------------------------------------------------------------- dials
    def _rotate(self, context, ticks):
        if not ticks:
            return
        settings = self._contexts.get(context) or {}
        action = settings.get("action")
        if action == SAVE:
            self._rotate_save(context, settings, ticks)
        elif action == SECTION:
            self._rotate_section(context, settings, ticks)
        elif action == REPLAY:
            self._rotate_replay(context, ticks)
        elif action == RECORD:
            self._rotate_record(context, ticks)
        self._render(context)

    def _rotate_save(self, context, settings, ticks):
        """Turn through the lengths this recorder can save, and keep it."""
        inst = self._instance("replay")
        exact = bool(inst and control.socket_alive(inst.get("ipc")))
        buffer = inst["replay_seconds"] if inst else config.replay_seconds()
        choices = dial_lengths(buffer, exact)
        current = save_length(settings)
        if current and not exact:
            current = control.snap_seconds(current)
        if current in choices:
            index = choices.index(current)
        else:
            # A length longer than this buffer reads as the whole of it.
            index = len(choices) - 1
        index = max(0, min(len(choices) - 1, index + ticks))
        self._persist(context, dict(settings, seconds=choices[index]))

    def _rotate_section(self, context, settings, ticks):
        """Move the mark: left is further back, right is closer to now.

        With no mark, turning left drops one that many steps in the past --
        "the last 20 seconds, and keep going" -- and turning right past now
        takes the mark away again.
        """
        inst = self._instance("replay")
        if inst is None or context in self._pending:
            return
        now = time.monotonic()
        mark = self._marks.get(context)
        if mark is not None and mark[0] != inst["pid"]:
            mark = None
        start = (mark[1] if mark else now) + ticks * section_step(settings)
        buffer = inst.get("replay_seconds") or 0
        held = min(buffer, inst.get("age") or 0.0) if buffer \
            else inst.get("age") or 0.0
        start = max(now - held, start)
        if start >= now:
            self._marks.pop(context, None)
        else:
            self._marks[context] = (inst["pid"], start)
        self._forget(context)

    def _rotate_replay(self, context, ticks):
        """Turn through recent saves: right is older, left back to newer."""
        saves = self._saves(self._instance("replay"))
        if not saves:
            return
        index, until = self._browse.get(context, (0, 0.0))
        if time.monotonic() >= until:
            # The first detent shows the newest save rather than skipping it.
            index = -1 if ticks > 0 else 0
            if ticks < 0:
                return
        index += ticks
        if index < 0:
            self._browse.pop(context, None)
            return
        index = min(index, len(saves) - 1)
        self._browse[context] = (index, time.monotonic() + BROWSE_SECONDS)

    def _rotate_record(self, context, ticks):
        """Left pauses, right resumes. Only ever changes something once."""
        inst, recording = self._recording()
        if recording is None or context in self._pending:
            return
        paused = self._paused.get(inst["pid"], False)
        if (ticks < 0) == paused:
            return
        self._paused[inst["pid"]] = not paused
        self._run(context, lambda: control.toggle_pause(inst))

    def _tap(self, context):
        """A tap on the touch strip. Like a press, except where it can't be.

        On Replay a press already toggles the buffer, so a tap opens the
        browsed save (or starts browsing); on Section a press is mark/save,
        so a tap is the only way to throw a mark away.
        """
        settings = self._contexts.get(context) or {}
        action = settings.get("action")
        if action == REPLAY:
            browsing = self._browsing(context, self._instance("replay"))
            if browsing is None:
                if self._saves(self._instance("replay")):
                    self._browse[context] = (0, time.monotonic()
                                             + BROWSE_SECONDS)
                else:
                    self._send("showAlert", context)
                self._render(context)
                return
            self._browse.pop(context, None)
            path = browsing[0]
            self._run(context, lambda: control.open_file(path))
            return
        if action == SECTION and context in self._marks:
            del self._marks[context]
            self._forget(context)
            self._render(context)
            return
        self._press(context)

    def _press_stream(self, context):
        self._mark_busy(context)
        self._run(context, lambda: control.ui_cli("toggle-stream"))

    def _press_screenshot(self, context, settings):
        command = AREA_COMMANDS["screenshot"].get(settings.get("area", "auto"),
                                                  "take-screenshot")
        directory = config.screenshot_directory()

        def take():
            _, before = control.newest_file(directory)
            result = control.ui_cli(command)
            if not result.ok:
                return result
            # A region or window screenshot waits for someone to drag a box,
            # so the watch has to outlast the picking, not just the writing.
            result.path = control.await_new_file(directory, before, 30.0)
            return result

        self._run(context, take)

    # ------------------------------------------------- property inspector
    def _inspector_payload(self, action, settings):
        instances = self._scan(force=True)
        replay = state.pick(instances, "replay")
        payload = {
            "language": i18n.language(),
            "strings": i18n.table("pi."),
            "themes": render.theme_choices(),
            "theme": self._theme,
            "ui": control.ui_running(),
            "ui_cli": control.ui_cli_available(),
            "running": [
                {"mode": inst["mode"], "pid": inst["pid"],
                 "ipc": bool(inst["ipc"]), "capture": inst["capture"]}
                for inst in instances
            ],
            "settings": {k: v for k, v in settings.items()
                         if k not in ("controller", "action")},
        }
        payload["controller"] = settings.get("controller", "Keypad")
        if action == SAVE:
            payload["exact"] = bool(replay
                                    and control.socket_alive(replay["ipc"]))
            payload["lengths"] = [
                {"value": seconds,
                 "label": t("pi.whole_buffer") if not seconds
                          else render.duration(seconds)}
                for seconds in SAVE_LENGTHS
            ]
            payload["buffer"] = (replay["replay_seconds"] if replay
                                 else config.replay_seconds())
            payload["buffer_label"] = render.duration(payload["buffer"] or 0)
        if action == SECTION:
            payload["exact"] = bool(replay
                                    and control.socket_alive(replay["ipc"]))
            payload["buffer"] = (replay["replay_seconds"] if replay
                                 else config.replay_seconds())
            payload["steps"] = SECTION_STEPS
        if action in (REPLAY, STATUS):
            payload["buffer"] = (replay["replay_seconds"] if replay
                                 else config.replay_seconds())
            payload["storage"] = (replay["replay_storage"] if replay
                                  else config.replay_storage())
        if action == STREAM:
            payload["service"] = config.stream_service()
        if action == SCREENSHOT:
            payload["directory"] = config.screenshot_directory()
        return payload

    # ------------------------------------------------------------- inbound
    def _handle(self, message):
        event = message.get("event")
        context = message.get("context")
        payload = message.get("payload") or {}

        if event in ("willAppear", "didReceiveSettings"):
            settings = dict(payload.get("settings") or {})
            settings["controller"] = payload.get("controller", "Keypad")
            settings["action"] = message.get("action", STATUS)
            self._contexts[context] = settings
            # A redraw after a settings change must not be suppressed by the
            # previous setting's cached image.
            self._forget(context)
            if settings["controller"] == "Encoder":
                self._send("setFeedbackLayout", context,
                           {"layout": ENCODER_LAYOUT})
            self._render(context)
        elif event == "didReceiveGlobalSettings":
            name = (payload.get("settings") or {}).get("theme")
            self._theme = name if name in render.THEMES \
                else render.DEFAULT_THEME
            render.set_theme(self._theme)
            # Every cached image was drawn in the old palette, so the cache
            # has to go: otherwise a theme change would show only on the keys
            # that happened to change for some other reason.
            self._drawn.clear()
            self._render_all()
        elif event == "willDisappear":
            self._contexts.pop(context, None)
            self._forget(context)
            self._flash.pop(context, None)
            self._busy.pop(context, None)
            self._browse.pop(context, None)
            # The mark is kept. Flipping to another page of the deck and back
            # is a disappear and an appear, and should not throw away the
            # start of a section; a mark in a replay since restarted is
            # dropped when it is next read.
        elif event in ("keyDown", "dialDown"):
            self._press(context)
        elif event == "touchTap":
            self._tap(context)
        elif event == "dialRotate":
            try:
                ticks = int(payload.get("ticks", 0))
            except (TypeError, ValueError):
                ticks = 0
            self._rotate(context, ticks)
        elif event == "propertyInspectorDidAppear":
            # Pushed, not waited for. The panel asks once when its webview is
            # built, which can be long before anyone looks at it, and a reply
            # that arrives then is answered to nobody. This event fires when
            # the panel is actually on screen.
            settings = self._contexts.get(context) or {}
            self._send("sendToPropertyInspector", context,
                       self._inspector_payload(
                           message.get("action")
                           or settings.get("action") or STATUS, settings))
        elif event == "sendToPlugin":
            if payload.get("debug"):
                log.info("PI[%s] %s", context, payload["debug"])
                return
            action = (payload.get("action")
                      or message.get("action")
                      or (self._contexts.get(context) or {}).get("action")
                      or STATUS)
            self._send("sendToPropertyInspector", context,
                       self._inspector_payload(
                           action, self._contexts.get(context) or {}))

    # ---------------------------------------------------------------- loop
    def _drain_jobs(self):
        while True:
            try:
                context, result = self._jobs.get_nowait()
            except queue.Empty:
                return
            try:
                self._finish(context, result)
            except Exception:                      # noqa: BLE001
                log.exception("finishing %s failed", context)

    def run(self):
        while True:
            try:
                ready, _, _ = select.select([self._ws], [], [], 0.2)
                # Drained rather than read once per wake-up: several frames
                # arrive in one segment whenever a profile loads and every key
                # appears at once, and select() has nothing left to report
                # once they are in the buffer.
                while ready or self._ws.pending():
                    ready = False
                    try:
                        raw = self._ws.receive()
                    except socket.timeout:
                        break
                    if not raw:
                        continue
                    log.debug("<- %s", raw[:400])
                    try:
                        self._handle(json.loads(raw))
                    except Exception:              # noqa: BLE001
                        log.exception("handler failed: %s", raw[:200])
                self._drain_jobs()
                now = time.monotonic()
                if now - self._last_refresh >= REFRESH_SECONDS:
                    self._last_refresh = now
                    self._render_all()
            except ConnectionError as exc:
                log.error("connection lost: %s", exc)
                return
            except Exception:                      # noqa: BLE001
                log.exception("loop iteration failed; continuing")
                time.sleep(0.25)


def main():
    args = {}
    argv = sys.argv[1:]
    for i in range(0, len(argv) - 1, 2):
        args[argv[i].lstrip("-")] = argv[i + 1]
    port = args.get("port")
    uuid = args.get("pluginUUID")
    register_event = args.get("registerEvent", "registerPlugin")
    if not port or not uuid:
        log.error("missing -port/-pluginUUID; got %s", argv)
        return 1
    log.info("language: %s", i18n.set_language(
        opendeck_language(args.get("info"))))
    try:
        Plugin(int(port), uuid, register_event).run()
    except Exception:                              # noqa: BLE001
        log.exception("fatal")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
