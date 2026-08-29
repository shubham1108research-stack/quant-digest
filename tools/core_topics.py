#!/usr/bin/env python3
"""Fetch OpenAlex topics for every candidate, so strays can be found by subject.

WHY THIS EXISTS. clean_core.py decides what is off-domain with a 33-word
blocklist (organizational, tourism, nursing, ...) matched against the title.
That catches management-literature strays riding in on generic method terms,
and it misses anything whose title happens to use finance-shaped words. A
100-paper sample of the untagged turned up "Functional Imaging of Neural
Responses to Expectancy and Experience" sitting in the pool: no off-domain word
in its title, so the gate kept it. OpenAlex labels it
`Neural and Behavioral Psychology Studies` in one field lookup.

THE GRANULARITY IS THE POINT, AND IT CUTS BOTH WAYS. These topics cannot tell
carry from FX -- measured, and the same reason s2FieldsOfStudy was rejected for
tagging. What they CAN do is say whether a paper is in economics at all, which
is exactly the question the stray cleaner asks and exactly the question a
402-term vocabulary is the wrong instrument for. Coarse labels for a coarse
question.

NOTHING IS REMOVED HERE. This writes export/core_topics.json and reports the
distribution. Which fields count as off-domain is a judgement about the desk,
not a fact about the data, so the cut belongs to a reviewed step -- and to
whoever is reading the list, not to this script.

    python tools/core_topics.py                 # every candidate with a DOI
    python tools/core_topics.py --limit 2000    # a sample first
    python tools/core_topics.py --report        # distribution from the cache
"""

import argparse
import collections
import csv
import io
import json
import pathlib
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import oa as oa_auth                                         # noqa: E402
from progress import Progress                                # noqa: E402

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
DEST = OUT / "core_topics.json"
API = "https://api.openalex.org/works"
PER = 100          # measured: 100 per request halves the call count vs 50 and
                   # returns the same 158 MB overall
SELECT = "doi,primary_topic,topics"


def log(m):
    print(m, flush=True)


def _load_cache():
    if not DEST.exists():
        return {}
    try:
        return json.loads(DEST.read_text(encoding="utf-8")) or {}
    except Exception as e:                                   # noqa: BLE001
        # A cache that exists but will not parse must stop the run. Returning
        # {} would silently re-fetch 2,380 requests' worth of somebody else's
        # budget and then overwrite the file that was merely unreadable.
        raise SystemExit(
            f"[topics] {DEST} exists but will not parse ({type(e).__name__}). "
            f"REFUSING to overwrite it -- move it aside to start fresh.")


def _report(cache, rows):
    if not cache:
        log("[topics] cache empty -- nothing to report")
        return
    field = collections.Counter()
    sub = collections.Counter()
    top = collections.Counter()
    for v in cache.values():
        field[v.get("f") or "(none)"] += 1
        sub[v.get("sf") or "(none)"] += 1
        top[v.get("t") or "(none)"] += 1
    log(f"\n[topics] {len(cache):,} papers carry a topic\n")
    log("FIELD (the coarse cut -- this is what a stray filter would use):")
    for k, n in field.most_common(25):
        log(f"   {n:>7,}  {k}")
    log("\nSUBFIELD, top 20:")
    for k, n in sub.most_common(20):
        log(f"   {n:>7,}  {k}")
    log("\nTOPIC, top 25:")
    for k, n in top.most_common(25):
        log(f"   {n:>7,}  {k}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", action="store_true",
                    help="print the distribution from the cache and stop")
    ap.add_argument("--pause", type=float, default=0.15)
    args = ap.parse_args()

    if not CAND.exists():
        raise SystemExit(f"[topics] {CAND} missing -- build the core list first")
    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))
    cache = _load_cache()
    if args.report:
        _report(cache, rows)
        return 0

    targets = [r for r in rows if (r.get("doi") or "").strip()]
    log(f"[topics] {len(rows):,} candidates; {len(targets):,} have a DOI "
        f"({len(rows)-len(targets):,} do not and cannot be looked up this way)")
    todo = [r for r in targets if r["uid"] not in cache]
    if args.limit:
        todo = todo[:args.limit]
    log(f"[topics] {len(cache):,} cached; {len(todo):,} to fetch in "
        f"{(len(todo)+PER-1)//PER:,} requests")
    if not todo:
        _report(cache, rows)
        return 0

    oa_auth.preflight(log)

    fails = collections.Counter()
    first_err = ""
    got = 0
    prog = Progress((len(todo) + PER - 1) // PER, "topics", every_s=60)
    for i in range(0, len(todo), PER):
        chunk = todo[i:i + PER]
        by_doi = {r["doi"].lower(): r for r in chunk}
        try:
            rr = requests.get(
                API, headers=oa_auth.headers({"User-Agent": "quant-digest/1.0"}),
                params={"filter": "doi:" + "|".join(
                            "https://doi.org/" + d for d in by_doi),
                        "select": SELECT, "per-page": PER}, timeout=120)
        except Exception as e:                               # noqa: BLE001
            fails[type(e).__name__] += 1
            prog.tick()
            continue
        if not rr.ok:
            # A REJECTED REQUEST IS NOT "THESE PAPERS HAVE NO TOPIC". Counted
            # and reported; the uids stay out of the cache so a re-run retries
            # them rather than recording an absence that was really an error.
            fails[rr.status_code] += 1
            if not first_err:
                first_err = rr.text[:200].replace("\n", " ")
            prog.tick()
            continue
        for w in (rr.json().get("results") or []):
            d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
            row = by_doi.get(d)
            if not row:
                continue
            pt = w.get("primary_topic") or {}
            cache[row["uid"]] = {
                "t": pt.get("display_name"),
                "sf": (pt.get("subfield") or {}).get("display_name"),
                "f": (pt.get("field") or {}).get("display_name"),
                "s": round(pt.get("score") or 0, 3),
                "all": [x.get("display_name") for x in (w.get("topics") or [])],
            }
            got += 1
        prog.tick()
        if (i // PER) % 20 == 19:
            DEST.write_text(json.dumps(cache), encoding="utf-8")
        time.sleep(args.pause)
    prog.done()
    DEST.write_text(json.dumps(cache), encoding="utf-8")

    log(f"[topics] resolved {got:,} this run; {len(cache):,} cached total "
        f"({100*len(cache)/max(1,len(targets)):.1f}% of DOI-bearing candidates)")
    if fails:
        log(f"[topics] !! {sum(fails.values()):,} requests FAILED: {dict(fails)}")
        if first_err:
            log(f"[topics]    first error: {first_err}")
        log(f"[topics]    those papers are NOT cached -- re-run to retry them")
    _report(cache, rows)
    log(f"\n[topics] written to {DEST} -- NOTHING removed. The off-domain cut "
        f"is a judgement about the desk and belongs to a reviewed step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
