"""Where GPU Screen Recorder's config is read from."""

import os
import shutil
import sys
import tempfile
import unittest

BUNDLE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "systems.garcia.gsr.sdPlugin")
sys.path.insert(0, BUNDLE)

from gsrdeck import config                           # noqa: E402


class ConfigLocationTests(unittest.TestCase):
    """A package install, the Flathub app, both, or neither."""

    def setUp(self):
        self.native = tempfile.mkdtemp()
        self.flatpak = tempfile.mkdtemp()
        self.saved = config.CONFIG_DIRS
        config.CONFIG_DIRS = (self.native, self.flatpak)
        self._forget()

    def tearDown(self):
        config.CONFIG_DIRS = self.saved
        self._forget()
        shutil.rmtree(self.native)
        shutil.rmtree(self.flatpak)

    def _forget(self):
        config._cache.clear()
        config._cache_at.clear()

    def _write(self, directory, name, text, written_at):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.utime(path, (written_at, written_at))

    def test_a_package_install_is_read(self):
        self._write(self.native, "config_ui", "replay.replay_time 60\n", 100)
        self.assertEqual(config.get("replay.replay_time"), "60")

    def test_the_flathub_app_is_read_when_it_is_the_only_install(self):
        self._write(self.flatpak, "config_ui", "replay.replay_time 90\n", 100)
        self.assertEqual(config.get("replay.replay_time"), "90")

    def test_with_both_the_one_written_last_wins(self):
        self._write(self.native, "config_ui", "replay.replay_time 60\n", 100)
        self._write(self.flatpak, "config_ui", "replay.replay_time 90\n", 200)
        self.assertEqual(config.get("replay.replay_time"), "90")
        self._forget()
        self._write(self.native, "config_ui", "replay.replay_time 30\n", 300)
        self.assertEqual(config.get("replay.replay_time"), "30")

    def test_each_file_is_located_on_its_own(self):
        self._write(self.native, "config", "record.save_directory /a\n", 100)
        self._write(self.flatpak, "config_ui", "replay.replay_time 90\n", 100)
        self.assertEqual(config.get("record.save_directory"), "/a")
        self.assertEqual(config.get("replay.replay_time"), "90")

    def test_no_config_anywhere_is_the_default(self):
        self.assertEqual(config.get("replay.replay_time", "x"), "x")


if __name__ == "__main__":
    unittest.main()
