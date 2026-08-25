#!/usr/bin/env python3
"""Build docs/nber.json -- NBER finance-program working papers by month, back
to the digest floor (config.BACKFILL_FLOOR, 2010-01), for the portal's NBER
tab. Reuses sources.nber() (which filters to NBER's finance programs via the
?facet=programs: API) one calendar month at a time.

    python tools/backfill_nber.py            # full history 2010-01 -> current
    python tools/backfill_nber.py 2026-01    # only from 2026-01 forward

Output shape: {"YYYY-MM": [ {title, authors, url, date, abstract, wp}, ... ]}.
Idempotent: re-running refreshes each month in range. The daily digest keeps
the CURRENT month fresh incrementally (see monthly.update_nber_current), so
this heavy full pull only needs running once (and after a floor change).
NBER's own API isn't rate-limited like OpenAlex, so this runs fine locally.
"""

import calendar
import datetime as dt
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config    # noqa: E402
import sources   # noqa: E402
import store     # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "nber.json")
_UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}
_MAILTO = os.environ.get("CONTACT_EMAIL") or os.environ.get("GMAIL_ADDRESS")


def _wp_num(wp) -> int:
    """'w9999' sorts above 'w31234' as a string. Sort on the number."""
    try:
        return int(str(wp or "").lstrip("w") or 0)
    except ValueError:
        return 0


def _atomic_write(data) -> None:
    """Write via a temp file and os.replace.

    open(OUT, "w") truncates BEFORE json.dump runs, so a crash or OOM
    mid-write left a truncated 1.2 MB tracked file -- which the workflow then
    deployed. This run is 30-90 minutes against a 90-minute ceiling, so it is
    not a hypothetical."""
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, default=str)
    os.replace(tmp, OUT)


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()




def _oa_get(params: dict, log):
    """OpenAlex GET with 429 backoff -- essential when enriching thousands of
    papers, or the whole pass fails and citations come back null."""
    if _MAILTO:
        params = {**params, "mailto": _MAILTO}
    for attempt in range(5):
        try:
            r = requests.get("https://api.openalex.org/works", params=params,
                             headers=_UA, timeout=45)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1) + 1)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:                              # noqa: BLE001
            time.sleep(1.5 * (attempt + 1))
    return None


def _set_cites(row: dict, c, yr) -> None:
    row["cites"] = c
    row["cites_per_year"] = round((c or 0) / max(1, dt.date.today().year - (yr or dt.date.today().year) + 1), 1)


def _published_cites(row: dict, log) -> None:
    """The WP-DOI count undercounts once a paper is published (OpenAlex counts
    citations against the JOURNAL version's DOI) -- e.g. Stambaugh-Yu-Yuan 'The
    Short of It' is ~90 on the WP DOI vs ~1500 published. Title-search the
    published version and take the higher count, guarded by an exact
    normalized-title match. Only called for already-citeable candidates."""
    want = _norm_title(row.get("title", ""))
    if not want:
        return
    data = _oa_get({"search": row["title"], "per-page": 5,
                    "select": "title,cited_by_count,publication_year"}, log)
    for w in (data or {}).get("results", []):
        if _norm_title(w.get("title", "")) != want:
            continue
        c = w.get("cited_by_count") or 0
        if c > (row.get("cites") or 0):
            _set_cites(row, c, w.get("publication_year"))
        break


def _enrich_cites(rows: list[dict], log=print) -> None:
    """Fast, reliable per-month pass: OpenAlex citation counts by NBER DOI
    (10.3386/w<n>, <=50/request, with 429 backoff). Every paper gets a count.
    The published-version correction (which undercounts here for since-published
    papers) is a bounded GLOBAL post-pass in main() -- see _refine_top."""
    doi_to_row = {f"10.3386/{r['wp']}": r for r in rows if r.get("wp")}
    dois = list(doi_to_row)
    for i in range(0, len(dois), 50):
        chunk = dois[i:i + 50]
        data = _oa_get({"filter": "doi:" + "|".join(
            "https://doi.org/" + d for d in chunk),
            "select": "doi,cited_by_count,publication_year", "per-page": 50}, log)
        for w in (data or {}).get("results", []):
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            row = doi_to_row.get(doi)
            if row:
                _set_cites(row, w.get("cited_by_count"), w.get("publication_year"))
        time.sleep(0.4)


# how many top-by-WP-cites papers get the published-version correction (a
# bounded post-pass so it can't blow past OpenAlex rate limits no matter how
# large the corpus). These are exactly the classic candidates.
_REFINE_TOP_N = 300


def _refine_top(data: dict, log=print) -> None:
    """Global post-pass: take the highest WP-DOI-cited papers across ALL months
    and correct each to its published version's (higher) count via a title
    match. Bounded to _REFINE_TOP_N total lookups, so it completes reliably
    even under OpenAlex throttling -- unlike a per-paper pass over thousands."""
    allp = [x for v in data.values() for x in v]
    top = sorted(allp, key=lambda x: x.get("cites") or 0, reverse=True)[:_REFINE_TOP_N]
    for i, row in enumerate(top):
        _published_cites(row, log)
        if i % 50 == 0:
            log(f"    [refine] {i}/{len(top)} published-version lookups", )
        time.sleep(0.25)
    log(f"    [refine] corrected {len(top)} top candidates to published cites")


def _months(start_ym: str, end_ym: str):
    y, m = map(int, start_ym.split("-"))
    ey, em = map(int, end_ym.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def _slim(it: dict) -> dict:
    # uid is what lets the portal treat an NBER card like any other card. Both
    # "near" (the paper's neighbourhood) and "implement" key off it, and
    # without it they did not render at all -- absent rather than disabled,
    # which reads as the feature not existing on this tab.
    return {"uid": store.make_uid(it),
            "title": it["title"], "authors": it["authors"], "url": it["url"],
            "date": it["date"], "abstract": (it.get("abstract") or "")[:400],
            "wp": it.get("nber_wp", ""), "cites": None, "cites_per_year": None}


def build_month(ym: str, log=print) -> list[dict]:
    """Collect one month's NBER Asset Pricing papers (NBER API only -- fast, not
    OpenAlex-throttled). Citation enrichment is a single GLOBAL pass in main(),
    NOT here, so OpenAlex is hit in ~44 bulk batches total instead of ~199
    per-month ones -- far fewer calls, far less rate-limit exposure."""
    y, m = map(int, ym.split("-"))
    start = f"{ym}-01"
    end = f"{ym}-{calendar.monthrange(y, m)[1]:02d}"
    items = sources.nber(log, start=start, end=end)
    by_wp = {}                                 # newest first, deduped by WP number
    for it in items:
        by_wp[it.get("nber_wp") or it["url"]] = _slim(it)
    return sorted(by_wp.values(), key=lambda x: x["wp"], reverse=True)


def main() -> None:
    start_ym = sys.argv[1] if len(sys.argv) > 1 else config.BACKFILL_FLOOR
    now = dt.date.today()
    end_ym = f"{now.year:04d}-{now.month:02d}"

    data = {}
    if os.path.exists(OUT):
        try:
            data = json.load(open(OUT, encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            data = {}

    for ym in _months(start_ym, end_ym):
        try:
            rows = build_month(ym)
        except Exception as e:                         # noqa: BLE001
            print(f"  [{ym}] failed: {type(e).__name__}: {e}", flush=True)
            continue
        # Merge, never replace. `if rows:` meant a month whose fetch
        # transiently returned [] silently kept stale content forever, and a
        # partial fetch overwrote a complete month -- the docstring's
        # "idempotent: re-running refreshes each month" was false in the one
        # direction that matters.
        if rows:
            merged = {x.get("wp"): x for x in data.get(ym, [])}
            merged.update({x.get("wp"): x for x in rows})
            data[ym] = sorted(merged.values(),
                              key=lambda x: _wp_num(x.get("wp")), reverse=True)
        print(f"  {ym}: {len(rows)} papers", flush=True)
        _atomic_write(data)          # checkpoint: a timeout keeps the months done so far

    # ONE global citation pass over every collected paper (~44 bulk DOI batches
    # for the whole corpus, not per-month), then the bounded top-N refinement
    all_rows = [x for v in data.values() for x in v]
    print(f"enriching citations for {len(all_rows)} papers...", flush=True)
    _enrich_cites(all_rows, print)
    _refine_top(data)

    _atomic_write(data)
    total = sum(len(v) for v in data.values())
    print(f"\nwrote {OUT}: {len(data)} months, {total} papers")


if __name__ == "__main__":
    main()
