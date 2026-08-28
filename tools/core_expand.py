#!/usr/bin/env python3
"""Propose new taxonomy terms from the sweep's OWN results.

THE VOCABULARY WAS WRITTEN TOP-DOWN and it shows: "trend following" -- the
single most common phrase across 2,963 practitioner articles, and the name of a
desk sleeve -- was never in it, so route A never searched for it and 896 papers
on Semantic Scholar were unreachable. Guessing harder is not a fix; the guess
is the failure mode.

The corpus can name its own subjects. Every swept paper records the term that
found it, so for each of the 299 terms we already hold the papers it retrieves.
A phrase that appears often in ONE term's results and rarely across the sweep
as a whole is a neighbouring subject that term keeps bumping into -- and if it
is not itself a term, it is a hole in the vocabulary.

SCORING IS LIFT, NOT FREQUENCY. Ranking by raw count returns "stock returns"
and "asset pricing" for every term, because common phrases are common
everywhere. Lift asks whether a phrase is disproportionately present here,
which is what makes it characteristic of this term rather than of finance.

NOTHING IS ADDED. Output is a reviewable candidate list; terms enter the
taxonomy only after core_tags.py --validate has measured them on S2, because a
phrase nobody writes costs a request and returns nothing.

    python tools/core_expand.py --min-lift 3 --per-term 3
"""

import argparse
import collections
import csv
import io
import json
import math
import pathlib
import re
import sys

OUT = pathlib.Path("export")
SWEEP = OUT / "core_sweep.json"
TAGS = OUT / "core_tags.csv"

STOP = set(
    "the a an of and or in on for to with from by is are as at we this that "
    "its their new evidence using use does do it more than what how why can "
    "be has have not but who when where which some our you your they them "
    "these those about into over under vs via towards toward within across "
    "between during after before through against among each other others "
    "case study approach analysis model models method methods based paper "
    "results result effect effects impact role does new empirical".split())

# Phrases that are structure rather than subject. They pass a lift test in
# families whose papers share a house style, and mean nothing as search terms.
JUNK = re.compile(
    r"^(evidence from|new evidence|empirical (study|analysis)|case (of|study)|"
    r"a (note|survey|review)|the (role|case|effect|impact)|role of|effect of|"
    r"impact of|does the|do the|what (drives|explains))\b")


def log(m):
    print(m, flush=True)


def _grams(title):
    w = [x for x in re.sub(r"[^a-z0-9 ]", " ", (title or "").lower()).split()
         if x not in STOP and len(x) > 2]
    for n in (2, 3):
        for i in range(len(w) - n + 1):
            yield " ".join(w[i:i + n])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-lift", type=float, default=3.0,
                    help="how much more common a phrase must be in this "
                         "term's papers than in the sweep overall")
    ap.add_argument("--min-docs", type=int, default=8,
                    help="a phrase must appear in this many of the term's "
                         "papers -- fewer is one author's habit")
    ap.add_argument("--per-term", type=int, default=3)
    args = ap.parse_args()

    if not SWEEP.exists():
        log(f"[expand] {SWEEP} missing -- run core_sources.py sweep first")
        return 2
    tax = {r["term"].strip().lower(): r["family"]
           for r in csv.DictReader(io.open(TAGS, encoding="utf-8"))}
    rows = json.loads(SWEEP.read_text(encoding="utf-8"))
    log(f"[expand] {len(rows):,} swept papers across {len(tax)} terms")

    # background: how often each phrase appears anywhere in the sweep
    bg = collections.Counter()
    by_term = collections.defaultdict(list)
    for r in rows:
        t = (r.get("tag") or "").strip().lower()
        if t:
            by_term[t].append(r)
    for r in rows:
        bg.update(set(_grams(r.get("title"))))
    total = len(rows)
    log(f"[expand] background vocabulary: {len(bg):,} phrases")

    props = []
    for term, papers in sorted(by_term.items()):
        if len(papers) < 20:
            continue
        loc = collections.Counter()
        for r in papers:
            loc.update(set(_grams(r.get("title"))))
        cands = []
        for g, n in loc.items():
            if n < args.min_docs or g in tax or JUNK.match(g):
                continue
            # lift with a prior, so a phrase seen twice globally cannot score
            # infinitely just because it is rare
            exp = (bg[g] / total) * len(papers) + 1.0
            lift = n / exp
            if lift >= args.min_lift:
                cands.append((lift, n, g))
        cands.sort(reverse=True)
        for lift, n, g in cands[:args.per_term]:
            props.append({"proposed": g, "near_term": term,
                          "family": tax.get(term, ""), "in_papers": n,
                          "lift": round(lift, 1)})

    # a phrase proposed from several different terms is a stronger candidate
    seen = collections.Counter(p["proposed"] for p in props)
    for p in props:
        p["proposed_by_n_terms"] = seen[p["proposed"]]
    props.sort(key=lambda p: (-p["proposed_by_n_terms"], -p["lift"]))
    dedup, done = [], set()
    for p in props:
        if p["proposed"] in done:
            continue
        done.add(p["proposed"])
        dedup.append(p)

    (OUT / "core_expand_candidates.json").write_text(
        json.dumps(dedup, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"[expand] {len(dedup):,} distinct candidate terms proposed\n")
    log(f"{'proposed':<34}{'lift':>6}{'papers':>8}  near-term (family)")
    for p in dedup[:40]:
        log(f"{p['proposed']:<34}{p['lift']:>6}{p['in_papers']:>8}  "
            f"{p['near_term']} ({p['family'].split('_')[0]})")
    log(f"\n[expand] written to {OUT}/core_expand_candidates.json")
    log(f"[expand] VALIDATE before adding: core_tags.py --validate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
