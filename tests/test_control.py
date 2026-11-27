"""The three ways to drive a recorder, and the choice between them.

The IPC tests run against a real unix socket speaking the real protocol: it is
a newline-delimited JSON exchange, which is small enough to stand up in a
thread and is the difference between testing the transport and testing a mock
of it.
"""

import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "systems.garcia.gsr.sdPlugin"))

from gsrdeck import control                          # noqa: E402


class FakeRecorder:
    """A socket that answers like gpu-screen-recorder's -ipc endpoint."""

    def __init__(self, reply=None, listen=True):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "gsr.sock")
        self.requests = []
        self.reply = reply
        self._server = None
        self._thread = None
        if listen:
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(self.path)
            self._server.listen(1)
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()
        else:
            # A socket file left behind by a recorder that was killed. It
            # exists and connecting to it fails, which is exactly the case
            # gsr-cli's "status" command exists to tell apart.
            open(self.path, "w").close()

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                data = b""
                conn.settimeout(1.0)
                try:
                    while b"\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except (OSError, socket.timeout):
                    continue
                if not data.strip():
                    continue
                request = json.loads(data.split(b"\n")[0])
                self.requests.append(request)
                reply = dict(self.reply or {"result": "ok"})
                reply["id"] = request.get("id", 0)
                try:
                    conn.sendall((json.dumps(reply) + "\n").encode())
                except OSError:
                    pass

    def close(self):
        if self._server:
            self._server.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class SocketTests(unittest.TestCase):
    def test_a_reply_carries_the_saved_path(self):
        recorder = FakeRecorder({"result": "ok", "data": "/videos/a.mp4"})
        self.addCleanup(recorder.close)
        result = control.ipc_request(recorder.path, "save-replay",
                                     {"seconds": 30})
        self.assertTrue(result.ok)
        self.assertEqual(result.path, "/videos/a.mp4")
        self.assertEqual(recorder.requests[0]["name"], "save-replay")
        self.assertEqual(recorder.requests[0]["data"], {"seconds": 30})

    def test_an_error_reply_is_not_success(self):
        recorder = FakeRecorder({"result": "error", "data": "no replay"})
        self.addCleanup(recorder.close)
        result = control.ipc_request(recorder.path, "stop")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no replay")

    def test_a_leftover_socket_file_is_not_alive(self):
        recorder = FakeRecorder(listen=False)
        self.addCleanup(recorder.close)
        self.assertFalse(control.socket_alive(recorder.path))

    def test_a_listening_socket_is_alive(self):
        recorder = FakeRecorder()
        self.addCleanup(recorder.close)
        self.assertTrue(control.socket_alive(recorder.path))

    def test_a_missing_socket_is_not_alive(self):
        self.assertFalse(control.socket_alive("/nonexistent/gsr.sock"))


class SignalTests(unittest.TestCase):
    def test_every_preset_maps_to_a_real_time_signal(self):
        for seconds, offset in control.PRESETS.items():
            self.assertGreater(seconds, 0)
            self.assertLessEqual(signal.SIGRTMIN + offset, signal.SIGRTMAX)

    def test_a_length_is_snapped_to_the_nearest_preset(self):
        self.assertEqual(control.snap_seconds(45), 30)
        self.assertEqual(control.snap_seconds(50), 60)
        self.assertEqual(control.snap_seconds(120), 60)
        self.assertEqual(control.snap_seconds(10000), 1800)

    def test_the_whole_buffer_is_not_snapped(self):
        # 0 means "everything", which the plain save signal does exactly;
        # rounding it to ten seconds would be the worst possible answer.
        self.assertEqual(control.snap_seconds(0), control.WHOLE_BUFFER)

    def test_signalling_a_process_that_is_gone_fails_quietly(self):
        self.assertFalse(control.send_signal(0x7FFFFFF0, signal.SIGUSR1))


class CoverTests(unittest.TestCase):
    def test_a_section_is_rounded_up_so_the_mark_stays_in_the_clip(self):
        self.assertEqual(control.cover_seconds(11), 30)
        self.assertEqual(control.cover_seconds(30), 30)
        self.assertEqual(control.cover_seconds(31), 60)

    def test_past_the_longest_preset_only_the_whole_buffer_covers_it(self):
        self.assertEqual(control.cover_seconds(1801), control.WHOLE_BUFFER)
        self.assertEqual(control.cover_seconds(0), control.WHOLE_BUFFER)


class RecentFilesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def touch(self, name, mtime, data=b"x"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        os.utime(path, (mtime, mtime))
        return path

    def test_videos_come_newest_first_and_nothing_else_does(self):
        old = self.touch("Replay_a.mp4", 100)
        new = self.touch("Replay_b.mkv", 200, b"xyz")
        self.touch("Screenshot.png", 300)
        self.touch(".Replay_c.mp4", 400)
        found = control.recent_files(self.dir)
        self.assertEqual([entry[0] for entry in found], [new, old])
        self.assertEqual(found[0][2], 3)

    def test_the_list_is_capped(self):
        for index in range(5):
            self.touch(f"Replay_{index}.mp4", 100 + index)
        self.assertEqual(len(control.recent_files(self.dir, limit=3)), 3)

    def test_an_unreadable_directory_has_no_saves(self):
        self.assertEqual(control.recent_files(self.dir + "/missing"), [])

    def test_opening_a_file_that_is_gone_fails_cleanly(self):
        result = control.open_file(os.path.join(self.dir, "gone.mp4"))
        self.assertFalse(result.ok)


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, name, when=None):
        path = os.path.join(self.dir, name)
        with open(path, "w") as handle:
            handle.write("x")
        if when is not None:
            os.utime(path, (when, when))
        return path

    def test_the_newest_file_wins(self):
        self._write("old.mp4", when=1000)
        newer = self._write("new.mp4", when=2000)
        path, when = control.newest_file(self.dir)
        self.assertEqual(path, newer)
        self.assertEqual(when, 2000)

    def test_an_unreadable_directory_is_empty_not_an_error(self):
        self.assertEqual(control.newest_file("/nonexistent"), (None, 0.0))

    def test_waiting_returns_the_file_that_appeared(self):
        _, before = control.newest_file(self.dir)
        expected = []

        def write_soon():
            time.sleep(0.2)
            expected.append(self._write("Replay_1.mp4",
                                        when=time.time() + 10))

        threading.Thread(target=write_soon, daemon=True).start()
        found = control.await_new_file(self.dir, before, deadline=5.0)
        self.assertEqual(found, expected[0])

    def test_waiting_gives_up_rather_than_inventing_a_file(self):
        newest = self._write("already-here.mp4")
        _, before = control.newest_file(self.dir)
        self.assertEqual(control.await_new_file(self.dir, before,
                                                deadline=0.5), "")
        self.assertTrue(os.path.exists(newest))


class UiCliTests(unittest.TestCase):
    """Where gsr-ui-cli is found: on PATH, or inside the Flathub app."""

    def setUp(self):
        self.bin = tempfile.mkdtemp()
        self.apps = tempfile.mkdtemp()
        self.saved = (os.environ.get("PATH"), control._FLATPAK_APP_DIRS,
                      control._ui_cli_command)
        os.environ["PATH"] = self.bin
        control._FLATPAK_APP_DIRS = (self.apps,)
        control._ui_cli_command = None

    def tearDown(self):
        path, control._FLATPAK_APP_DIRS, control._ui_cli_command = self.saved
        os.environ["PATH"] = path or ""
        shutil.rmtree(self.bin)
        shutil.rmtree(self.apps)

    def _tool(self, name):
        path = os.path.join(self.bin, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(path, 0o755)
        return path

    def test_a_cli_on_path_is_used_directly(self):
        cli = self._tool("gsr-ui-cli")
        self._tool("flatpak")
        os.mkdir(os.path.join(self.apps, control.FLATPAK_APP))
        self.assertEqual(control._find_ui_cli(), [cli])

    def test_the_flatpak_app_is_used_when_nothing_is_on_path(self):
        flatpak = self._tool("flatpak")
        os.mkdir(os.path.join(self.apps, control.FLATPAK_APP))
        self.assertEqual(control._find_ui_cli(), [
            flatpak, "run", "--command=gsr-ui-cli", control.FLATPAK_APP])
        self.assertTrue(control.ui_cli_available())

    def test_flatpak_without_the_app_is_not_a_cli(self):
        self._tool("flatpak")
        self.assertEqual(control._find_ui_cli(), [])
        self.assertFalse(control.ui_cli_available())


class VerbTests(unittest.TestCase):
    def test_saving_with_no_replay_running_is_a_clean_failure(self):
        result = control.save_replay(None)
        self.assertFalse(result.ok)
        self.assertIn("replay", result.error)

    def test_recording_needs_an_output_directory(self):
        result = control.toggle_recording({"pid": 1, "record_output": "",
                                           "ipc": ""})
        self.assertFalse(result.ok)
        self.assertIn("-ro", result.error)

    def test_a_socket_is_preferred_over_a_signal(self):
        recorder = FakeRecorder({"result": "ok", "data": "/videos/b.mp4"})
        self.addCleanup(recorder.close)
        # pid 1 is init: were this to fall through to signalling, the test
        # would fail to signal rather than silently signal something else.
        result = control.save_replay(
            {"pid": 1, "ipc": recorder.path, "output": "/videos"}, 45)
        self.assertTrue(result.ok)
        self.assertEqual(result.via, "ipc")
        self.assertEqual(recorder.requests[0]["data"]["seconds"], 45,
                         "a socket saves the exact length asked for")


if __name__ == "__main__":
    unittest.main()
