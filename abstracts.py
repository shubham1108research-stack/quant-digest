"""Semantic Scholar batch lookup -- shared by the live digest (sources.py) and
the history backfill (backfill.py). One POST to /paper/batch returns, for up to
500 DOIs at a time, the abstract, the paper's citation count, and the authors'
h-indexes -- feeding three signals (abstract text, paper_cites, author_cites) in
a single call. Free/unauthenticated; a S2_API_KEY env var is used transparently
if present. Best-effort: any error just yields fewer results.

Kept dependency-light (only `requests`) so backfill.py can import it without
pulling in sources.py's feedparser dependency.
"""

import os
import time

import requests

_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
_FIELDS = "externalIds,abstract,citationCount,authors.hIndex,year"
_UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}
_BATCH_SIZE = 500        # S2 hard cap per batch request
_MAX_RETRIES = 4


def _headers() -> dict:
    h = dict(_UA)
    key = os.environ.get("S2_API_KEY")
    if key:
        h["x-api-key"] = key
    return h


def s2_papers(dois, log=print, batch: int = _BATCH_SIZE) -> dict[str, dict]:
    """Return {doi_lower: {"abstract": str, "cites": int|None, "author_h": int|None}}
    for the given DOIs via Semantic Scholar's batch endpoint. Missing papers and
    missing fields are simply absent. Never raises."""
    uniq = list(dict.fromkeys(d.lower() for d in dois if d))
    out: dict[str, dict] = {}
    for i in range(0, len(uniq), min(batch, _BATCH_SIZE)):
        chunk = uniq[i:i + min(batch, _BATCH_SIZE)]
        ids = [f"DOI:{d}" for d in chunk]
        results = None
        for attempt in range(_MAX_RETRIES):
            try:
                r = requests.post(_BATCH, params={"fields": _FIELDS},
                                  json={"ids": ids}, headers=_headers(), timeout=90)
            except Exception as e:                    # noqa: BLE001
                log(f"[s2] batch {i // batch} network error: {type(e).__name__}")
                break
            if r.status_code == 429:
                wait = int(float(r.headers.get("retry-after", 0))) or 5 * (attempt + 1)
                time.sleep(min(wait, 60))
                continue
            if r.status_code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:                  # 400/404 -> bad ids, skip chunk
                log(f"[s2] batch {i // batch} HTTP {r.status_code}; skipped")
                break
            try:
                results = r.json()
            except Exception:                         # noqa: BLE001
                results = None
            break
        for res in results or []:
            if not res:
                continue
            doi = ((res.get("externalIds") or {}).get("DOI") or "").lower()
            if not doi:
                continue
            hs = [a.get("hIndex") for a in (res.get("authors") or [])
                  if a.get("hIndex") is not None]
            out[doi] = {
                "abstract": res.get("abstract") or "",
                "cites": res.get("citationCount"),
                "author_h": max(hs) if hs else None,
                "year": res.get("year"),
            }
        time.sleep(0.5)
    return out
