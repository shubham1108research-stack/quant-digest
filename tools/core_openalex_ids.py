#!/usr/bin/env python3
"""Map every candidate's DOI to its own OpenAlex Work ID.

WHY THIS IS A SEPARATE, SECOND PASS. core_openalex_extract.py already
fetched referenced_works for the whole pool -- but referenced_works is a list
of OpenAlex WORK IDS ("W2013178519"), not DOIs, and OpenAlex's `select`
parameter is strict: verified live, asking for `doi,title` returns exactly
those two keys, never `id` unless it is asked for. So the edges are held, but
there was no way to tell whether any of them point at a paper THIS POOL also
holds -- inverting them into a forward-citation graph needs a Work ID for our
own 230,804 papers, and that was never fetched.

Re-running the full extractor with `id` added to its SELECT would refetch
every heavy field (abstract, authorships, keywords) a second time for
nothing -- this asks for exactly two fields, doi and id, so the payload is a
few dozen bytes per paper instead of a few thousand.

OUTPUT: export/core_openalex_ids.json, {uid: bare_work_id}. Small enough (a
few MB for 230k entries) that a single JSON dict with periodic checkpointing
is the right shape here, unlike the ndjson choice in the two extract tools --
this file is never large enough for "hold it all in memory" to be the
problem those tools were built to avoid.

    python tools/core_openalex_ids.py
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

import oa as oa_auth                                          # noqa: E402
from progress import Progress                                 # noqa: E402

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
DEST = OUT / "core_openalex_ids.json"
API = "https://api.openalex.org/works"
PER = 100


def log(m):
    print(m, flush=True)


def _bare(work_id):
    return (work_id or "").rsplit("/", 1)[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pause", type=float, default=0.1)
    args = ap.parse_args()

    if not CAND.exists():
        raise SystemExit(f"[oaid] {CAND} missing -- build the core list first")
    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))
    targets = [r for r in rows if (r.get("doi") or "").strip()
               and "http" not in r["doi"] and "|" not in r["doi"]
               and not any(c.isspace() for c in r["doi"])]
    log(f"[oaid] {len(rows):,} candidates; {len(targets):,} have a usable DOI")

    ids = {}
    if DEST.exists():
        try:
            ids = json.loads(DEST.read_text(encoding="utf-8")) or {}
            log(f"[oaid] {len(ids):,} already cached")
        except Exception as e:                                # noqa: BLE001
            raise SystemExit(
                f"[oaid] {DEST} exists but will not parse ({type(e).__name__}). "
                f"REFUSING to overwrite it -- move it aside to start fresh.")

    todo = [r for r in targets if r["uid"] not in ids]
    if args.limit:
        todo = todo[:args.limit]
    log(f"[oaid] {len(todo):,} to fetch in {(len(todo)+PER-1)//PER:,} requests")
    if not todo:
        return 0

    oa_auth.preflight(log)
    fails = collections.Counter()
    got = 0
    prog = Progress((len(todo) + PER - 1) // PER, "oaid", every_s=60)
    for i in range(0, len(todo), PER):
        chunk = todo[i:i + PER]
        by_doi = {r["doi"].lower(): r for r in chunk}
        try:
            rr = requests.get(
                API, headers=oa_auth.headers({"User-Agent": "quant-digest/1.0"}),
                params={"filter": "doi:" + "|".join(
                            "https://doi.org/" + d for d in by_doi),
                        "select": "doi,id", "per-page": PER}, timeout=90)
        except Exception as e:                                # noqa: BLE001
            fails[type(e).__name__] += 1
            prog.tick()
            continue
        if not rr.ok:
            fails[rr.status_code] += 1
            prog.tick()
            continue
        for w in (rr.json().get("results") or []):
            d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
            row = by_doi.get(d)
            if row and w.get("id"):
                ids[row["uid"]] = _bare(w["id"])
                got += 1
        if (i // PER) % 20 == 19:
            DEST.write_text(json.dumps(ids), encoding="utf-8")
        prog.tick()
        time.sleep(args.pause)
    prog.done()
    DEST.write_text(json.dumps(ids), encoding="utf-8")

    log(f"[oaid] resolved {got:,} this run; {len(ids):,} cached total "
        f"({100*len(ids)/max(1,len(targets)):.1f}%)")
    if fails:
        log(f"[oaid] !! {sum(fails.values()):,} requests FAILED: {dict(fails)}")
    log(f"[oaid] written to {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
