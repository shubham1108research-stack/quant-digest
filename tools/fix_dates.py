#!/usr/bin/env python3
"""One-off: re-pad the unpadded dates sources._crossref_item used to write.

Crossref returns `date-parts` as integers, and the collector joined them with
"-" -- producing "2026-8-3" rather than "2026-08-03". Everything downstream
derives a month key with str(date)[:7], which for those rows yields "2026-8-",
a string that can never equal the "%Y-%m" the monthly composite looks for.

Measured before the fix: 3,978 of 11,583 rows (34%) -- every SSRN item, every
journal:* feed and the Crossref watchlist pull. docs/monthly.json for the
current month contained no journal or SSRN papers at all, in a digest whose
whole premise is ranking journal research. It also broke lexicographic date
sorting, where "2026-7-3" sorts above "2026-10-01".

The collector is fixed; this repairs the rows already written.

  python tools/fix_dates.py --dry-run
  python tools/fix_dates.py
"""

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store   # noqa: E402

UNPADDED = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def log(m):
    print(m, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = store.connect()
    fixes = []
    for uid, meta in con.execute("SELECT uid, meta FROM items"):
        try:
            d = json.loads(meta)
        except Exception:                                # noqa: BLE001
            continue
        m = UNPADDED.match(str(d.get("date") or ""))
        if not m:
            continue
        y, mo, dy = m.groups()
        iso = f"{int(y):04d}-{int(mo):02d}-{int(dy):02d}"
        if iso != d["date"]:
            fixes.append((uid, d["date"], iso))

    log(f"[dates] {len(fixes)} rows need repadding")
    if not fixes:
        return
    for uid, old, new in fixes[:5]:
        log(f"    {old:>12}  ->  {new}   {uid[:44]}")
    months = sorted({f[2][:7] for f in fixes})
    log(f"[dates] months affected: {', '.join(months)}")

    if args.dry_run:
        log("[dates] dry run -- nothing written")
        return

    # update_meta drops falsy values but a date string is never falsy here, and
    # it touches meta only -- the date lives nowhere else.
    n = 0
    for uid, _, iso in fixes:
        if store.update_meta(con, uid, {"date": iso}):
            n += 1
    con.commit()
    log(f"[dates] rewrote {n} rows")

    left = sum(1 for (meta,) in con.execute("SELECT meta FROM items")
               if UNPADDED.match(str((json.loads(meta) or {}).get("date") or ""))
               and not re.match(r"^\d{4}-\d{2}-\d{2}$",
                                str((json.loads(meta) or {}).get("date") or "")))
    log(f"[dates] unpadded remaining: {left}")


if __name__ == "__main__":
    main()
