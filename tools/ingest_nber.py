#!/usr/bin/env python3
"""Ingest docs/nber.json into the archive as first-class items.

Same defect the classics had, at four times the scale: docs/nber.json holds
2,210 NBER Asset Pricing working papers and only 178 are in the items table.
The other 2,032 are display-only -- browsable in the NBER tab, invisible to Ask,
to scoring, and to the sleeve labels.

Cheap to fix, because nber.json already carries everything needed: title,
authors, date, abstract (100% coverage) and the working-paper number, which
gives a deterministic DOI (10.3386/wN) and PDF path. No external lookups, so
nothing to throttle -- unlike the classics ingest, this runs offline.

  python tools/ingest_nber.py --dry-run
  python tools/ingest_nber.py
"""

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store  # noqa: E402

SRC = pathlib.Path("docs/nber.json")


def log(m):
    print(m, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    nb = json.loads(SRC.read_text(encoding="utf-8"))
    months = [k for k in nb if re.match(r"^\d{4}-\d{2}$", k)]
    papers = [p for k in sorted(months) for p in nb[k]]
    log(f"[nber] {len(papers)} papers across {len(months)} months")

    con = store.connect()
    built = []
    for p in papers:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        wp = str(p.get("wp") or "").strip().lstrip("w")
        item = {
            "title": title,
            "authors": p.get("authors", ""),
            "date": p.get("date", ""),
            "url": p.get("url", ""),
            "abstract": (p.get("abstract") or "").strip()[:6000],
            "source": "NBER",
            "section": "2",              # working papers / preprints
            "tier": "T2",
            "cites": p.get("cites"),
            "cites_per_year": p.get("cites_per_year"),
        }
        if wp.isdigit():
            # deterministic identity AND a deterministic PDF path downstream
            item["doi"] = f"10.3386/w{wp}"
            item["wp"] = f"w{wp}"
            if not item["url"]:
                item["url"] = f"https://www.nber.org/papers/w{wp}"
        built.append(item)

    if args.limit:
        built = built[:args.limit]

    fresh = store.filter_new(con, built)
    with_abs = sum(1 for f in fresh if f.get("abstract"))
    with_doi = sum(1 for f in fresh if f.get("doi"))
    log(f"[nber] {len(built)} candidates, {len(fresh)} new to the archive")
    log(f"[nber]   with abstract: {with_abs}  with DOI: {with_doi}")

    if args.dry_run:
        log("[nber] dry run -- nothing written")
        for f in fresh[:5]:
            log(f"   {f.get('doi','(none)'):<18} {f['title'][:62]}")
        return

    store.save(con, fresh)
    total = con.execute("SELECT count(*) FROM items").fetchone()[0]
    log(f"[nber] inserted {len(fresh)}; archive now {total} papers")


if __name__ == "__main__":
    main()
