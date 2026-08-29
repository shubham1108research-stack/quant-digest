#!/usr/bin/env python3
"""Fetch abstracts for candidates that no route could label.

THE POPULATION THIS EXISTS FOR. 15,440 candidates carry no tag at all, and
they are not marginal papers -- the set includes "Returns to Buying Winners and
Selling Losers", "Common risk factors in the returns on stocks and bonds" and
"The Pricing of Options and Corporate Liabilities". They come entirely from the
seven routes that find papers by means other than words:

    authors 5,999 · snowball 4,735 · pwb 1,255 · quantseeker 1,170 · nber 879

A paper found through an author's back catalogue or a citation edge arrives
with no search phrase attached, so the taxonomy has nothing to inherit and the
labeller falls back to matching the TITLE. That fails here, and no amount of
extra vocabulary fixes it: S2 itself retrieves on title AND abstract, and only
27.3% of swept papers contain their own term in their title. The labeller has
been doing a harder job than the retriever with less information.

WHY THIS IS CHEAP. S2's batch endpoint returns abstracts 500 ids at a time, so
15,440 papers cost ~31 requests. The sweep already keeps the abstracts it
fetches (core_sources.py); this is the same idea for the routes that never
fetched one.

COVERAGE IS MEASURED, NOT ASSUMED. S2 withholds some abstracts for licensing
reasons, so the run reports the hit rate. If it comes back low, that is the
answer about this approach rather than a reason to retry it differently.

NOTHING IS INGESTED. Writes export/core_abstracts.json, which build_core reads
the same way it reads the sweep's abstracts.

    python tools/core_abstracts.py            # every untagged candidate
    python tools/core_abstracts.py --limit 500  # coverage sample first
"""

import argparse
import collections
import csv
import io
import json
import os
import pathlib
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from progress import Progress                              # noqa: E402

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
DEST = OUT / "core_abstracts.json"
S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
UA = "quant-digest/1.0"


def log(m):
    print(m, flush=True)


def _s2id(uid):
    if uid.startswith("doi:"):
        return "DOI:" + uid[4:]
    if uid.startswith("arxiv:"):
        return "ARXIV:" + uid[6:]
    return None            # oa:, sig:, t: -- S2 has no handle for these


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="sample this many first -- coverage before commitment")
    args = ap.parse_args()
    if not CAND.exists():
        log(f"[abs] {CAND} missing -- build the core list first")
        return 2

    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))
    untagged = [r for r in rows if not (r.get("tag") or "").strip()]
    targets = [r for r in untagged if _s2id(r["uid"])]
    log(f"[abs] {len(untagged):,} untagged candidates; "
        f"{len(targets):,} have an id S2 accepts "
        f"({len(untagged)-len(targets):,} are oa:/sig:/title-hash and cannot "
        f"be looked up)")
    log(f"[abs] by route: "
        f"{dict(collections.Counter(r['found_by'] for r in targets).most_common(5))}")

    have = {}
    if DEST.exists():
        try:
            have = json.loads(DEST.read_text(encoding="utf-8"))
            log(f"[abs] {len(have):,} already cached")
        except Exception:                                   # noqa: BLE001
            have = {}
    todo = [r for r in targets if r["uid"] not in have]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        log("[abs] nothing to fetch")
        return 0

    key = os.environ.get("S2_API_KEY", "").strip()
    hdr = {"User-Agent": UA}
    if key:
        hdr["x-api-key"] = key
    pause = 1.1 if key else 3.2
    n_req = (len(todo) + 499) // 500
    log(f"[abs] fetching {len(todo):,} in {n_req} requests "
        f"({'key set' if key else 'NO KEY -- slow'})")

    got = miss = 0
    prog = Progress(n_req, "abs", every_s=30)
    for i in range(0, len(todo), 500):
        chunk = todo[i:i + 500]
        ids = [_s2id(r["uid"]) for r in chunk]
        body = None
        for attempt in range(5):
            try:
                rr = requests.post(S2_BATCH, headers=hdr,
                                   params={"fields": "abstract,title"},
                                   json={"ids": ids}, timeout=120)
            except Exception as e:                          # noqa: BLE001
                log(f"[abs]   {type(e).__name__}; retrying")
                time.sleep(5 * (attempt + 1))
                continue
            if rr.status_code == 429:
                time.sleep(6 * (attempt + 1))
                continue
            if not rr.ok:
                # A failed batch is NOT 500 papers without abstracts. Say so,
                # rather than recording an absence that is really an error --
                # that conflation cost this repo a sweep, a PDF route and an
                # OpenAlex stage before it was noticed.
                log(f"[abs]   !! HTTP {rr.status_code}: {rr.text[:120]}")
                break
            body = rr.json()
            break
        if body:
            for rec, hit in zip(chunk, body):
                a = ((hit or {}).get("abstract") or "").strip()
                if a:
                    have[rec["uid"]] = a
                    got += 1
                else:
                    miss += 1
        prog.tick()
        # CHECKPOINT EVERY OTHER BATCH, not every tenth. At one-in-ten a run
        # killed at batch 7 discards 3,500 fetched abstracts and leaves the
        # cache byte-identical, so the next run re-fetches everything and the
        # only evidence of the work is a log that scrolled past. Ten batches is
        # a lot of somebody else's rate limit to be willing to throw away.
        if (i // 500) % 2 == 1:
            DEST.write_text(json.dumps(have), encoding="utf-8")
        time.sleep(pause)
    prog.done()
    DEST.write_text(json.dumps(have), encoding="utf-8")
    seen = got + miss
    log(f"[abs] abstracts found for {got:,} of {seen:,} looked up "
        f"({100*got/max(1,seen):.1f}% coverage); {len(have):,} cached total")
    log(f"[abs] written to {DEST} -- nothing ingested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
