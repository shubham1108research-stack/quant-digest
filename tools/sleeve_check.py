#!/usr/bin/env python3
"""Score a SAMPLE with the new sleeve fields and print it for human review,
before anything is written to the archive.

Both earlier attempts at this classification failed and neither failure was
visible until it was measured -- a keyword pass found 2 Carry papers out of 162,
an embedding pass drifted Carry into EU fiscal integration. So this deliberately
stops before persisting: it scores, groups by sleeve, and prints titles for you
to read. Correct the taxonomy in config.SLEEVES, re-run, and only backfill the
archive once the sample looks right.

The sample is stratified rather than random: it force-includes papers matching
carry-adjacent language, because that boundary is the one that has broken twice
and a random draw would contain almost none of them.

  python tools/sleeve_check.py --n 120
"""

import argparse
import collections
import json
import pathlib
import random
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import llm     # noqa: E402
import store   # noqa: E402

# language the carry literature actually uses -- the exact set a keyword
# classifier fired on wrongly, so these are the cases worth watching
CARRY_PROBE = ["convenience yield", "forward premium", "roll yield",
               "backwardation", "carry trade", "uncovered interest",
               "term premium", "currency carry"]


def log(m):
    print(m, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--probe", type=int, default=30,
                    help="how many carry-boundary papers to force in")
    args = ap.parse_args()

    con = store.connect()
    rows = []
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            m = json.loads(meta)
        except Exception:                                   # noqa: BLE001
            m = {}
        abstract = (m.get("abstract") or "").strip()
        if len(abstract.split()) < 40:
            continue                       # nothing to classify from
        rows.append({"uid": uid, "title": title or "", "abstract": abstract,
                     "url": m.get("url", ""), "source": m.get("source", ""),
                     "section": m.get("section", "1")})

    hay = lambda r: (r["title"] + " " + r["abstract"]).lower()      # noqa: E731
    probe = [r for r in rows if any(k in hay(r) for k in CARRY_PROBE)]
    rest = [r for r in rows if r not in probe]
    random.seed(7)
    random.shuffle(probe)
    random.shuffle(rest)
    sample = probe[:args.probe] + rest[:max(0, args.n - args.probe)]
    log(f"[check] {len(rows)} papers with a usable abstract")
    log(f"[check] sample: {len(sample)} "
        f"({min(args.probe, len(probe))} carry-boundary + "
        f"{len(sample) - min(args.probe, len(probe))} random)")

    if not llm.have_key():
        raise SystemExit("no LLM provider key set")
    llm.start_run_budget(0)                # dedicated job: no shared deadline
    llm.rank(sample, log)

    scored = [r for r in sample if r.get("sleeves")]
    log(f"\n[check] scored {len(scored)}/{len(sample)}\n")

    # multi-label: a paper appears under EVERY sleeve it carries
    by = collections.defaultdict(list)
    for r in scored:
        for k in r["sleeves"]:
            by[k].append(r)
    ntags = collections.Counter(len(r["sleeves"]) for r in scored)
    log(f"[check] tags per paper: {dict(sorted(ntags.items()))}")

    log("=" * 72)
    for key in config.SLEEVES:
        items = by.get(key, [])
        fits = collections.Counter(r.get("desk_fit", 0) for r in items)
        log(f"\n### {key}  ({len(items)} papers)  desk_fit "
            f"{dict(sorted(fits.items(), reverse=True))}")
        for r in sorted(items, key=lambda x: -x.get("desk_fit", 0))[:8]:
            also = [k for k in r["sleeves"] if k != key]
            tag = ("+" + ",".join(also)) if also else ""
            log(f"   fit={r.get('desk_fit',0)} {tag:<22} {r['title'][:66]}")
    log("\n" + "=" * 72)

    # the boundary that has broken twice -- show it explicitly
    log("\n### CARRY-BOUNDARY papers and where they landed")
    n_carry = 0
    for r in scored:
        if any(k in hay(r) for k in CARRY_PROBE):
            tags = ",".join(r["sleeves"])
            n_carry += "carry" in r["sleeves"]
            log(f"   {tags:<34} fit={r.get('desk_fit',0)}  {r['title'][:56]}")
    log(f"\n   -> {n_carry} of these carry the 'carry' tag")

    log("\n[check] nothing was written to the archive -- review, adjust "
        "config.SLEEVES, re-run, then backfill.")


if __name__ == "__main__":
    main()
