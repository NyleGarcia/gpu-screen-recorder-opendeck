#!/usr/bin/env python3
"""Build the release archive OpenDeck installs from.

The zip contains the dev.gsr.sdPlugin directory at its root, which is what
OpenDeck's installer expects: it extracts straight into the plugins directory,
so a zip of the directory's *contents* silently installs a broken plugin with
no manifest where one is expected.

Written with zipfile rather than the zip command: the plugin is stdlib-only
Python and the release should build on a machine that has nothing else, which
a missing zip binary would otherwise be the one exception to.
"""

import json
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = "dev.gsr.sdPlugin"
# Bytecode compiled by one Python is loaded in preference to source by
# another, and the plugin runs against whatever python3 the host has. The log
# is the plugin's own scratch file and has no business in a release.
EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_SUFFIXES = (".pyc", ".log")


def main():
    manifest = json.load(open(os.path.join(ROOT, BUNDLE, "manifest.json")))
    version = manifest["Version"]
    dist = os.path.join(ROOT, "dist")
    shutil.rmtree(dist, ignore_errors=True)
    os.makedirs(dist)
    out = os.path.join(dist, f"gpu-screen-recorder-opendeck-{version}.zip")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for directory, subdirectories, files in os.walk(
                os.path.join(ROOT, BUNDLE)):
            subdirectories[:] = [name for name in sorted(subdirectories)
                                 if name not in EXCLUDED_DIRS]
            for name in sorted(files):
                if name.endswith(EXCLUDED_SUFFIXES):
                    continue
                path = os.path.join(directory, name)
                inside = os.path.relpath(path, ROOT)
                info = zipfile.ZipInfo.from_file(path, inside)
                # The launcher has to stay executable: OpenDeck runs
                # CodePathLin directly, and a zip that drops the bit installs
                # a plugin that cannot start.
                info.external_attr = (os.stat(path).st_mode & 0xFFFF) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(path, "rb") as handle:
                    archive.writestr(info, handle.read())
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
