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

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "nber.json")
_UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}
_MAILTO = os.environ.get("CONTACT_EMAIL") or os.environ.get("GMAIL_ADDRESS")


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _published_cites(row: dict) -> None:
    """Most NBER papers are later published in a journal, and OpenAlex counts
    citations against THAT version's DOI -- so the working-paper DOI count
    severely undercounts (e.g. Stambaugh-Yu-Yuan 'The Short of It' shows ~90
    on the WP DOI vs ~1500 published). Title-search the published version and
    take the higher count. Guarded by a strong title match to avoid grabbing a
    different paper's citations."""
    want = _norm_title(row.get("title", ""))
    if not want:
        return
    params = {"search": row["title"], "per-page": 5,
              "select": "title,cited_by_count,publication_year,type"}
    if _MAILTO:
        params["mailto"] = _MAILTO
    try:
        r = requests.get("https://api.openalex.org/works", params=params,
                         headers=_UA, timeout=30)
        r.raise_for_status()
        for w in r.json().get("results", []):
            if _norm_title(w.get("title", "")) != want:     # exact normalized match only
                continue
            c = w.get("cited_by_count") or 0
            if c > (row.get("cites") or 0):
                row["cites"] = c
                yr = w.get("publication_year") or dt.date.today().year
                row["cites_per_year"] = round(
                    c / max(1, dt.date.today().year - yr + 1), 1)
            break
    except Exception:                                       # noqa: BLE001
        pass


def _enrich_cites(rows: list[dict], log=print) -> None:
    """Attach OpenAlex citation counts (in place). First the fast bulk pass by
    NBER DOI (10.3386/w<n>, <=50/request), then -- because the WP DOI badly
    undercounts once a paper is published -- a per-paper title lookup that
    takes the published version's higher count. Citations are the dominant
    'is this a classic?' signal; cites_per_year fairly compares vintages."""
    doi_to_row = {f"10.3386/{r['wp']}": r for r in rows if r.get("wp")}
    dois = list(doi_to_row)
    now_year = dt.date.today().year
    for i in range(0, len(dois), 50):
        chunk = dois[i:i + 50]
        params = {"filter": "doi:" + "|".join("https://doi.org/" + d for d in chunk),
                  "select": "doi,cited_by_count,publication_year", "per-page": 50}
        if _MAILTO:
            params["mailto"] = _MAILTO
        try:
            r = requests.get("https://api.openalex.org/works", params=params,
                             headers=_UA, timeout=45)
            r.raise_for_status()
            for w in r.json().get("results", []):
                doi = (w.get("doi") or "").replace("https://doi.org/", "")
                row = doi_to_row.get(doi)
                if not row:
                    continue
                c = w.get("cited_by_count")
                row["cites"] = c
                yr = w.get("publication_year") or now_year
                row["cites_per_year"] = round((c or 0) / max(1, now_year - yr + 1), 1)
        except Exception as e:                         # noqa: BLE001
            log(f"    [cites] DOI batch failed: {type(e).__name__}")
        time.sleep(0.3)
    # published-version pass (title match); catches the journal citations
    for row in rows:
        _published_cites(row)
        time.sleep(0.12)


def _months(start_ym: str, end_ym: str):
    y, m = map(int, start_ym.split("-"))
    ey, em = map(int, end_ym.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def _slim(it: dict) -> dict:
    return {"title": it["title"], "authors": it["authors"], "url": it["url"],
            "date": it["date"], "abstract": (it.get("abstract") or "")[:400],
            "wp": it.get("nber_wp", ""), "cites": None, "cites_per_year": None}


def build_month(ym: str, log=print) -> list[dict]:
    y, m = map(int, ym.split("-"))
    start = f"{ym}-01"
    end = f"{ym}-{calendar.monthrange(y, m)[1]:02d}"
    items = sources.nber(log, start=start, end=end)
    # newest first within the month, deduped by working-paper number
    by_wp = {}
    for it in items:
        by_wp[it.get("nber_wp") or it["url"]] = _slim(it)
    rows = sorted(by_wp.values(), key=lambda x: x["wp"], reverse=True)
    _enrich_cites(rows, log)               # citation counts -> classics signal
    return rows


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
        if rows:
            data[ym] = rows
        print(f"  {ym}: {len(rows)} papers", flush=True)

    json.dump(data, open(OUT, "w", encoding="utf-8"), default=str)
    total = sum(len(v) for v in data.values())
    print(f"\nwrote {OUT}: {len(data)} months, {total} papers")


if __name__ == "__main__":
    main()
