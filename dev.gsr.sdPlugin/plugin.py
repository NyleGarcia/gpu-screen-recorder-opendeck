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
import os
import queue
import re
import select
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gsrdeck import config, control, render, state   # noqa: E402
from gsrdeck.ws import WebSocket                     # noqa: E402

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "plugin.log")
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG if os.environ.get("GSR_DECK_DEBUG") else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gsr-deck")

REPLAY = "dev.gsr.replay"
SAVE = "dev.gsr.save"
RECORD = "dev.gsr.record"
STREAM = "dev.gsr.stream"
SCREENSHOT = "dev.gsr.screenshot"
STATUS = "dev.gsr.status"

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

AREA_COMMANDS = {
    "record": {"auto": "toggle-record", "region": "toggle-record-region",
               "window": "toggle-record-window"},
    "screenshot": {"auto": "take-screenshot",
                   "region": "take-screenshot-region",
                   "window": "take-screenshot-window"},
}
AREA_LABELS = {"auto": "full screen", "region": "region", "window": "window"}

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
    return seconds if seconds in SAVE_LENGTHS else 0


def length_label(seconds, exact=True):
    """What the key prints in its corner.

    Marked when a signal will round it: a key set to five minutes on a
    recorder with no socket saves ten, and finding that out from the length of
    the resulting clip is a bad way to find it out.
    """
    if not seconds:
        return "ALL"
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

    def _flash_path(self, context):
        """The file a recent press produced, while it is still worth showing."""
        path, until = self._flash.get(context, ("", 0.0))
        if until and time.monotonic() >= until:
            del self._flash[context]
            return ""
        return path

    def _render_replay(self, context, settings, busy_until):
        inst = self._instance("replay")
        if inst is None:
            if time.monotonic() < busy_until:
                self._paint(context, render.replay_key(
                    False, config.replay_seconds(), note="starting…"))
                return
            if not control.ui_running():
                self._paint(context, render.replay_key(
                    False, 0, unavailable=True,
                    note="gsr-ui not running"))
                return
            self._paint(context, render.replay_key(
                False, config.replay_seconds()))
            return
        self._paint(context, render.replay_key(
            True, inst["replay_seconds"], state.buffer_fill(inst),
            inst["age"], inst["replay_storage"]))

    def _render_save(self, context, settings):
        inst = self._instance("replay")
        seconds = save_length(settings)
        exact = bool(inst and control.socket_alive(inst.get("ipc")))
        if not exact and seconds:
            seconds = control.snap_seconds(seconds)
        self._paint(context, render.save_key(
            length_label(seconds, exact or not seconds),
            inst is not None,
            flash=self._flash_path(context),
            pending=context in self._pending,
            note="replay is off"))

    def _render_record(self, context, settings, busy_until):
        inst, recording = self._recording()
        if recording is not None:
            self._paint(context, render.record_key(
                True, self._recording_elapsed(inst, recording), recording[1],
                paused=self._paused.get(inst["pid"], False),
                in_replay=inst["mode"] != "record"))
            return
        host = self._instance("record")
        if time.monotonic() < busy_until:
            self._paint(context, render.record_key(
                False, in_replay=bool(host and host["mode"] != "record"),
                note="starting…"))
            return
        if host is None and not control.ui_running():
            self._paint(context, render.record_key(
                False, unavailable=True, note="gsr-ui not running"))
            return
        self._paint(context, render.record_key(
            False, in_replay=bool(host and host["mode"] != "record")))

    def _render_stream(self, context, settings, busy_until):
        inst = self._instance("stream")
        if inst is not None:
            self._paint(context, render.stream_key(
                True, config.stream_service(), inst["age"]))
            return
        if time.monotonic() < busy_until:
            self._paint(context, render.stream_key(
                False, "starting…"))
            return
        if not control.ui_running():
            self._paint(context, render.stream_key(
                False, unavailable=True, note="gsr-ui not running"))
            return
        self._paint(context, render.stream_key(False, config.stream_service()))

    def _render_screenshot(self, context, settings):
        area = settings.get("area", "auto")
        if not control.ui_running():
            self._paint(context, render.screenshot_key(
                "", unavailable=True, note="gsr-ui not running"))
            return
        self._paint(context, render.screenshot_key(
            AREA_LABELS.get(area, "full screen"),
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
            lines = ["nothing is recording"]
            if config.replay_seconds():
                lines.append(f"replay {render.duration(config.replay_seconds())}"
                             f" · {config.replay_storage()}")
            if config.stream_service():
                lines.append(f"stream {config.stream_service()}")
            lines.append("gsr-ui up" if control.ui_running()
                         else "gsr-ui down")
            self._paint(context, render.status_key(lines, "IDLE", "idle"))
            return

        headline = {"replay": "REPLAY", "record": "REC",
                    "stream": "LIVE"}.get(inst["mode"], "ON")
        accent = {"replay": "good", "record": "hot",
                  "stream": "hot"}.get(inst["mode"], "live")
        first = {
            "replay": lambda: f"{render.duration(inst['replay_seconds'])} "
                              f"buffer · {inst['replay_storage']}",
            "record": lambda: f"recording {render.clock(inst['age'])}",
            "stream": lambda: f"live {render.clock(inst['age'])}",
        }[inst["mode"]]()
        quality = inst["options"].get("-q", "")
        if inst["bitrate"]:
            quality = f"{inst['bitrate'] / 1000:.0f} Mbps"
        lines = [
            first,
            f"{inst['capture'] or 'unknown'} · {inst['fps'] or '?'} fps",
            f"{inst['codec']}"
            + (f" · {quality}" if quality else ""),
            f"{len(inst['audio'])} audio · "
            f"{render.size(inst['rss_kb'] * 1024)} RAM",
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
        self._drawn.pop(context, None)

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
        self._drawn.pop(context, None)
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
        self._drawn.pop(context, None)

    def _press(self, context):
        settings = self._contexts.get(context) or {}
        action = settings.get("action")
        if action == REPLAY:
            self._press_replay(context, settings)
        elif action == SAVE:
            self._press_save(context, settings)
        elif action == RECORD:
            self._press_record(context, settings)
        elif action == STREAM:
            self._press_stream(context)
        elif action == SCREENSHOT:
            self._press_screenshot(context, settings)
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
        if action == SAVE:
            payload["exact"] = bool(replay
                                    and control.socket_alive(replay["ipc"]))
            payload["lengths"] = [
                {"value": seconds,
                 "label": "Whole buffer" if not seconds
                          else render.duration(seconds)}
                for seconds in SAVE_LENGTHS
            ]
            payload["buffer"] = (replay["replay_seconds"] if replay
                                 else config.replay_seconds())
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
            self._drawn.pop(context, None)
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
            self._drawn.pop(context, None)
            self._flash.pop(context, None)
            self._busy.pop(context, None)
        elif event in ("keyDown", "dialDown", "touchTap"):
            self._press(context)
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
    try:
        Plugin(int(port), uuid, register_event).run()
    except Exception:                              # noqa: BLE001
        log.exception("fatal")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
