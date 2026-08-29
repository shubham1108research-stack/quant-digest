#!/usr/bin/env python3
"""Fetch the rest of OpenAlex for every candidate -- everything but the topic.

tools/core_topics.py already holds primary_topic + the topic list for 99.0%
of the pool. This is the second half of the same measured field inventory,
deliberately excluding sustainable_development_goals, mesh and the fields S2
covers better (title, publicationTypes, venue are fetched from S2 instead,
where relevant) -- reviewed and cut by hand rather than fetched wholesale:

    keywords                    Wikipedia-entity tags (noisy; kept anyway --
                                 "Investment (military)" on a finance paper is
                                 visible noise, not silent noise)
    abstract_inverted_index     reconstructed to plain text, ~56-57% coverage
    cited_by_count, fwci,
    citation_normalized_percentile, counts_by_year
    open_access.oa_status
    referenced_works            outbound citation edges -- ~39/paper, ~9M for
                                 the pool. THIS IS THE FORWARD-CITATION
                                 PRECONDITION: invert these edges within the
                                 pool and "who cites this seed" falls out with
                                 no further requests.
    related_works               10 OpenAlex-similarity IDs per paper
    authorships                 distilled to name + institution + country;
                                 the verbose raw_affiliation_strings /
                                 author_position / is_corresponding fields are
                                 dropped -- measured to roughly double the
                                 payload for data this project has no use for
    publication_year, type

OUTPUT IS NDJSON, NOT ONE JSON DICT. core_topics.json is 67 MB and a single
dict is fine at that size; this payload measures ~5x larger per paper because
of referenced_works and authorships, and core_abstracts.py's own history is
the reason not to hold something this size in memory and rewrite it whole
every few batches -- that pattern is exactly the "10 batches of somebody
else's rate limit thrown away on interruption" bug fixed there. NDJSON is
append-only: a kill mid-run loses at most the batch in flight, and resuming
means reading existing lines into a set of done uids and skipping them.

OpenAlex work IDs (referenced_works, related_works) are stored bare
("W2013178519"), not as full URLs -- saves ~24 bytes per edge, ~220 MB across
~9M edges, for information the "https://openalex.org/" prefix does not carry.

    python tools/core_openalex_extract.py                 # every DOI-bearing candidate
    python tools/core_openalex_extract.py --limit 2000    # a sample first
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
from sources import _reconstruct_abstract                     # noqa: E402
from progress import Progress                                 # noqa: E402

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
DEST = OUT / "core_openalex_extra.ndjson"
API = "https://api.openalex.org/works"
PER = 100
SELECT = ("doi,keywords,abstract_inverted_index,cited_by_count,fwci,"
          "citation_normalized_percentile,counts_by_year,open_access,"
          "referenced_works,related_works,authorships,publication_year,type")


def log(m):
    print(m, flush=True)


def _bare(work_id):
    """Full OpenAlex URL -> bare id. 'https://openalex.org/W123' -> 'W123'."""
    return (work_id or "").rsplit("/", 1)[-1]


def _load_done():
    """uid -> True for every line already in DEST. NDJSON, so a partial last
    line from an interrupted write is dropped rather than crashing the run."""
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
                continue    # the interrupted partial line -- skip, don't crash
    return done


def _distil(w):
    aa = w.get("authorships") or []
    authors = []
    for a in aa[:12]:                    # cap -- some papers list 40+ authors
        au = a.get("author") or {}
        insts = [i.get("display_name") for i in (a.get("institutions") or [])]
        authors.append({
            "name": au.get("display_name"),
            "inst": [x for x in insts if x][:2],
            "country": (a.get("countries") or [None])[0],
        })
    return {
        "keywords": [k.get("display_name") for k in (w.get("keywords") or [])],
        "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
        "cites": w.get("cited_by_count"),
        "fwci": w.get("fwci"),
        "pctl": (w.get("citation_normalized_percentile") or {}).get("value"),
        "by_year": [{"y": c.get("year"), "n": c.get("cited_by_count")}
                    for c in (w.get("counts_by_year") or [])],
        "oa_status": (w.get("open_access") or {}).get("oa_status"),
        "refs": [_bare(x) for x in (w.get("referenced_works") or [])],
        "related": [_bare(x) for x in (w.get("related_works") or [])],
        "authors": authors,
        "year": w.get("publication_year"),
        "type": w.get("type"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pause", type=float, default=0.15)
    args = ap.parse_args()

    if not CAND.exists():
        raise SystemExit(f"[oax] {CAND} missing -- build the core list first")
    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))
    targets = [r for r in rows if (r.get("doi") or "").strip()]
    log(f"[oax] {len(rows):,} candidates; {len(targets):,} have a DOI")

    done = _load_done()
    todo = [r for r in targets if r["uid"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    log(f"[oax] {len(done):,} already cached; {len(todo):,} to fetch in "
        f"{(len(todo)+PER-1)//PER:,} requests")
    if not todo:
        return 0

    oa_auth.preflight(log)
    fails = collections.Counter()
    first_err = ""
    got = 0
    fh = io.open(DEST, "a", encoding="utf-8")
    prog = Progress((len(todo) + PER - 1) // PER, "oax", every_s=60)
    try:
        for i in range(0, len(todo), PER):
            chunk = todo[i:i + PER]
            by_doi = {r["doi"].lower(): r for r in chunk}
            try:
                rr = requests.get(
                    API, headers=oa_auth.headers({"User-Agent": "quant-digest/1.0"}),
                    params={"filter": "doi:" + "|".join(
                                "https://doi.org/" + d for d in by_doi),
                            "select": SELECT, "per-page": PER}, timeout=120)
            except Exception as e:                            # noqa: BLE001
                fails[type(e).__name__] += 1
                prog.tick()
                continue
            if not rr.ok:
                # A rejected request is NOT "these papers have no data" --
                # counted, reported, and the uids stay OUT of DEST so a
                # re-run retries them.
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
                rec = {"uid": row["uid"], **_distil(w)}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                got += 1
            fh.flush()
            prog.tick()
            time.sleep(args.pause)
    finally:
        fh.close()
    prog.done()

    log(f"[oax] resolved {got:,} this run; "
        f"{len(done)+got:,} cached total ({100*(len(done)+got)/max(1,len(targets)):.1f}%)")
    if fails:
        log(f"[oax] !! {sum(fails.values()):,} requests FAILED: {dict(fails)}")
        if first_err:
            log(f"[oax]    first error: {first_err}")
        log(f"[oax]    those papers are NOT cached -- re-run to retry them")
    log(f"[oax] written to {DEST} (ndjson, append-only) -- nothing ingested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
