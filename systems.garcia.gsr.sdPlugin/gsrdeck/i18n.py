"""The words on the keys and in the inspectors, in OpenDeck's language.

One file per language in lang/, named by its code (en.json, fr.json). Each
is laid out the way OpenDeck expects a plugin's <code>.json: the plugin's and
the actions' Name and Tooltip at the top level, which OpenDeck shows in its
action list, and every other string under "Localization", which is what this
module serves. The packaging copies the files next to manifest.json, the only
place OpenDeck looks for them.

English is the reference: a key missing from another language falls back to
it, and a key missing from English shows as itself, so a forgotten string is
visible rather than blank.
"""

import json
import os

LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lang")
DEFAULT = "en"

_reference = {}
_strings = {}
_language = DEFAULT


def _load(code):
    try:
        with open(os.path.join(LANG_DIR, f"{code}.json"),
                  encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    strings = data.get("Localization")
    return strings if isinstance(strings, dict) else None


def available():
    try:
        names = os.listdir(LANG_DIR)
    except OSError:
        return [DEFAULT]
    return sorted(name[:-5] for name in names if name.endswith(".json"))


def resolve(code):
    """The language file to use for a code OpenDeck or the system gave.

    "fr", "fr_FR", "fr-FR" and "fr_FR.UTF-8" all mean French; a region with
    no file of its own uses the base language, and anything unknown English.
    """
    code = (code or "").split(".")[0].replace("-", "_")
    known = available()
    for candidate in (code, code.split("_")[0].lower()):
        if candidate in known:
            return candidate
    return DEFAULT


def set_language(code):
    global _reference, _strings, _language
    _reference = _load(DEFAULT) or {}
    _language = resolve(code)
    _strings = (_load(_language) or {}) if _language != DEFAULT else {}
    return _language


def language():
    return _language


def t(key, default=None, **values):
    """The string for `key`, with {placeholders} filled from `values`.

    `default` is used when no language has the key; without one the key
    itself is shown.
    """
    if not _reference:
        set_language(_language)
    text = _strings.get(key) or _reference.get(key) or default or key
    if values:
        try:
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def table(prefix=""):
    """Every string whose key starts with `prefix`, for the inspectors."""
    if not _reference:
        set_language(_language)
    merged = dict(_reference)
    merged.update(_strings)
    return {key: value for key, value in merged.items()
            if key.startswith(prefix)}
