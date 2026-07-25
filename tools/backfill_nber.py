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


# only chase the published version for papers already citeable enough to
# matter for the classics list -- keeps the title-search count (and rate-limit
# pressure) low. A paper with 5 WP-DOI cites isn't a classic either way.
_PUBLISHED_LOOKUP_MIN = 20


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
    """Attach OpenAlex citation counts (in place). Fast bulk pass by NBER DOI
    (10.3386/w<n>, <=50/request, with 429 backoff), then a published-version
    title lookup ONLY for candidates already >= _PUBLISHED_LOOKUP_MIN cites --
    so the expensive per-paper searches stay few and don't trip rate limits.
    Citations are the dominant classics signal; cites_per_year compares
    vintages fairly."""
    doi_to_row = {f"10.3386/{r['wp']}": r for r in rows if r.get("wp")}
    dois = list(doi_to_row)
    for i in range(0, len(dois), 50):
        chunk = dois[i:i + 50]
        data = _oa_get({"filter": "doi:" + "|".join(
            "https://doi.org/" + d for d in chunk),
            "select": "doi,cited_by_count,publication_year", "per-page": 50}, log)
        if data is None:
            log(f"    [cites] DOI batch {i // 50} gave up after retries")
        for w in (data or {}).get("results", []):
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            row = doi_to_row.get(doi)
            if row:
                _set_cites(row, w.get("cited_by_count"), w.get("publication_year"))
        time.sleep(0.4)
    # published-version pass, candidates only (keeps title searches to a few %)
    cand = [r for r in rows if (r.get("cites") or 0) >= _PUBLISHED_LOOKUP_MIN]
    for row in cand:
        _published_cites(row, log)
        time.sleep(0.25)
    if cand:
        log(f"    [cites] checked published version for {len(cand)} candidates")


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
