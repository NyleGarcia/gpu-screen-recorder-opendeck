#!/usr/bin/env python3
"""Check the plugin bundle is loadable before it is released.

OpenDeck fails a broken plugin quietly: a missing property inspector gives an
empty panel, a missing layout gives a blank touch strip, and neither writes
anything to a log. Nothing here needs a Stream Deck, so it can gate a release
in a way that hardware testing cannot.
"""

import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "dev.gsr.sdPlugin")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
# The oldest Python the plugin claims to run on. It executes against whatever
# python3 the host distribution ships, not one it chooses, so syntax newer
# than this is a crash on someone's machine rather than a style question.
OLDEST_PYTHON = (3, 10)
# Layouts named with a leading $ are built into OpenDeck and have no file.
BUILTIN_LAYOUT = re.compile(r"^\$[A-Z]\d$")
# Elgato omits the extension in manifest image references.
IMAGE_SUFFIXES = ("", ".png", ".svg")

problems = []


def check(condition, message):
    if not condition:
        problems.append(message)
    return condition


def _modules():
    for root, _dirs, files in os.walk(BUNDLE):
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


# A backslash inside the expression part of an f-string. Legal from 3.12
# (PEP 701), a SyntaxError before it. compileall cannot see this because it
# uses the interpreter running it, and ast.parse's feature_version does not
# either -- the rule lives in the tokenizer, not the grammar. Checked with a
# scanner because the alternative is having no 3.10 interpreter to ask.
_FSTRING = re.compile(r"(?<![\w])([fF][rR]?|[rR][fF])(\'\'\'|\"\"\"|\'|\")")


def _fstring_expressions(line):
    """Yield the {...} parts of every f-string opened on one line."""
    for match in _FSTRING.finditer(line):
        quote = match.group(2)
        rest = line[match.end():]
        end = rest.find(quote)
        body = rest if end < 0 else rest[:end]
        depth, buf = 0, ""
        for ch in body:
            if ch == "{":
                depth += 1
                if depth == 1:
                    buf = ""
                    continue
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield buf
                    continue
            if depth >= 1:
                buf += ch


def check_old_grammar(path):
    for number, line in enumerate(open(path), 1):
        for expression in _fstring_expressions(line):
            if "\\" in expression:
                problems.append(
                    f"{os.path.relpath(path, ROOT)}:{number} puts a backslash "
                    f"inside an f-string expression, which is a SyntaxError "
                    f"before Python 3.12; bind it to a name first")
    try:
        ast.parse(open(path).read(), filename=path,
                  feature_version=OLDEST_PYTHON)
    except SyntaxError as exc:
        problems.append(
            f"{os.path.relpath(path, ROOT)}:{exc.lineno} is not valid Python "
            f"{OLDEST_PYTHON[0]}.{OLDEST_PYTHON[1]}: {exc.msg}")


def resolve_image(reference):
    return any(os.path.isfile(os.path.join(BUNDLE, reference + suffix))
               for suffix in IMAGE_SUFFIXES)


def main():
    manifest_path = os.path.join(BUNDLE, "manifest.json")
    if not check(os.path.isfile(manifest_path), "manifest.json is missing"):
        return report()
    try:
        manifest = json.load(open(manifest_path))
    except ValueError as exc:
        problems.append(f"manifest.json is not valid JSON: {exc}")
        return report()

    check(SEMVER.match(str(manifest.get("Version", ""))),
          f"Version {manifest.get('Version')!r} is not MAJOR.MINOR.PATCH -- "
          f"semantic-release writes it, so a mismatch means the bump failed")
    check(resolve_image(manifest.get("Icon", "")),
          f"plugin Icon {manifest.get('Icon')!r} resolves to no file")

    entry = manifest.get("CodePathLin") or manifest.get("CodePath")
    entry_path = os.path.join(BUNDLE, entry or "")
    if check(entry and os.path.isfile(entry_path),
             f"CodePathLin {entry!r} does not exist"):
        check(os.access(entry_path, os.X_OK),
              f"{entry} is not executable; OpenDeck runs it directly")

    uuids = set()
    for action in manifest.get("Actions") or []:
        uuid = action.get("UUID", "<unnamed>")
        check(uuid not in uuids, f"duplicate action UUID {uuid}")
        uuids.add(uuid)

        inspector = action.get("PropertyInspectorPath")
        if inspector:
            check(os.path.isfile(os.path.join(BUNDLE, inspector)),
                  f"{uuid}: property inspector {inspector} is missing")

        check(resolve_image(action.get("Icon", "")),
              f"{uuid}: Icon {action.get('Icon')!r} resolves to no file")
        for i, state in enumerate(action.get("States") or []):
            check(resolve_image(state.get("Image", "")),
                  f"{uuid}: state {i} Image {state.get('Image')!r} "
                  f"resolves to no file")

        layout = (action.get("Encoder") or {}).get("layout")
        if layout and not BUILTIN_LAYOUT.match(layout):
            path = os.path.join(BUNDLE, layout)
            if check(os.path.isfile(path),
                     f"{uuid}: encoder layout {layout} is missing"):
                try:
                    items = json.load(open(path)).get("items") or []
                except ValueError as exc:
                    problems.append(f"{uuid}: layout {layout} is not valid "
                                    f"JSON: {exc}")
                    continue
                check(items, f"{uuid}: layout {layout} defines no items")
                for item in items:
                    check("key" in item and "type" in item and "rect" in item,
                          f"{uuid}: layout {layout} has an item missing "
                          f"key/type/rect")

    for path in _modules():
        check_old_grammar(path)

    # Every file the entry point will import must be present in the bundle:
    # a module left out of a release zip is only discovered on someone's deck.
    for module in ("plugin.py", "gsrdeck/ws.py", "gsrdeck/state.py",
                   "gsrdeck/control.py", "gsrdeck/config.py",
                   "gsrdeck/render.py"):
        check(os.path.isfile(os.path.join(BUNDLE, module)),
              f"{module} is missing from the bundle")

    return report()


def report():
    if problems:
        print(f"{len(problems)} problem(s) in the plugin bundle:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("plugin bundle OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
