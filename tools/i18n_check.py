#!/usr/bin/env python3
"""Check translation files against the strings used in the source.

Two independent catalogs exist:

* Python app (CLI, GUI, daemon): ``fanctrl/*.py``      -> ``fanctrl/locales/<lang>.json``
* GNOME panel extension:         ``gnome-extension/*/extension.js``
                                                        -> ``gnome-extension/*/locales/<lang>.json``

Usage::

    python3 tools/i18n_check.py              # report missing / unused keys for every language
    python3 tools/i18n_check.py --init de    # create or update de.json in both places

``--init`` keeps existing translations and adds every missing string with an
empty value; empty values fall back to English at runtime.
Exit status is 1 when any translation is missing a key or has an unused key.
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

JS_CALL = re.compile(r"""\b_\(\s*(['"])((?:\\.|(?!\1).)*)\1""", re.S)


def python_strings():
    found = set()
    for path in sorted((ROOT / "fanctrl").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_"
                and node.args
            ):
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)
                else:
                    print(f"warning: {path.name}:{node.lineno}: _() needs a plain string literal",
                          file=sys.stderr)
    return found


def js_unescape(text):
    escapes = {"n": "\n", "t": "\t", "r": "\r"}
    return re.sub(r"\\(.)", lambda m: escapes.get(m.group(1), m.group(1)), text, flags=re.S)


def js_strings(ext_dir):
    source = (ext_dir / "extension.js").read_text(encoding="utf-8")
    return {js_unescape(m.group(2)) for m in JS_CALL.finditer(source)}


def catalogs():
    yield "python", python_strings(), ROOT / "fanctrl" / "locales"
    for ext_dir in sorted((ROOT / "gnome-extension").iterdir()):
        if (ext_dir / "extension.js").is_file():
            yield "extension", js_strings(ext_dir), ext_dir / "locales"


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--init", metavar="LANG", help="create/update LANG.json with all strings")
    args = parser.parse_args()

    problems = 0
    for name, strings, locale_dir in catalogs():
        if args.init:
            path = locale_dir / f"{args.init}.json"
            data = load(path) if path.exists() else {}
            added = 0
            for key in sorted(strings):
                if key not in data:
                    data[key] = ""
                    added += 1
            save(path, data)
            print(f"{name}: {path.relative_to(ROOT)} — {added} new, {len(strings)} total")
            continue

        print(f"{name}: {len(strings)} strings")
        for path in sorted(locale_dir.glob("*.json")):
            data = {k: v for k, v in load(path).items() if not k.startswith("_")}
            missing = sorted(strings - data.keys())
            unused = sorted(data.keys() - strings)
            empty = sorted(k for k, v in data.items() if k in strings and not v)
            done = len(strings) - len(missing) - len(empty)
            print(f"  {path.name}: {done}/{len(strings)} translated")
            for key in missing:
                print(f"    missing: {key!r}")
            for key in unused:
                print(f"    unused:  {key!r}")
            problems += len(missing) + len(unused)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
