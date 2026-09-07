#!/usr/bin/env python3
"""Write a release version into the plugin manifest.

The manifest is the only place a version means anything -- it is what OpenDeck
shows and what a user reads when reporting a bug -- so semantic-release writes
it there rather than only tagging the repository. Edited as text rather than
re-serialised so the diff is one line and the file's formatting survives.
"""

import os
import re
import sys

MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dev.gsr.sdPlugin", "manifest.json")


def main(version):
    if not re.match(r"^\d+\.\d+\.\d+", version):
        print(f"refusing to write a non-semver version: {version!r}")
        return 1
    # A prerelease like 1.2.0-next.1 is not valid in a manifest, which takes
    # MAJOR.MINOR.PATCH only; the tag keeps the full version either way.
    numeric = re.match(r"^\d+\.\d+\.\d+", version).group(0)
    source = open(MANIFEST).read()
    updated, count = re.subn(r'("Version":\s*")[^"]*(")',
                             rf'\g<1>{numeric}\g<2>', source, count=1)
    if count != 1:
        print("no Version field found in manifest.json")
        return 1
    open(MANIFEST, "w").write(updated)
    print(f"manifest.json Version -> {numeric}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: set_version.py <version>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
