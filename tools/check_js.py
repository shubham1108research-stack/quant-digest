#!/usr/bin/env python3
"""Parse the JavaScript that portal.py emits, before it ships.

The whole portal is a Python string in portal.py. Python will happily hold a
syntactically broken JavaScript program -- it is just text -- so `python -c
"import portal"` proves nothing, the deploy succeeds, and the failure surfaces
as a blank page in a browser nobody is watching. This project has shipped that
twice: `async(role,...)` parsed as a call to a function named `async`, and a
block spliced into the middle of a fetch() object literal.

Parsing the EVALUATED template matters. portal.py's _INDEX is an ordinary
(non-raw) Python string, so a regex over the file's raw text sees `\\\\.` where
the browser will see `\\.` and reports errors that are not there. Import the
module and read the attribute; do not scrape the source.

    python tools/check_js.py
"""

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main():
    try:
        import esprima
    except ImportError:
        # Not fatal: this is a guard, and a guard that blocks a deploy because
        # of its own missing dependency is worse than the bug it prevents.
        print("[js] esprima is not installed; skipping the syntax check")
        return 0

    import portal

    html = portal._INDEX
    # Placeholders are substituted by portal.build() at render time; they are
    # not valid JS on their own, so stand in a literal before parsing.
    html = re.sub(r"__[A-Z_]+__", "null", html)
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not blocks:
        print("[js] no <script> blocks found in portal._INDEX -- the template "
              "shape changed and this check is no longer looking at anything")
        return 1

    bad = 0
    for i, block in enumerate(blocks):
        try:
            esprima.parseScript(block)
        except Exception as e:                          # noqa: BLE001
            bad += 1
            print(f"[js] block {i} FAILED to parse: {e}")
            line = getattr(e, "lineNumber", None)
            if line:
                rows = block.split("\n")
                lo, hi = max(0, line - 3), min(len(rows), line + 2)
                for n in range(lo, hi):
                    mark = ">>" if n == line - 1 else "  "
                    print(f"     {mark} {n + 1:5d}  {rows[n][:120]}")
    if bad:
        print(f"[js] {bad} block(s) would not parse -- the portal would load "
              f"blank. Not deploying.")
        return 1
    total = sum(len(b.split("\n")) for b in blocks)
    print(f"[js] {len(blocks)} script block(s), {total:,} lines, parsed clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
