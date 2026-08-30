#!/usr/bin/env python3
"""Authors across the whole corpus, with real h-indices.

WHY A NAME IS NOT AN AUTHOR. core_master.ndjson holds author NAMES (the
extract kept name/inst/country and dropped the ids), and grouping 549,216
author slots by name gives 241,843 "authors" that are wrong in both
directions: every J. Wang in the corpus merges into one person, while
"Kenneth R. French" and "Kenneth French" split into two. The roster work
already measured what that costs -- exact-name matching scored Kenneth French
at ZERO against a bibliography of 55 papers. So this fetches OpenAlex author
IDs and groups on those.

TWO PHASES, because they have very different costs.

    --ids     re-fetch every paper's authorships WITH author ids.
              ~2,300 requests, the same shape as core_openalex_ids.py.
              Writes export/core_authorships.ndjson.

    --build   group by author id, compute the IN-CORPUS h-index, keep the
              authors who clear --min-papers, then fetch REAL h-indices for
              only those from OpenAlex /authors, 100 at a time. Filtering
              first is the point: 241,843 names is ~2,400 requests, while
              the ~20,000 authors with 5+ papers is ~200.

TWO DIFFERENT H-INDICES, AND THE DISTINCTION MATTERS. `h_corpus` is computed
over this pool only -- how much of an author's impact lives in YOUR corpus.
`h_openalex` is their real, globally-quoted h-index. Fama's h_corpus is 78
and his true h is far higher, because most of his work is not in this pool.
Neither is wrong; they answer different questions, so both are columns.

    python tools/core_authors.py --ids
    python tools/core_authors.py --build --min-papers 5
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
from progress import Progress                                 # noqa: E402

OUT = pathlib.Path("export")
DATA = pathlib.Path("data")
CAND = OUT / "core_candidates.csv"
MASTER = OUT / "core_master.csv"
AUTHORSHIPS = OUT / "core_authorships.ndjson"
DEST = DATA / "core_authors.csv"
WORKS_API = "https://api.openalex.org/works"
AUTHORS_API = "https://api.openalex.org/authors"
PER = 100

COLS = ["author_id", "name", "n_papers", "h_corpus", "cites_corpus",
        "h_openalex", "works_openalex", "cites_openalex",
        "institution", "country", "top_sleeve", "distinct_sleeves",
        "fwd_citers_total", "top_paper_title", "top_paper_cites"]


def log(m):
    print(m, flush=True)


def _bare(x):
    return (x or "").rsplit("/", 1)[-1]


def cmd_ids(args):
    if not CAND.exists():
        raise SystemExit(f"[auth] {CAND} missing")
    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))
    targets = [r for r in rows if (r.get("doi") or "").strip()
               and "http" not in r["doi"] and "|" not in r["doi"]
               and not any(c.isspace() for c in r["doi"])]

    done = set()
    if AUTHORSHIPS.exists():
        with io.open(AUTHORSHIPS, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line)["uid"])
                except Exception:                             # noqa: BLE001
                    continue
    todo = [r for r in targets if r["uid"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    log(f"[auth] {len(targets):,} papers with a usable DOI; {len(done):,} "
        f"cached; {len(todo):,} to fetch in {(len(todo)+PER-1)//PER:,} requests")
    if not todo:
        return 0

    oa_auth.preflight(log)
    fails = collections.Counter()
    got = 0
    fh_out = io.open(AUTHORSHIPS, "a", encoding="utf-8")
    prog = Progress((len(todo) + PER - 1) // PER, "auth-ids", every_s=60)
    try:
        for i in range(0, len(todo), PER):
            chunk = todo[i:i + PER]
            by_doi = {r["doi"].lower(): r for r in chunk}
            try:
                rr = requests.get(
                    WORKS_API,
                    headers=oa_auth.headers({"User-Agent": "quant-digest/1.0"}),
                    params={"filter": "doi:" + "|".join(
                                "https://doi.org/" + d for d in by_doi),
                            "select": "doi,authorships", "per-page": PER},
                    timeout=120)
            except Exception as e:                            # noqa: BLE001
                fails[type(e).__name__] += 1
                prog.tick()
                continue
            if not rr.ok:
                fails[rr.status_code] += 1
                prog.tick()
                continue
            for w in (rr.json().get("results") or []):
                d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
                row = by_doi.get(d)
                if not row:
                    continue
                aus = []
                for a in (w.get("authorships") or [])[:25]:
                    au = a.get("author") or {}
                    if not au.get("id"):
                        continue
                    insts = [x.get("display_name")
                             for x in (a.get("institutions") or [])]
                    aus.append({"id": _bare(au["id"]),
                                "name": au.get("display_name") or "",
                                "inst": (insts or [None])[0],
                                "country": (a.get("countries") or [None])[0]})
                fh_out.write(json.dumps({"uid": row["uid"], "authors": aus},
                                        ensure_ascii=False) + "\n")
                got += 1
            fh_out.flush()
            prog.tick()
            time.sleep(args.pause)
    finally:
        fh_out.close()
    prog.done()
    log(f"[auth] {got:,} papers' authorships written -> {AUTHORSHIPS}")
    if fails:
        log(f"[auth] !! {sum(fails.values()):,} requests FAILED: {dict(fails)} "
            f"-- those papers are NOT cached, re-run to retry")
    return 0


def _hindex(cites):
    cites = sorted(cites, reverse=True)
    h = 0
    for i, c in enumerate(cites, 1):
        if c >= i:
            h = i
        else:
            break
    return h


def cmd_build(args):
    if not AUTHORSHIPS.exists():
        raise SystemExit(
            f"[auth] {AUTHORSHIPS} missing -- run --ids first. Building from "
            f"names instead would merge every J. Wang in the corpus and split "
            f"Kenneth French from himself, which is the failure this tool "
            f"exists to avoid.")
    if not MASTER.exists():
        raise SystemExit(f"[auth] {MASTER} missing -- run tools/core_master.py")

    master = {}
    for r in csv.DictReader(io.open(MASTER, encoding="utf-8", newline="")):
        master[r["uid"]] = r
    log(f"[auth] master: {len(master):,} papers")

    def _i(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    papers = collections.defaultdict(list)     # author_id -> [master rows]
    names = {}
    meta = {}
    n_lines = 0
    with io.open(AUTHORSHIPS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:                                 # noqa: BLE001
                continue
            n_lines += 1
            m = master.get(rec["uid"])
            if m is None:
                continue
            for a in rec.get("authors") or []:
                aid = a["id"]
                papers[aid].append(m)
                names.setdefault(aid, a.get("name") or "")
                if aid not in meta:
                    meta[aid] = (a.get("inst"), a.get("country"))
    log(f"[auth] {n_lines:,} authorship rows -> {len(papers):,} distinct "
        f"author IDs (name-grouping gave 241,843, which is the point)")

    keep = {a: ps for a, ps in papers.items() if len(ps) >= args.min_papers}
    log(f"[auth] {len(keep):,} authors with >= {args.min_papers} papers "
        f"-- fetching real h-indices for those "
        f"({(len(keep)+PER-1)//PER:,} requests)")

    oa_auth.preflight(log)
    stats = {}
    fails = collections.Counter()
    ids = sorted(keep)
    prog = Progress((len(ids) + PER - 1) // PER, "auth-h", every_s=60)
    for i in range(0, len(ids), PER):
        batch = ids[i:i + PER]
        try:
            rr = requests.get(
                AUTHORS_API,
                headers=oa_auth.headers({"User-Agent": "quant-digest/1.0"}),
                params={"filter": "openalex_id:" + "|".join(batch),
                        "select": "id,display_name,summary_stats,works_count,"
                                  "cited_by_count,last_known_institutions",
                        "per-page": PER}, timeout=120)
        except Exception as e:                                # noqa: BLE001
            fails[type(e).__name__] += 1
            prog.tick()
            continue
        if not rr.ok:
            fails[rr.status_code] += 1
            prog.tick()
            continue
        for a in (rr.json().get("results") or []):
            stats[_bare(a.get("id"))] = a
        prog.tick()
        time.sleep(args.pause)
    prog.done()
    log(f"[auth] real h-index resolved for {len(stats):,} of {len(keep):,}")
    if fails:
        log(f"[auth] !! {sum(fails.values()):,} requests FAILED: {dict(fails)}")

    rows = []
    for aid, ps in keep.items():
        cites = [_i(p.get("cites")) for p in ps]
        sl = collections.Counter((p.get("sleeve") or "") for p in ps
                                 if (p.get("sleeve") or ""))
        top = max(ps, key=lambda p: _i(p.get("cites")), default=None)
        st = stats.get(aid) or {}
        inst, country = meta.get(aid, (None, None))
        lki = (st.get("last_known_institutions") or [{}])
        rows.append({
            "author_id": aid,
            "name": (st.get("display_name") or names.get(aid) or ""),
            "n_papers": len(ps),
            "h_corpus": _hindex(cites),
            "cites_corpus": sum(cites),
            "h_openalex": (st.get("summary_stats") or {}).get("h_index", ""),
            "works_openalex": st.get("works_count", ""),
            "cites_openalex": st.get("cited_by_count", ""),
            "institution": ((lki[0] or {}).get("display_name") if lki else None)
                           or inst or "",
            "country": country or "",
            "top_sleeve": (sl.most_common(1)[0][0] if sl else ""),
            "distinct_sleeves": len(sl),
            "fwd_citers_total": sum(_i(p.get("fwd_citers")) for p in ps),
            "top_paper_title": ((top or {}).get("title") or "")[:120],
            "top_paper_cites": _i((top or {}).get("cites")),
        })
    rows.sort(key=lambda r: -r["h_corpus"])
    DEST.parent.mkdir(parents=True, exist_ok=True)
    with io.open(DEST, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)

    log(f"\n[auth] top 20 by IN-CORPUS h-index:\n")
    log(f"    {'h_corp':>6} {'h_real':>6} {'papers':>7}  name")
    for r in rows[:20]:
        log(f"    {r['h_corpus']:>6} {str(r['h_openalex']):>6} "
            f"{r['n_papers']:>7}  {r['name']}")
    log(f"\n[auth] {len(rows):,} authors -> {DEST}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", action="store_true",
                    help="phase 1: fetch authorships with author IDs")
    ap.add_argument("--build", action="store_true",
                    help="phase 2: group, filter, fetch real h-indices")
    ap.add_argument("--min-papers", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pause", type=float, default=0.15)
    args = ap.parse_args()
    if not (args.ids or args.build):
        ap.error("pass --ids and/or --build")
    if args.ids:
        cmd_ids(args)
    if args.build:
        return cmd_build(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
