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
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config    # noqa: E402
import sources   # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "nber.json")


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
            "wp": it.get("nber_wp", "")}


def build_month(ym: str, log=print) -> list[dict]:
    y, m = map(int, ym.split("-"))
    start = f"{ym}-01"
    end = f"{ym}-{calendar.monthrange(y, m)[1]:02d}"
    items = sources.nber(log, start=start, end=end)
    # newest first within the month, deduped by working-paper number
    by_wp = {}
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
        if rows:
            data[ym] = rows
        print(f"  {ym}: {len(rows)} papers", flush=True)

    json.dump(data, open(OUT, "w", encoding="utf-8"), default=str)
    total = sum(len(v) for v in data.values())
    print(f"\nwrote {OUT}: {len(data)} months, {total} papers")


if __name__ == "__main__":
    main()
