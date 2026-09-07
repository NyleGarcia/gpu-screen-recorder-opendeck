"""The plugin's own logic, driven through the events OpenDeck actually sends.

The WebSocket is replaced with something that records what was sent, and the
recorder scan with a fixed answer, so a whole key -- appear, draw, press,
finish -- can be exercised without a deck, a GPU or a recorder.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dev.gsr.sdPlugin"))

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
        self.assertEqual(gsr_plugin.save_length({"seconds": 45}), 0)
        self.assertEqual(gsr_plugin.save_length({}), 0)
        self.assertEqual(gsr_plugin.save_length({"seconds": "nonsense"}), 0)

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
