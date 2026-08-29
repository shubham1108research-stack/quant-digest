#!/usr/bin/env python3
"""Fetch what S2 has that OpenAlex does not: influentialCitationCount, TLDR,
an open-access PDF link, and the SPECTER v2 embedding.

fieldsOfStudy/s2FieldsOfStudy, publicationTypes and venue are deliberately
excluded -- reviewed and cut, not overlooked. abstract and title are also
skipped even though the endpoint returns them: OpenAlex already gives a
better abstract (56-57% vs S2's measured 16.5% for this pool), and titles
already exist except for the 40 rows tools/core_resolve_titles.py could not
place -- that is a different, already-solved problem, not this tool's job.

influentialCitationCount IS THE REASON TO RUN THIS AT ALL. It has no OpenAlex
equivalent (fwci and citation_normalized_percentile measure something else --
field-relative rate, not "is this citation substantive"), and it was 100%
present whenever S2 had the paper in every sample pulled during this session.

SPECTER v2 is kept, with the accuracy already measured on this project's own
data attached to the field so it is never mistaken for something stronger:
45% term-level, 67% sleeve-level accuracy (measured on Returns to Buying
Winners and Selling Losers -- top centroid match was "long-term reversal",
the WRONG effect, at a top1-top2 gap of 0.0006). Good for a coarse pre-filter
or a sleeve prior; not for picking one of 402 taxonomy terms.

OUTPUT, TWO FILES. The scalar fields go to export/core_s2_extra.ndjson,
append-only for the same reason core_openalex_extract.py uses it -- a kill
mid-run must lose at most the batch in flight. The embedding goes SEPARATELY
to export/core_s2_specter.npy + export/core_s2_specter_uids.json (float32,
uids[i] rows to V[i]) because 768 floats as JSON text is roughly 10x its
binary size for no benefit; this supersedes the earlier 12,086-paper sample
in export/core_specter.npy with coverage across the full pool.

    python tools/core_s2_extract.py                 # every DOI/arXiv candidate
    python tools/core_s2_extract.py --limit 2000    # a sample first
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

import numpy as np
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from progress import Progress                                 # noqa: E402

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
DEST = OUT / "core_s2_extra.ndjson"
VEC = OUT / "core_s2_specter.npy"
VEC_UIDS = OUT / "core_s2_specter_uids.json"
S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
UA = "quant-digest/1.0"
PER = 500       # S2 batch max; measured payload with the embedding included
               # is well under any practical response-size limit at this size
FIELDS = "citationCount,influentialCitationCount,referenceCount,tldr,openAccessPdf,embedding.specter_v2"


def log(m):
    print(m, flush=True)


def _s2id(uid):
    if uid.startswith("doi:"):
        return "DOI:" + uid[4:]
    if uid.startswith("arxiv:"):
        return "ARXIV:" + uid[6:]
    return None


def _load_done():
    done = set()
    if not DEST.exists():
        return done
    with io.open(DEST, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["uid"])
            except Exception:                                # noqa: BLE001
                continue
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not CAND.exists():
        raise SystemExit(f"[s2x] {CAND} missing -- build the core list first")
    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))
    targets = [r for r in rows if _s2id(r["uid"])]
    log(f"[s2x] {len(rows):,} candidates; {len(targets):,} have an id S2 "
        f"accepts ({len(rows)-len(targets):,} are oa:/sig:/title-hash and "
        f"cannot be looked up)")

    done = _load_done()
    todo = [r for r in targets if r["uid"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    log(f"[s2x] {len(done):,} already cached; {len(todo):,} to fetch in "
        f"{(len(todo)+PER-1)//PER:,} requests")
    if not todo:
        return 0

    key = os.environ.get("S2_API_KEY", "").strip()
    hdr = {"User-Agent": UA}
    if key:
        hdr["x-api-key"] = key
    pause = 1.1 if key else 3.2
    log(f"[s2x] {'key set' if key else 'NO KEY -- slow'}")

    fails = collections.Counter()
    rate_limited = [0]
    got = 0
    vec_uids, vec_rows = [], []
    fh = io.open(DEST, "a", encoding="utf-8")
    prog = Progress((len(todo) + PER - 1) // PER, "s2x", every_s=60)
    try:
        for i in range(0, len(todo), PER):
            chunk = todo[i:i + PER]
            ids = [_s2id(r["uid"]) for r in chunk]
            body = None
            for attempt in range(5):
                try:
                    rr = requests.post(S2_BATCH, headers=hdr,
                                       params={"fields": FIELDS},
                                       json={"ids": ids}, timeout=180)
                except Exception as e:                        # noqa: BLE001
                    log(f"[s2x]   {type(e).__name__}; retrying")
                    time.sleep(5 * (attempt + 1))
                    continue
                if rr.status_code == 429:
                    time.sleep(6 * (attempt + 1))
                    continue
                if not rr.ok:
                    fails[rr.status_code] += 1
                    log(f"[s2x]   !! HTTP {rr.status_code}: {rr.text[:150]}")
                    break
                body = rr.json()
                break
            else:
                # Retries exhausted without a break -- five 429s in a row.
                # NOT "500 papers with no data": counted separately so a
                # rate-limited run cannot be read as a coverage result.
                rate_limited[0] += len(chunk)
                log(f"[s2x]   !! batch {i//PER+1} ABANDONED after 5 attempts "
                    f"-- {len(chunk)} papers NOT looked up")
            if body:
                for rec, hit in zip(chunk, body):
                    if not hit:
                        continue
                    fh.write(json.dumps({
                        "uid": rec["uid"],
                        "cites": hit.get("citationCount"),
                        "influential": hit.get("influentialCitationCount"),
                        "refs": hit.get("referenceCount"),
                        "tldr": (hit.get("tldr") or {}).get("text"),
                        "pdf_url": (hit.get("openAccessPdf") or {}).get("url"),
                    }, ensure_ascii=False) + "\n")
                    got += 1
                    vec = (hit.get("embedding") or {}).get("vector")
                    if vec:
                        vec_uids.append(rec["uid"])
                        vec_rows.append(vec)
            fh.flush()
            prog.tick()
            time.sleep(pause)
    finally:
        fh.close()
    prog.done()

    if vec_rows:
        old_uids, old_V = [], None
        if VEC.exists() and VEC_UIDS.exists():
            try:
                old_uids = json.loads(VEC_UIDS.read_text(encoding="utf-8"))
                old_V = np.load(VEC)
            except Exception:                                # noqa: BLE001
                old_uids, old_V = [], None
        new_uids = old_uids + vec_uids
        new_V = (np.vstack([old_V, np.array(vec_rows, dtype=np.float32)])
                 if old_V is not None else np.array(vec_rows, dtype=np.float32))
        np.save(VEC, new_V)
        VEC_UIDS.write_text(json.dumps(new_uids), encoding="utf-8")
        log(f"[s2x] {len(vec_rows):,} embeddings appended; "
            f"{len(new_uids):,} total in {VEC}")

    log(f"[s2x] resolved {got:,} this run; "
        f"{len(done)+got:,} cached total ({100*(len(done)+got)/max(1,len(targets)):.1f}%)")
    if fails:
        log(f"[s2x] !! {sum(fails.values()):,} requests FAILED: {dict(fails)}")
    if rate_limited[0]:
        log(f"[s2x] !! {rate_limited[0]:,} papers never looked up -- batches "
            f"abandoned on repeated 429. Set S2_API_KEY and re-run.")
    log(f"[s2x] written to {DEST} (ndjson, append-only) -- nothing ingested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
