"""The key art.

Keys are SVG generated per frame, so the thing worth testing is that every
state produces a well-formed document with the text a person needs on it --
a key that renders as nothing looks identical to a key that is switched off.
"""

import os
import sys
import unittest
import xml.etree.ElementTree as ElementTree

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dev.gsr.sdPlugin"))

from gsrdeck import render                           # noqa: E402


def text_of(svg):
    root = ElementTree.fromstring(svg)
    return " ".join(node.text or "" for node in root.iter()
                    if node.tag.endswith("text"))


class DocumentTests(unittest.TestCase):
    def setUp(self):
        render.set_theme(render.DEFAULT_THEME)

    def test_every_key_is_parseable_svg(self):
        for name, svg in {
            "replay on": render.replay_key(True, 60, 0.5, 30),
            "replay off": render.replay_key(False, 60),
            "replay unavailable": render.replay_key(False, 0,
                                                    unavailable=True),
            "save": render.save_key("ALL", True),
            "save pending": render.save_key("30s", True, pending=True),
            "save flash": render.save_key("30s", True,
                                          flash="/v/Replay_x_12-00-00.mp4"),
            "save off": render.save_key("ALL", False),
            "record": render.record_key(True, 61, 1024),
            "record paused": render.record_key(True, 61, 1024, paused=True),
            "record off": render.record_key(False),
            "stream": render.stream_key(True, "twitch", 61),
            "stream off": render.stream_key(False, "twitch"),
            "screenshot": render.screenshot_key("region"),
            "status": render.status_key(["a", "b", "c", "d"], "REC", "hot"),
        }.items():
            with self.subTest(name):
                root = ElementTree.fromstring(svg)
                self.assertEqual(root.get("width"), str(render.SIZE))

    def test_a_name_with_markup_in_it_cannot_break_the_document(self):
        svg = render.stream_key(True, "a<b>&c", 10)
        self.assertIn("a&lt;b&gt;&amp;c", svg)
        ElementTree.fromstring(svg)

    def test_a_running_replay_says_how_long_the_buffer_is(self):
        self.assertIn("1m", text_of(render.replay_key(True, 60, 1.0, 300)))

    def test_a_filling_buffer_shows_how_far_in_it_is(self):
        filling = text_of(render.replay_key(True, 60, 0.5, 30))
        self.assertIn("00:30 in", filling)
        full = text_of(render.replay_key(True, 60, 1.0, 600))
        self.assertIn("up 10:00", full)

    def test_a_recording_shows_its_clock_and_size(self):
        drawn = text_of(render.record_key(True, 3725, 1234567890))
        self.assertIn("1:02:05", drawn)
        self.assertIn("1.1GB", drawn)
        self.assertIn("REC", drawn)

    def test_a_paused_recording_says_so(self):
        self.assertIn("PAUSED", text_of(render.record_key(True, 10, 5,
                                                          paused=True)))

    def test_every_theme_draws(self):
        for name in render.THEMES:
            with self.subTest(name):
                render.set_theme(name)
                ElementTree.fromstring(render.replay_key(True, 60, 1.0, 90))

    def test_an_unknown_theme_falls_back_rather_than_failing(self):
        render.set_theme("no such theme")
        self.assertEqual(render.THEME, render.THEMES[render.DEFAULT_THEME])


class FormattingTests(unittest.TestCase):
    def test_clock_grows_an_hour_field_only_when_needed(self):
        self.assertEqual(render.clock(0), "00:00")
        self.assertEqual(render.clock(59), "00:59")
        self.assertEqual(render.clock(3599), "59:59")
        self.assertEqual(render.clock(3600), "1:00:00")

    def test_durations_read_the_way_people_say_them(self):
        self.assertEqual(render.duration(30), "30s")
        self.assertEqual(render.duration(60), "1m")
        self.assertEqual(render.duration(90), "1m30")
        self.assertEqual(render.duration(1800), "30m")
        self.assertEqual(render.duration(3600), "1h")

    def test_sizes_stay_under_four_digits(self):
        self.assertEqual(render.size(0), "0B")
        self.assertEqual(render.size(2048), "2.0KB")
        self.assertEqual(render.size(1024 * 1024 * 512), "512MB")

    def test_a_saved_filename_is_split_where_it_can_be_read(self):
        # "Replay_2026-09-07_12-06-08" truncated to one line says nothing;
        # the kind and the time are what tell two saves apart.
        self.assertEqual(
            render.file_label("/v/Replay_2026-09-07_12-06-08.mp4"),
            ["Replay", "12:06:08"])

    def test_a_name_without_the_usual_shape_still_gets_lines(self):
        self.assertEqual(render.file_label("/v/clip.mp4"), ["clip"])
        self.assertEqual(render.file_label(""), [""])


if __name__ == "__main__":
    unittest.main()
