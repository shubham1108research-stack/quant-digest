#!/usr/bin/env python3
"""Assign subject tags to every paper from its title and abstract.

A FINER layer beneath sleeves. A sleeve says which book a paper belongs to
(trend_cta, carry, fx); a tag says what it is actually about (roll yield, ZLB,
positioning). The vocabulary is closed and lives in config.TAGS -- see the
comment there for why an open keyword extractor is the wrong tool.

DETERMINISTIC AND FREE. No model call, so this runs over the whole archive in
seconds and can be re-run after every vocabulary edit. Papers the matcher
leaves empty are the only ones the scorer spends a token on, via the `tags`
field in llm.py's schema, constrained to this same vocabulary.

WORD BOUNDARIES, AND WHY THE BACKSLASHES LOOK LIKE THAT
Surfaces are matched with \\b...\\b so "etf " does not fire inside "wetfoot"
and "gmm" does not fire inside "gmmv". This file is a normal Python module, so
a single backslash is correct HERE -- but the same pattern written inside
portal.py's _INDEX string would be eaten by Python and shipped as a backspace
byte. That has happened three times in this repo; tools/check_js.py now fails
the build on it. Mentioned because the next person to add a regex will be
copying from somewhere.

    python tools/tags.py --dry-run        # counts and the frequency table
    python tools/tags.py                  # write tags into items.meta
    python tools/tags.py --force          # re-tag rows that already have tags
"""

import argparse
import collections
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config   # noqa: E402
import store    # noqa: E402


def log(m):
    print(m, flush=True)


def _compile():
    """canonical tag -> one compiled alternation over its surface forms.

    Longest surface first so "time-series momentum" wins over "momentum" when
    both are present in the same alternation -- Python's `|` is first-match,
    not longest-match, and the order is the only thing that decides it.
    """
    out = {}
    for tag, surfaces in config.TAGS.items():
        parts = sorted((s.strip().lower() for s in surfaces if s.strip()),
                       key=len, reverse=True)
        # A surface ending in a space ("etf ", "the fed ") is deliberate: it is
        # how the vocabulary asks for a word boundary on the right only. Strip
        # it and let \b do the work, which also matches at end-of-string.
        alts = "|".join(re.escape(p.rstrip()) for p in parts)
        out[tag] = re.compile(r"\b(?:" + alts + r")\b", re.I)
    return out


_PATTERNS = _compile()


def tags_for(title, abstract, summary=""):
    """Every tag whose vocabulary appears in the text. Order is stable."""
    text = " ".join(x for x in (title or "", abstract or "", summary or "") if x)
    if not text.strip():
        return []
    return [tag for tag, rx in _PATTERNS.items() if rx.search(text)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-tag rows that already carry tags")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    con = store.connect()
    rows = con.execute("SELECT uid, title, meta FROM items").fetchall()
    freq = collections.Counter()
    n_live = n_tagged = n_empty = n_written = 0
    per_paper = []
    patch = []

    for uid, title, meta in rows:
        try:
            d = json.loads(meta or "{}")
        except Exception:                            # noqa: BLE001
            continue
        if d.get("retired"):
            continue
        n_live += 1
        if d.get("tags") and not args.force:
            n_tagged += 1
            freq.update(d["tags"])
            per_paper.append(len(d["tags"]))
            continue
        found = tags_for(title, d.get("abstract"), d.get("summary"))
        if found:
            n_tagged += 1
            freq.update(found)
            per_paper.append(len(found))
            patch.append((uid, found))
        else:
            n_empty += 1
        if args.limit and len(patch) >= args.limit:
            break

    log(f"[tags] {n_live:,} live rows")
    log(f"[tags]   at least one tag : {n_tagged:,} ({100.0*n_tagged/max(1,n_live):.0f}%)")
    log(f"[tags]   no tag matched   : {n_empty:,} "
        f"-- these are what the LLM fallback is for")
    if per_paper:
        log(f"[tags]   mean tags/paper  : {sum(per_paper)/len(per_paper):.1f}")
    log(f"\n[tags] most common ({len(freq)} of {len(config.TAGS)} tags ever fired):")
    for t, n in freq.most_common(28):
        log(f"    {t:<24} {n:>6,}")
    never = [t for t in config.TAGS if t not in freq]
    if never:
        log(f"\n[tags] never fired ({len(never)}): {', '.join(never)}")
        log("[tags] a tag that never fires is vocabulary nobody can filter on "
            "-- either its surfaces are wrong or the corpus does not cover it")

    if args.dry_run:
        log("\n[tags] dry run -- nothing written")
        return
    for uid, found in patch:
        if store.update_meta(con, uid, {"tags": found}):
            n_written += 1
    con.commit()
    log(f"\n[tags] wrote tags onto {n_written:,} rows")


if __name__ == "__main__":
    main()
