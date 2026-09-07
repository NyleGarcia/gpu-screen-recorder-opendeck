"""Reading a recorder's state out of /proc, against a fabricated /proc.

The interesting cases -- a replay with a recording inside it, a stream, a
recorder that has only just opened its output -- are all states that would
otherwise need a GPU, a monitor and several minutes of waiting to produce.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dev.gsr.sdPlugin"))

from gsrdeck import state                            # noqa: E402

REPLAY_ARGV = [
    "gpu-screen-recorder", "-w", "DP-1", "-c", "mp4", "-ac", "opus",
    "-cursor", "yes", "-f", "60", "-r", "60", "-o", "/videos",
    "-replay-storage", "ram", "-bm", "cbr", "-q", "40000",
    "-a", "name:Mic|device:mic.monitor", "-a", "name:Game|device:game.monitor",
    "-ro", "/videos",
]


class FakeProc:
    """A /proc tree with the files state.py actually reads."""

    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.spool = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "uptime"), exist_ok=True)
        shutil.rmtree(os.path.join(self.root, "uptime"))
        with open(os.path.join(self.root, "uptime"), "w") as handle:
            handle.write("100000.00 900000.00\n")

    def add(self, pid, argv, started=99000.0, rss=505272, writing=()):
        base = os.path.join(self.root, str(pid))
        os.makedirs(os.path.join(base, "fd"), exist_ok=True)
        os.makedirs(os.path.join(base, "fdinfo"), exist_ok=True)
        with open(os.path.join(base, "cmdline"), "w") as handle:
            handle.write("\0".join(argv) + "\0")
        # Fields up to the closing bracket are skipped by the parser; what
        # matters is that starttime lands in position 22 overall.
        # /proc/<pid>/stat: field 3 (the state letter) is the first token
        # after the bracketed name, so starttime -- field 22 -- is index 19
        # of what the parser sees. The name contains a space on purpose: a
        # parser splitting from the left gets this wrong.
        fields = ["0"] * 50
        fields[0] = "S"
        fields[19] = str(int(started * state.HZ))
        with open(os.path.join(base, "stat"), "w") as handle:
            handle.write(f"{pid} (gpu-screen reco) " + " ".join(fields))
        with open(os.path.join(base, "status"), "w") as handle:
            handle.write(f"Name:\tgpu-screen-reco\nVmRSS:\t{rss} kB\n")
        for index, (path, contents) in enumerate(writing, start=3):
            with open(path, "w") as handle:
                handle.write(contents)
            os.symlink(path, os.path.join(base, "fd", str(index)))
            with open(os.path.join(base, "fdinfo", str(index)), "w") as h:
                h.write("pos:\t0\nflags:\t0100001\nmnt_id:\t1\n")

    def file(self, name, contents=""):
        return os.path.join(self.spool, name), contents

    def clean(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.spool, ignore_errors=True)


class ParsingTests(unittest.TestCase):
    def test_repeated_audio_options_are_collected(self):
        options = state.parse_argv(REPLAY_ARGV)
        self.assertEqual(len(options["-a"]), 2)
        self.assertEqual(options["-r"], "60")
        self.assertEqual(options["-ro"], "/videos")

    def test_an_option_with_no_value_does_not_eat_the_next_one(self):
        options = state.parse_argv(
            ["gpu-screen-recorder", "-v", "-w", "DP-1"])
        self.assertEqual(options["-v"], "")
        self.assertEqual(options["-w"], "DP-1")

    def test_classification(self):
        self.assertEqual(state.classify({"-r": "60", "-o": "/v"}), "replay")
        self.assertEqual(state.classify({"-o": "rtmp://live/x"}), "stream")
        self.assertEqual(state.classify({"-o": "/v/clip.mp4"}), "record")

    def test_a_streamed_replay_is_still_a_replay(self):
        # The buffer is what a Save Replay key acts on, whatever else the
        # recorder is also doing with the frames.
        self.assertEqual(state.classify({"-r": "60", "-o": "rtmp://x"}),
                         "replay")

    def test_buffer_fill_is_clamped(self):
        self.assertEqual(state.buffer_fill({"replay_seconds": 60,
                                            "age": 30.0}), 0.5)
        self.assertEqual(state.buffer_fill({"replay_seconds": 60,
                                            "age": 6000.0}), 1.0)
        self.assertEqual(state.buffer_fill({"replay_seconds": 0,
                                            "age": 10.0}), 0.0)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.proc = FakeProc()
        self.original = state.PROC
        state.PROC = self.proc.root
        state._first_seen.clear()
        # state.py reads /proc/uptime through the real path, so the fake tree
        # answers only for the per-process files; the boot clock is patched.
        self.original_uptime = state._uptime
        state._uptime = lambda: 100000.0

    def tearDown(self):
        state.PROC = self.original
        state._uptime = self.original_uptime
        self.proc.clean()

    def test_a_replay_is_found_and_described(self):
        self.proc.add(4242, REPLAY_ARGV)
        found = state.instances(now=1000.0)
        self.assertEqual(len(found), 1)
        inst = found[0]
        self.assertEqual(inst["mode"], "replay")
        self.assertEqual(inst["replay_seconds"], 60)
        self.assertEqual(inst["capture"], "DP-1")
        self.assertEqual(inst["fps"], 60)
        self.assertEqual(inst["bitrate"], 40000)
        self.assertEqual(inst["rss_kb"], 505272)
        self.assertEqual(len(inst["audio"]), 2)
        self.assertAlmostEqual(inst["age"], 1000.0, places=1)
        self.assertIsNone(inst["recording"])

    def test_a_process_that_is_not_the_recorder_is_ignored(self):
        self.proc.add(7, ["/usr/bin/gsr-ui", "launch-hide"])
        self.assertEqual(state.instances(now=1000.0), [])

    def test_a_freshly_opened_output_is_a_save_not_a_recording(self):
        target = self.proc.file("Video_2026-09-07_12-00-00.mp4", "x" * 10)
        self.proc.add(4242, REPLAY_ARGV + ["-ro", self.proc.spool],
                      writing=[target])
        first = state.instances(now=1000.0)
        self.assertIsNone(first[0]["recording"],
                          "a descriptor open for no time is not a recording")
        later = state.instances(now=1000.0 + state.RECORDING_SETTLE + 0.1)
        self.assertIsNotNone(later[0]["recording"])
        path, size, _age = later[0]["recording"]
        self.assertTrue(path.endswith("Video_2026-09-07_12-00-00.mp4"))
        self.assertEqual(size, 10)

    def test_a_write_outside_the_output_directory_is_not_a_recording(self):
        # A shader cache is opened for writing and lives nowhere near the
        # videos; treating it as a recording would show REC permanently.
        cache = self.proc.file("shader.bin", "x")
        self.proc.add(4242, REPLAY_ARGV, writing=[cache])
        found = state.instances(now=1000.0 + 10)
        self.assertIsNone(found[0]["recording"])

    def test_an_output_directory_reached_through_a_symlink_still_matches(self):
        # ~/games/videos is a link to /mnt/games/videos on a machine that
        # keeps its videos on another disk. The recorder is given the link and
        # its descriptor reads back as the target, so a plain prefix match
        # shows a running recording as stopped.
        real = os.path.join(self.proc.spool, "real")
        link = os.path.join(self.proc.spool, "link")
        os.makedirs(real, exist_ok=True)
        os.symlink(real, link)
        target = (os.path.join(real, "Video_2026-09-07_12-00-00.mp4"), "xyz")
        self.proc.add(4242, REPLAY_ARGV + ["-ro", link], writing=[target])
        found = state.instances(now=1000.0)
        state.instances(now=1000.0 + state.RECORDING_SETTLE + 0.1)
        found = state.instances(now=1000.0 + state.RECORDING_SETTLE + 0.2)
        self.assertIsNotNone(found[0]["recording"])

    def test_pick_falls_back_to_a_replay_for_recording(self):
        self.proc.add(4242, REPLAY_ARGV)
        found = state.instances(now=1000.0)
        self.assertEqual(state.pick(found, "replay")["pid"], 4242)
        self.assertEqual(state.pick(found, "record")["pid"], 4242)
        self.assertIsNone(state.pick(found, "stream"))


if __name__ == "__main__":
    unittest.main()
