"""Translations: which file a language code picks, and that nothing is missing."""

import glob
import json
import os
import re
import sys
import unittest

BUNDLE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "systems.garcia.gsr.sdPlugin")
sys.path.insert(0, BUNDLE)

from gsrdeck import i18n, render                     # noqa: E402

import plugin                                        # noqa: E402


def english():
    with open(os.path.join(BUNDLE, "lang", "en.json"),
              encoding="utf-8") as handle:
        return json.load(handle)["Localization"]


class ResolveTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_every_spelling_of_french_picks_french(self):
        for code in ("fr", "fr_FR", "fr-FR", "fr_FR.UTF-8", "FR"):
            self.assertEqual(i18n.resolve(code), "fr", code)

    def test_an_unknown_or_missing_language_is_english(self):
        for code in ("xx", "", None, "C.UTF-8"):
            self.assertEqual(i18n.resolve(code), "en", code)

    def test_strings_follow_the_language(self):
        i18n.set_language("fr")
        self.assertEqual(i18n.t("key.save.saving"), "Écriture…")
        i18n.set_language("en")
        self.assertEqual(i18n.t("key.save.saving"), "Saving…")

    def test_placeholders_are_filled(self):
        i18n.set_language("fr")
        self.assertEqual(i18n.t("key.replay.up", clock="01:00"),
                         "actif 01:00")

    def test_a_missing_key_shows_itself_or_its_default(self):
        self.assertEqual(i18n.t("no.such.key"), "no.such.key")
        self.assertEqual(i18n.t("no.such.key", default="x"), "x")

    def test_the_inspector_table_holds_only_inspector_strings(self):
        table = i18n.table("pi.")
        self.assertIn("pi.colours", table)
        self.assertFalse([key for key in table if not key.startswith("pi.")])

    def test_theme_names_are_translated(self):
        i18n.set_language("fr")
        labels = {choice["id"]: choice["label"]
                  for choice in render.theme_choices()}
        self.assertEqual(labels["light"], "Clair")


class LanguageFromOpenDeckTests(unittest.TestCase):
    def test_the_language_comes_from_the_info_argument(self):
        info = json.dumps({"application": {"language": "fr"}})
        self.assertEqual(plugin.opendeck_language(info), "fr")

    def test_bad_info_falls_back_to_the_environment(self):
        saved = os.environ.get("LC_ALL")
        os.environ["LC_ALL"] = "fr_FR.UTF-8"
        try:
            self.assertEqual(plugin.opendeck_language("not json"),
                             "fr_FR.UTF-8")
        finally:
            if saved is None:
                del os.environ["LC_ALL"]
            else:
                os.environ["LC_ALL"] = saved


class CoverageTests(unittest.TestCase):
    """Every key the code asks for exists in English."""

    def _missing(self, paths, pattern):
        known = english()
        missing = set()
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                for key in re.findall(pattern, handle.read()):
                    if key not in known:
                        missing.add((os.path.basename(path), key))
        return sorted(missing)

    def test_python_keys_exist(self):
        paths = [os.path.join(BUNDLE, "plugin.py")] + glob.glob(
            os.path.join(BUNDLE, "gsrdeck", "*.py"))
        # Literal keys only; the theme and area keys are built with an
        # f-string and checked below.
        self.assertEqual(self._missing(paths, r'\bt\("([a-z_.]+)"'), [])

    def test_inspector_keys_exist(self):
        paths = glob.glob(os.path.join(BUNDLE, "pi", "*"))
        self.assertEqual(self._missing(paths, r'data-i18n="([a-z_.]+)"'), [])
        self.assertEqual(self._missing(paths, r'GSR\.t\("([a-z_.]+)"'), [])

    def test_built_keys_exist(self):
        known = english()
        for theme in render.THEMES:
            self.assertIn(f"theme.{theme}", known)
        for area in plugin.AREAS:
            self.assertIn(f"key.area.{area}", known)


if __name__ == "__main__":
    unittest.main()
