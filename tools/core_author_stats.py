#!/usr/bin/env python3
"""What the 175-person roster actually yielded, per author.

THE ROSTER WAS BUILT, APPROVED, HARVESTED AND NEVER REPORTED ON. Route D
reached 171 of the 175 people in data/core_roster.csv and pulled 10,910
papers; nothing since has said how many survived cleaning, which authors
came back empty, or which of them the corpus actually builds on. This joins
export/core_roster_papers.json (which carries an `author` field per paper) to
the master file and answers that.

THE COUNT HAS BEEN QUOTED FOUR DIFFERENT WAYS and this tool refuses to pick
one. data/core_roster.csv has 175 rows; the harvest reached 171 distinct
authors; some number of those have at least one paper surviving in the pool;
and this project has been calling it "174" in conversation for weeks. All
four are reported, and any author with zero papers in the pool is NAMED --
a roster member who silently contributes nothing looks identical to one who
was never harvested, and those are very different problems.

TWO COLUMNS WORTH READING CAREFULLY:

    fwd_citers_total   how much this corpus's OWN later work builds on that
                       author, summed over their in-pool papers. This is not
                       s2_h and does not rank like it: h-index measures a
                       career against all of science, this measures a
                       bibliography against this desk's collection.

    distinct_sleeves   how many desk sleeves their pool papers span. This is
                       the measurement that killed the author-sleeve prior
                       earlier in the project -- authors span a median of 3
                       sleeves, so assigning one sleeve per author was wrong
                       59.6% of the time. Recording it keeps that finding
                       visible instead of relearnable.

Output is data/core_author_stats.csv, not export/ -- 175 rows of our own
measurements with no third-party text, the same test that put
data/core_roster.csv and data/practitioner_sources.csv under version control.

    python tools/core_author_stats.py
"""

import collections
import csv
import io
import json
import pathlib
import statistics
import sys

OUT = pathlib.Path("export")
DATA = pathlib.Path("data")
ROSTER = DATA / "core_roster.csv"
PAPERS = OUT / "core_roster_papers.json"
MASTER = OUT / "core_master.csv"
DEST = DATA / "core_author_stats.csv"

COLS = ["name", "sleeve", "category", "firm", "s2_id", "s2_h", "s2_cites",
        "s2_papers", "needs_review",
        "papers_harvested", "papers_in_pool", "pct_in_pool",
        "downloadable", "has_abstract",
        "pool_cites_total", "pool_cites_median",
        "fwd_citers_total", "pagerank_total",
        "distinct_sleeves", "top_sleeve",
        "top_paper_title", "top_paper_fwd_citers"]


def log(m):
    print(m, flush=True)


def _uid(r):
    if r.get("doi"):
        return "doi:" + r["doi"].lower()
    if r.get("arxiv"):
        return "arxiv:" + r["arxiv"]
    return None


def main():
    for p in (ROSTER, PAPERS, MASTER):
        if not p.exists():
            raise SystemExit(
                f"[authors] {p} missing. REFUSING to run -- without it every "
                f"author would report zero papers, which is indistinguishable "
                f"from a roster that genuinely yielded nothing.")

    roster = list(csv.DictReader(io.open(ROSTER, encoding="utf-8")))
    harvest = json.loads(PAPERS.read_text(encoding="utf-8"))
    master = {r["uid"]: r
              for r in csv.DictReader(io.open(MASTER, encoding="utf-8",
                                              newline=""))}
    log(f"[authors] roster {len(roster)} rows · harvest {len(harvest):,} papers "
        f"· master {len(master):,} papers")

    # author -> [master rows], and author -> harvested count
    harvested = collections.Counter()
    in_pool = collections.defaultdict(list)
    for h in harvest:
        a = (h.get("author") or "").strip()
        if not a:
            continue
        harvested[a] += 1
        u = _uid(h)
        if u and u in master:
            in_pool[a].append(master[u])

    log(f"[authors] harvest reached {len(harvested)} distinct authors; "
        f"{len(in_pool)} have >=1 paper in the pool")

    def _i(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    rows = []
    for m in roster:
        name = (m.get("name") or "").strip()
        mine = in_pool.get(name, [])
        cites = [_i(r.get("cites")) for r in mine if _i(r.get("cites"))]
        sleeves = collections.Counter((r.get("sleeve") or "") for r in mine
                                      if (r.get("sleeve") or ""))
        top = max(mine, key=lambda r: _i(r.get("fwd_citers")), default=None)
        rows.append({
            "name": name,
            "sleeve": m.get("sleeve") or "",
            "category": m.get("category") or "",
            "firm": m.get("firm") or "",
            "s2_id": m.get("s2_id") or "",
            "s2_h": m.get("s2_h") or "",
            "s2_cites": m.get("s2_cites") or "",
            "s2_papers": m.get("s2_papers") or "",
            "needs_review": m.get("needs_review") or "",
            "papers_harvested": harvested.get(name, 0),
            "papers_in_pool": len(mine),
            "pct_in_pool": (round(100 * len(mine) / harvested[name], 1)
                            if harvested.get(name) else ""),
            "downloadable": sum(1 for r in mine if r.get("downloadable") == "1"),
            "has_abstract": sum(1 for r in mine if r.get("has_abstract") == "1"),
            "pool_cites_total": sum(cites),
            "pool_cites_median": (round(statistics.median(cites))
                                  if cites else 0),
            "fwd_citers_total": sum(_i(r.get("fwd_citers")) for r in mine),
            "pagerank_total": round(sum(float(r["pagerank"]) for r in mine
                                        if r.get("pagerank")), 8),
            "distinct_sleeves": len(sleeves),
            "top_sleeve": (sleeves.most_common(1)[0][0] if sleeves else ""),
            "top_paper_title": ((top or {}).get("title") or "")[:120],
            "top_paper_fwd_citers": _i((top or {}).get("fwd_citers")),
        })

    rows.sort(key=lambda r: -r["fwd_citers_total"])
    DEST.parent.mkdir(parents=True, exist_ok=True)
    with io.open(DEST, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)

    # ------------------------------------------------------------- reporting
    tot_pool = sum(r["papers_in_pool"] for r in rows)
    log(f"\n[authors] {tot_pool:,} roster papers in the pool across "
        f"{sum(1 for r in rows if r['papers_in_pool']):,} authors")

    # NAME the empty ones. A roster member contributing nothing and one never
    # harvested look identical in a count and are different problems.
    never = [r["name"] for r in rows if r["papers_harvested"] == 0]
    empty = [r["name"] for r in rows
             if r["papers_harvested"] and not r["papers_in_pool"]]
    if never:
        log(f"[authors] !! {len(never)} roster member(s) NEVER HARVESTED: "
            f"{', '.join(never)}")
    if empty:
        log(f"[authors] !! {len(empty)} harvested but ZERO papers survived "
            f"into the pool: {', '.join(empty)}")

    log(f"\n[authors] top 15 by fwd_citers_total -- whose work THIS corpus "
        f"builds on:\n")
    log(f"    {'fwd':>7} {'papers':>7} {'s2_h':>5}  name")
    for r in rows[:15]:
        log(f"    {r['fwd_citers_total']:>7,} {r['papers_in_pool']:>7,} "
            f"{str(r['s2_h']):>5}  {r['name']}")

    sl = [r["distinct_sleeves"] for r in rows if r["papers_in_pool"]]
    if sl:
        log(f"\n[authors] sleeve spread: median {statistics.median(sl):.0f} "
            f"sleeves per author, {sum(1 for x in sl if x > 1)} of {len(sl)} "
            f"span more than one -- why one sleeve per author was wrong")
    log(f"\n[authors] written to {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
