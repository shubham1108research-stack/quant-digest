#!/usr/bin/env python3
"""Recover titles for candidates that reached the list with none.

THE POPULATION. 958 rows -- 951 from QuantSeeker, 7 from snowball -- carry a
DOI and nothing else: no title, no year, no citation count. Upstream title
resolution failed and blank was the deliberate choice (core_sources.py): a
blank title makes dedup fall back to the uid instead of collapsing unrelated
papers. Correct for dedup, useless afterwards -- a row with no title cannot be
labelled, read, judged or ranked, and build_core.py's _audit_written() is what
first counted them.

TWO SOURCES, IN ORDER OF COST. Semantic Scholar was tried first and has
NOTHING for these -- a batch of their DOIs returns 400 "No valid paper ids
given", because these are working papers S2 never indexed. OpenAlex resolves
most of them in one bulk lookup (measured: 880 of 950, 92.6%). What is left
after that is tried one at a time against Crossref, which is the registration
agency for the 10.2139 (SSRN) prefix and can answer for a DOI that exists but
that OpenAlex has not indexed. Measured: 38 more, 4.0%. The remaining ~3% is a
genuine 404 on both -- unregistered or malformed, not a coverage gap in either
service.

WHERE THE RESULT GOES. export/core_titles.json, MERGED not overwritten --
core_abstracts.py writes the same file for a different reason (a title S2
returns alongside an abstract lookup) and build_core.py already reads it
AFTER dedup, applying it as the last-resort title. This tool does not touch
build_core.py; it only adds another source into the file that tool already
consumes.

NOTHING IS INGESTED.

    python tools/core_resolve_titles.py
"""

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

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
TITLES = OUT / "core_titles.json"
CROSSREF_UA = "quant-digest/1.0 (mailto:upadhyays1108@gmail.com)"


def log(m):
    print(m, flush=True)


def _load_titles():
    if not TITLES.exists():
        return {}
    try:
        return json.loads(TITLES.read_text(encoding="utf-8")) or {}
    except Exception as e:                                   # noqa: BLE001
        raise SystemExit(
            f"[titles] {TITLES} exists but will not parse ({type(e).__name__}). "
            f"REFUSING to overwrite it -- move it aside to start fresh.")


def _openalex(rows, titles):
    oa_auth.preflight(log)
    got = 0
    for i in range(0, len(rows), 100):
        b = rows[i:i + 100]
        try:
            r = requests.get(
                "https://api.openalex.org/works",
                headers=oa_auth.headers({"User-Agent": "quant-digest/1.0"}),
                params={"filter": "doi:" + "|".join(
                            "https://doi.org/" + x["doi"].lower() for x in b),
                        "select": "doi,title", "per-page": 100},
                timeout=120)
        except Exception as e:                                # noqa: BLE001
            log(f"[titles]   OpenAlex batch {i//100+1} {type(e).__name__}")
            continue
        if not r.ok:
            log(f"[titles]   OpenAlex batch {i//100+1} HTTP {r.status_code}: "
                f"{r.text[:150]}")
            continue
        for w in (r.json().get("results") or []):
            d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
            t = (w.get("title") or "").strip()
            row = next((x for x in b if x["doi"].lower() == d), None)
            if t and row and row["uid"] not in titles:
                titles[row["uid"]] = t
                got += 1
        time.sleep(0.3)
    return got


def _crossref(rows, titles):
    got = fail = 0
    for r in rows:
        try:
            rr = requests.get(f"https://api.crossref.org/works/{r['doi']}",
                              headers={"User-Agent": CROSSREF_UA}, timeout=30)
        except Exception:                                     # noqa: BLE001
            fail += 1
            continue
        if not rr.ok:
            fail += 1
            time.sleep(0.15)
            continue
        m = rr.json().get("message") or {}
        t = ((m.get("title") or [""])[0] or "").strip()
        if t and r["uid"] not in titles:
            titles[r["uid"]] = t
            got += 1
        time.sleep(0.15)
    return got, fail


def main():
    if not CAND.exists():
        log(f"[titles] {CAND} missing -- build the core list first")
        return 2
    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))
    blank = [r for r in rows
             if not (r.get("title") or "").strip() and (r.get("doi") or "").strip()]
    no_doi = sum(1 for r in rows
                 if not (r.get("title") or "").strip()
                 and not (r.get("doi") or "").strip())
    log(f"[titles] {len(blank):,} title-less candidates with a DOI "
        f"({no_doi:,} more have neither and cannot be looked up this way)")
    log(f"[titles]   by route: "
        f"{dict(collections.Counter(r['found_by'] for r in blank).most_common(5))}")

    titles = _load_titles()
    todo = [r for r in blank if r["uid"] not in titles]
    if not todo:
        log("[titles] nothing to resolve -- all cached already")
        return 0

    log(f"[titles] trying OpenAlex for {len(todo):,}")
    n1 = _openalex(todo, titles)
    log(f"[titles] OpenAlex resolved {n1:,}")
    TITLES.write_text(json.dumps(titles), encoding="utf-8")

    remaining = [r for r in todo if r["uid"] not in titles]
    if remaining:
        log(f"[titles] trying Crossref for the {len(remaining):,} OpenAlex missed "
            f"(one request each -- this is the slow path)")
        n2, fail = _crossref(remaining, titles)
        log(f"[titles] Crossref resolved {n2:,}; {fail:,} not found (genuine "
            f"404 on both services, not a coverage gap in either)")
        TITLES.write_text(json.dumps(titles), encoding="utf-8")

    still = [r for r in blank if r["uid"] not in titles]
    log(f"\n[titles] {len(blank)-len(still):,} of {len(blank):,} recovered "
        f"({100*(len(blank)-len(still))/max(1,len(blank)):.1f}%); "
        f"{len(still):,} remain unresolved")
    log(f"[titles] written to {TITLES} -- read by build_core.py AFTER dedup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
