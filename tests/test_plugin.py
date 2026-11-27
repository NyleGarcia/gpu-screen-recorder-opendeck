"""The plugin's own logic, driven through the events OpenDeck actually sends.

The WebSocket is replaced with something that records what was sent, and the
recorder scan with a fixed answer, so a whole key -- appear, draw, press,
finish -- can be exercised without a deck, a GPU or a recorder.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "systems.garcia.gsr.sdPlugin"))

import plugin as gsr_plugin                          # noqa: E402
from gsrdeck import control                          # noqa: E402


class FakeSocket:
    def __init__(self, *args, **kwargs):
        self.sent = []

    def send_json(self, payload):
        self.sent.append(payload)

    def events(self, name):
        return [message for message in self.sent
                if message.get("event") == name]


REPLAY = {
    "pid": 4242, "mode": "replay", "options": {"-q": "40000"}, "ipc": "",
    "replay_seconds": 60, "replay_storage": "ram", "output": "/videos",
    "record_output": "/videos", "capture": "DP-1", "fps": 60,
    "codec": "auto", "container": "mp4", "bitrate": 40000,
    "bitrate_mode": "cbr", "audio": ["a", "b", "c"], "age": 900.0,
    "rss_kb": 505272, "recording": None, "outputs": [],
}


def with_recording(inst, path="/videos/Video_2026-09-07_12-00-00.mp4",
                   size=1024, age=30.0):
    copied = dict(inst)
    copied["recording"] = (path, size, age)
    return copied


class PluginTestCase(unittest.TestCase):
    def setUp(self):
        self.socket = FakeSocket()
        self.original_ws = gsr_plugin.WebSocket
        gsr_plugin.WebSocket = lambda *a, **k: self.socket
        self.addCleanup(setattr, gsr_plugin, "WebSocket", self.original_ws)
        self.plugin = gsr_plugin.Plugin(1234, "uuid", "registerPlugin")
        self.instances = []
        self.plugin._scan = lambda force=False: self.instances
        self.addCleanup(gsr_plugin.render.set_theme,
                        gsr_plugin.render.DEFAULT_THEME)
        # Tests replace pieces of control to keep signals and subprocesses
        # out of the suite. The module is shared, so what they replaced is
        # put back rather than leaking into whichever test runs next.
        for name in ("save_replay", "toggle_recording", "toggle_pause",
                     "stop", "ui_cli", "ui_running", "socket_alive"):
            self.addCleanup(setattr, control, name, getattr(control, name))

    def appear(self, action, settings=None):
        context = f"ctx-{action}"
        self.plugin._handle({
            "event": "willAppear", "context": context, "action": action,
            "payload": {"settings": settings or {}, "controller": "Keypad"},
        })
        return context

    def last_image(self, context):
        for message in reversed(self.socket.sent):
            if message.get("event") == "setImage" \
                    and message.get("context") == context:
                return message["payload"]["image"]
        return ""

    def drawn_text(self, context):
        import base64
        import xml.etree.ElementTree as ElementTree
        uri = self.last_image(context)
        self.assertTrue(uri, "the key was never drawn")
        svg = base64.b64decode(uri.split(",", 1)[1]).decode()
        root = ElementTree.fromstring(svg)
        return " ".join(node.text or "" for node in root.iter()
                        if node.tag.endswith("text"))


class SettingsTests(unittest.TestCase):
    def test_a_save_length_outside_the_offered_set_becomes_the_whole_buffer(self):
        self.assertEqual(gsr_plugin.save_length({"seconds": 30}), 30)
        self.assertEqual(gsr_plugin.save_length({"seconds": "600"}), 600)
        self.assertEqual(gsr_plugin.save_length({"seconds": 45}), 45)
        self.assertEqual(gsr_plugin.save_length({"seconds": 47}), 0)
        self.assertEqual(gsr_plugin.save_length({}), 0)
        self.assertEqual(gsr_plugin.save_length({"seconds": "nonsense"}), 0)

    def test_a_dial_offers_only_what_the_transport_can_save(self):
        self.assertEqual(gsr_plugin.dial_lengths(120, exact=False),
                         [10, 30, 60, 0])
        exact = gsr_plugin.dial_lengths(120, exact=True)
        self.assertEqual(exact[-1], 0)
        self.assertIn(45, exact)
        self.assertTrue(all(seconds < 120 for seconds in exact[:-1]))

    def test_a_section_step_outside_the_offered_set_is_the_default(self):
        self.assertEqual(gsr_plugin.section_step({"step": 10}), 10)
        self.assertEqual(gsr_plugin.section_step({"step": 7}),
                         gsr_plugin.SECTION_STEP)

    def test_a_rounded_length_is_marked_on_the_key(self):
        self.assertEqual(gsr_plugin.length_label(0), "ALL")
        self.assertEqual(gsr_plugin.length_label(30, exact=True), "30s")
        self.assertEqual(gsr_plugin.length_label(30, exact=False), "30s*")

    def test_a_recordings_start_time_comes_from_its_name(self):
        stamp = gsr_plugin.file_started_at("/v/Video_2026-09-07_12-06-08.mp4")
        self.assertIsNotNone(stamp)
        import time
        self.assertEqual(time.localtime(stamp).tm_hour, 12)
        self.assertEqual(time.localtime(stamp).tm_min, 6)

    def test_a_name_with_no_timestamp_has_no_start_time(self):
        self.assertIsNone(gsr_plugin.file_started_at("/v/clip.mp4"))
        self.assertIsNone(gsr_plugin.file_started_at(""))


class DrawingTests(PluginTestCase):
    def test_a_replay_key_draws_the_running_buffer(self):
        self.instances = [REPLAY]
        context = self.appear(gsr_plugin.REPLAY)
        self.assertIn("ON", self.drawn_text(context))
        self.assertIn("1m", self.drawn_text(context))

    def test_a_replay_key_with_nothing_running_offers_the_configured_length(self):
        self.instances = []
        gsr_plugin.control.ui_running = lambda: True
        context = self.appear(gsr_plugin.REPLAY)
        self.assertIn("OFF", self.drawn_text(context))

    def test_a_key_is_not_redrawn_when_nothing_changed(self):
        # The deck repaints on receipt, so a key that resends an identical
        # image every second visibly flickers.
        self.instances = [REPLAY]
        context = self.appear(gsr_plugin.REPLAY)
        before = len(self.socket.events("setImage"))
        self.plugin._render(context)
        self.plugin._render(context)
        self.assertEqual(len(self.socket.events("setImage")), before)

    def test_a_record_key_shows_a_recording_inside_a_replay(self):
        self.instances = [with_recording(REPLAY)]
        context = self.appear(gsr_plugin.RECORD)
        drawn = self.drawn_text(context)
        self.assertIn("REC", drawn)
        self.assertIn("in replay", drawn)

    def test_a_save_key_marks_a_length_a_signal_will_round(self):
        self.instances = [REPLAY]                  # no -ipc socket
        context = self.appear(gsr_plugin.SAVE, {"seconds": 600})
        self.assertIn("10m*", self.drawn_text(context))

    def test_a_save_key_with_no_replay_says_the_replay_is_off(self):
        self.instances = []
        context = self.appear(gsr_plugin.SAVE, {"seconds": 0})
        self.assertIn("replay is off", self.drawn_text(context))

    def test_a_status_key_reports_the_capture_and_the_bitrate(self):
        self.instances = [REPLAY]
        context = self.appear(gsr_plugin.STATUS, {"source": "auto"})
        drawn = self.drawn_text(context)
        self.assertIn("DP-1", drawn)
        self.assertIn("40 Mbps", drawn)
        self.assertIn("3 audio", drawn)

    def test_a_status_key_asked_for_a_stream_does_not_show_the_replay(self):
        self.instances = [REPLAY]
        context = self.appear(gsr_plugin.STATUS, {"source": "stream"})
        self.assertIn("nothing is recording", self.drawn_text(context))


class PressTests(PluginTestCase):
    def press(self, context):
        self.plugin._handle({"event": "keyDown", "context": context,
                             "payload": {}})

    def test_a_status_key_does_nothing_when_pressed(self):
        self.instances = [REPLAY]
        context = self.appear(gsr_plugin.STATUS)
        before = list(self.socket.sent)
        self.press(context)
        self.assertEqual(self.socket.sent, before)

    def test_a_save_with_no_replay_running_alerts_rather_than_acting(self):
        self.instances = []
        context = self.appear(gsr_plugin.SAVE)
        self.press(context)
        self.assertTrue(self.socket.events("showAlert"))

    def test_a_press_that_starts_work_does_not_block_the_loop(self):
        # The work is handed to a thread and its result collected later; a
        # thirty minute save must not stop the deck answering OpenDeck.
        self.instances = [REPLAY]
        context = self.appear(gsr_plugin.SAVE)
        held = []
        gsr_plugin.control.save_replay = lambda *a, **k: held.append(a) or \
            control.Result(True, "signal", path="/videos/Replay_x.mp4")
        self.press(context)
        self.assertIn(context, self.plugin._pending)
        for _ in range(50):
            self.plugin._drain_jobs()
            if context not in self.plugin._pending:
                break
            import time
            time.sleep(0.02)
        self.assertNotIn(context, self.plugin._pending)
        self.assertTrue(self.socket.events("showOk"))
        self.assertIn("SAVED", self.drawn_text(context))

    def test_a_recording_inside_a_replay_is_stopped_in_place(self):
        # Stopping the recorder itself would take the replay buffer down
        # with it, which is not what "stop recording" means to anyone.
        self.instances = [with_recording(REPLAY)]
        context = self.appear(gsr_plugin.RECORD)
        called = []
        gsr_plugin.control.toggle_recording = lambda inst: \
            called.append(inst) or control.Result(True, "signal")
        gsr_plugin.control.stop = lambda inst: \
            self.fail("the whole recorder must not be stopped")
        self.press(context)
        for _ in range(50):
            self.plugin._drain_jobs()
            if context not in self.plugin._pending:
                break
            import time
            time.sleep(0.02)
        self.assertEqual(len(called), 1)

    def test_a_replay_key_set_to_start_only_does_not_stop_a_running_one(self):
        self.instances = [REPLAY]
        context = self.appear(gsr_plugin.REPLAY, {"press": "start"})
        gsr_plugin.control.ui_cli = lambda command: \
            self.fail("a start-only key must not stop the replay")
        self.press(context)
        self.assertTrue(self.socket.events("showOk"))


class ThemeTests(PluginTestCase):
    def test_a_theme_change_redraws_every_key(self):
        self.instances = [REPLAY]
        context = self.appear(gsr_plugin.REPLAY)
        before = self.last_image(context)
        self.plugin._handle({
            "event": "didReceiveGlobalSettings", "context": "uuid",
            "payload": {"settings": {"theme": "light"}},
        })
        self.assertNotEqual(self.last_image(context), before)

    def test_an_unknown_theme_is_ignored(self):
        self.plugin._handle({
            "event": "didReceiveGlobalSettings", "context": "uuid",
            "payload": {"settings": {"theme": "chartreuse"}},
        })
        self.assertEqual(self.plugin._theme, gsr_plugin.render.DEFAULT_THEME)


class DialTests(PluginTestCase):
    def appear_dial(self, action, settings=None):
        context = f"dial-{action}"
        self.plugin._handle({
            "event": "willAppear", "context": context, "action": action,
            "payload": {"settings": settings or {}, "controller": "Encoder"},
        })
        return context

    def send(self, event, context, **payload):
        self.plugin._handle({"event": event, "context": context,
                             "payload": payload})

    def strip_text(self, context):
        import base64
        import xml.etree.ElementTree as ElementTree
        for message in reversed(self.socket.sent):
            if message.get("event") == "setFeedback" \
                    and message.get("context") == context:
                uri = message["payload"][gsr_plugin.CANVAS]
                root = ElementTree.fromstring(
                    base64.b64decode(uri.split(",", 1)[1]).decode())
                return " ".join(node.text or "" for node in root.iter()
                                if node.tag.endswith("text"))
        self.fail("the strip was never drawn")

    def finish_jobs(self):
        import time
        for _ in range(50):
            self.plugin._drain_jobs()
            if not self.plugin._pending:
                return
            time.sleep(0.02)

    def test_a_dial_gets_its_layout_and_a_strip(self):
        self.instances = [REPLAY]
        context = self.appear_dial(gsr_plugin.REPLAY)
        layouts = self.socket.events("setFeedbackLayout")
        self.assertEqual(layouts[-1]["payload"]["layout"],
                         gsr_plugin.ENCODER_LAYOUT)
        self.assertIn("ON", self.strip_text(context))
        # The key image too, for OpenDeck's editor.
        self.assertTrue(self.last_image(context))

    def test_a_key_gets_no_strip(self):
        self.instances = [REPLAY]
        self.appear(gsr_plugin.REPLAY)
        self.assertFalse(self.socket.events("setFeedback"))
        self.assertFalse(self.socket.events("setFeedbackLayout"))

    def test_turning_a_save_dial_steps_the_length_and_keeps_it(self):
        self.instances = [REPLAY]                  # 60s buffer, no socket
        context = self.appear_dial(gsr_plugin.SAVE, {"seconds": 10})
        self.send("dialRotate", context, ticks=1)
        self.assertEqual(self.plugin._contexts[context]["seconds"], 30)
        stored = self.socket.events("setSettings")[-1]["payload"]
        self.assertEqual(stored, {"seconds": 30})
        self.assertIn("30s", self.strip_text(context))
        self.send("dialRotate", context, ticks=5)
        self.assertEqual(self.plugin._contexts[context]["seconds"], 0)
        self.assertIn("ALL", self.strip_text(context))
        self.send("dialRotate", context, ticks=-9)
        self.assertEqual(self.plugin._contexts[context]["seconds"], 10)

    def test_pressing_a_save_dial_saves_what_it_shows(self):
        self.instances = [REPLAY]
        context = self.appear_dial(gsr_plugin.SAVE, {"seconds": 30})
        asked = []
        gsr_plugin.control.save_replay = lambda inst, seconds, restart: \
            asked.append(seconds) or control.Result(True, "signal")
        self.send("dialDown", context)
        self.finish_jobs()
        self.assertEqual(asked, [30])

    def test_a_section_is_marked_then_saved_rounded_up(self):
        self.instances = [REPLAY]                  # no socket: presets
        context = self.appear_dial(gsr_plugin.SECTION)
        self.assertIn("no mark", self.strip_text(context))
        self.send("dialDown", context)
        self.assertIn(context, self.plugin._marks)
        pid, _ = self.plugin._marks[context]
        # Pretend the mark was dropped 12 seconds ago.
        import time
        self.plugin._marks[context] = (pid, time.monotonic() - 12)
        self.plugin._render(context)
        self.assertIn("30s*", self.strip_text(context))
        asked = []
        gsr_plugin.control.save_replay = lambda inst, seconds: \
            asked.append(seconds) or control.Result(True, "signal")
        self.send("dialDown", context)
        self.finish_jobs()
        self.assertEqual(asked, [30])
        self.assertNotIn(context, self.plugin._marks)

    def test_a_section_over_a_socket_saves_to_the_second(self):
        self.instances = [dict(REPLAY, ipc="/tmp/sock")]
        gsr_plugin.control.socket_alive = lambda path: True
        context = self.appear_dial(gsr_plugin.SECTION)
        self.send("dialRotate", context, ticks=-3)  # mark 15s back
        asked = []
        gsr_plugin.control.save_replay = lambda inst, seconds: \
            asked.append(seconds) or control.Result(True, "ipc")
        self.send("dialDown", context)
        self.finish_jobs()
        self.assertEqual(len(asked), 1)
        self.assertIn(asked[0], (15, 16))

    def test_a_section_cannot_reach_past_the_buffer(self):
        self.instances = [REPLAY]                  # 60s buffer
        context = self.appear_dial(gsr_plugin.SECTION, {"step": 30})
        self.send("dialRotate", context, ticks=-10)
        section = self.plugin._section(context, REPLAY)
        self.assertTrue(section["capped"])
        self.assertEqual(section["seconds"], 0)
        self.assertIn("buffer limit", self.strip_text(context))

    def test_turning_a_section_right_past_now_drops_the_mark(self):
        self.instances = [REPLAY]
        context = self.appear_dial(gsr_plugin.SECTION)
        self.send("dialRotate", context, ticks=-1)
        self.assertIn(context, self.plugin._marks)
        self.send("dialRotate", context, ticks=2)
        self.assertNotIn(context, self.plugin._marks)

    def test_a_tap_cancels_a_section_mark(self):
        self.instances = [REPLAY]
        context = self.appear_dial(gsr_plugin.SECTION)
        self.send("dialDown", context)
        self.send("touchTap", context)
        self.assertNotIn(context, self.plugin._marks)

    def test_a_mark_from_a_replay_since_restarted_is_dropped(self):
        self.instances = [REPLAY]
        context = self.appear_dial(gsr_plugin.SECTION)
        self.send("dialDown", context)
        self.instances = [dict(REPLAY, pid=9999)]
        self.plugin._render(context)
        self.assertNotIn(context, self.plugin._marks)

    def test_a_record_dial_pauses_left_and_resumes_right_once_each(self):
        self.instances = [with_recording(REPLAY)]
        context = self.appear_dial(gsr_plugin.RECORD)
        calls = []
        gsr_plugin.control.toggle_pause = lambda inst: \
            calls.append(inst["pid"]) or control.Result(True, "signal")
        self.send("dialRotate", context, ticks=-1)
        self.finish_jobs()
        self.assertTrue(self.plugin._paused[REPLAY["pid"]])
        self.assertIn("PAUSED", self.strip_text(context))
        self.send("dialRotate", context, ticks=-2)  # already paused
        self.finish_jobs()
        self.send("dialRotate", context, ticks=1)
        self.finish_jobs()
        self.assertFalse(self.plugin._paused[REPLAY["pid"]])
        self.assertEqual(len(calls), 2)

    def test_a_record_dial_press_stops_even_if_the_key_is_set_to_pause(self):
        self.instances = [with_recording(REPLAY)]
        context = self.appear_dial(gsr_plugin.RECORD, {"press": "pause"})
        toggled, paused = [], []
        gsr_plugin.control.toggle_recording = lambda inst: \
            toggled.append(1) or control.Result(True, "signal")
        gsr_plugin.control.toggle_pause = lambda inst: \
            paused.append(1) or control.Result(True, "signal")
        self.send("dialDown", context)
        self.finish_jobs()
        self.assertEqual((toggled, paused), ([1], []))

    def test_a_replay_dial_browses_saves_and_a_tap_opens_one(self):
        import time
        now = time.time()
        saves = [("/videos/Replay_2026-09-07_12-00-02.mp4", now - 30, 10),
                 ("/videos/Replay_2026-09-07_11-00-00.mp4", now - 3600, 20)]
        gsr_plugin.control.recent_files = lambda directory, limit=20: saves
        self.addCleanup(setattr, gsr_plugin.control, "recent_files",
                        control.recent_files)
        self.instances = [REPLAY]
        context = self.appear_dial(gsr_plugin.REPLAY)
        self.assertIn("saved 30s ago", self.strip_text(context))
        self.send("dialRotate", context, ticks=1)
        self.assertIn("1/2", self.strip_text(context))
        self.send("dialRotate", context, ticks=1)
        self.assertIn("2/2", self.strip_text(context))
        opened = []
        original = gsr_plugin.control.open_file
        self.addCleanup(setattr, gsr_plugin.control, "open_file", original)
        gsr_plugin.control.open_file = lambda path: \
            opened.append(path) or control.Result(True, "open")
        self.send("touchTap", context)
        self.finish_jobs()
        self.assertEqual(opened, [saves[1][0]])
        self.assertIn("ON", self.strip_text(context))

    def test_turning_a_replay_dial_back_past_the_newest_leaves_browsing(self):
        import time
        saves = [("/videos/Replay_a_12-00-00.mp4", time.time() - 5, 1)]
        gsr_plugin.control.recent_files = lambda directory, limit=20: saves
        self.addCleanup(setattr, gsr_plugin.control, "recent_files",
                        control.recent_files)
        self.instances = [REPLAY]
        context = self.appear_dial(gsr_plugin.REPLAY)
        self.send("dialRotate", context, ticks=1)
        self.assertIn(context, self.plugin._browse)
        self.send("dialRotate", context, ticks=-1)
        self.assertNotIn(context, self.plugin._browse)


class InspectorTests(PluginTestCase):
    def test_the_panel_is_answered_when_it_appears(self):
        self.instances = [REPLAY]
        context = self.appear(gsr_plugin.SAVE)
        self.plugin._handle({"event": "propertyInspectorDidAppear",
                             "context": context, "action": gsr_plugin.SAVE})
        replies = self.socket.events("sendToPropertyInspector")
        self.assertTrue(replies)
        payload = replies[-1]["payload"]
        self.assertFalse(payload["exact"], "no socket means rounded lengths")
        self.assertEqual(payload["buffer"], 60)
        self.assertEqual(payload["running"][0]["mode"], "replay")

    def test_the_offered_lengths_are_the_ones_the_key_accepts(self):
        self.instances = [REPLAY]
        context = self.appear(gsr_plugin.SAVE)
        self.plugin._handle({"event": "propertyInspectorDidAppear",
                             "context": context, "action": gsr_plugin.SAVE})
        payload = self.socket.events("sendToPropertyInspector")[-1]["payload"]
        offered = [length["value"] for length in payload["lengths"]]
        self.assertEqual(offered, gsr_plugin.SAVE_LENGTHS)


if __name__ == "__main__":
    unittest.main()
