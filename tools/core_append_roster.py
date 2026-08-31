#!/usr/bin/env python3
"""Add newly-harvested roster papers to the pool without a full rebuild.

THE PROBLEM THIS SOLVES. Route D only reaches the pool through
build_core.py, which re-assembles every route from scratch -- an hour in CI.
Adding one person to data/core_roster.csv and harvesting them
(`core_sources.py roster --only "Name"`, seconds) therefore leaves their
papers stranded in core_roster_papers.json until someone spends that hour.
Dirk Baur joined the roster with 59 papers, 21 already in the pool and 38
with nowhere to go.

WHAT IT DOES. Finds harvested roster papers absent from
core_candidates.csv, fetches their OpenAlex record (topic, abstract,
metrics, references) and their S2 record, derives the same columns
build_core derives -- crucially with THE SAME score formula and THE SAME
taxonomy matching, not an approximation of them -- and appends.

WHAT IT CANNOT DO, stated rather than glossed:

    held        left 0. The authoritative state.db lives in R2; the local
                copy is whatever was last pulled, so testing membership
                against it would mark papers unheld that the archive
                actually has. build_core in CI gets this right.
    seed_indegree  left 0. It counts citations FROM the seed set, which is
                computed during a full compile from paper_refs.
    fwd_citers  not computed here; run core_forward_graph.py afterwards to
                fold the new papers into the citation graph.

So these rows are correct on everything the harvest and the APIs can
establish, and conservatively zero on the two fields that need a full
compile. A subsequent build_core run supersedes them.

    python tools/core_append_roster.py --dry-run
    python tools/core_append_roster.py --only "Dirk G. Baur"
"""

import argparse
import collections
import csv
import io
import json
import math
import pathlib
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import oa as oa_auth                                          # noqa: E402
import textnorm                                               # noqa: E402
from sources import _reconstruct_abstract                     # noqa: E402

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
HARVEST = OUT / "core_roster_papers.json"
TAGS = OUT / "core_tags.csv"
TOPICS = OUT / "core_topics.json"
OAX = OUT / "core_openalex_extra.ndjson"
IDS = OUT / "core_openalex_ids.json"
WORKS = "https://api.openalex.org/works"


def log(m):
    print(m, flush=True)


def _taxonomy():
    """Same vocabulary, same normalisation, longest-first -- as build_core."""
    rows = list(csv.DictReader(io.open(TAGS, encoding="utf-8")))
    tax = [(textnorm.norm(r["term"]), r["family"], (r.get("sleeve") or "").strip())
           for r in rows if (r.get("term") or "").strip()]
    return sorted(tax, key=lambda x: -len(x[0]))


FAMILY_SLEEVE = {}


def _family_sleeve():
    import build_core                                         # noqa: PLC0415
    return build_core.FAMILY_SLEEVE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="restrict to these authors")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for p in (CAND, HARVEST, TAGS):
        if not p.exists():
            raise SystemExit(f"[append] {p} missing")

    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))
    cols = list(rows[0].keys())
    pool = {r["uid"] for r in rows}
    harvest = json.loads(HARVEST.read_text(encoding="utf-8"))

    only = [x.strip().lower() for x in args.only.split(",") if x.strip()]
    if only:
        harvest = [h for h in harvest
                   if any(o in (h.get("author") or "").lower() for o in only)]

    def _uid(h):
        if h.get("doi"):
            return "doi:" + h["doi"].lower()
        if h.get("arxiv"):
            return "arxiv:" + h["arxiv"]
        return None

    # one row per paper, but remember every roster author who claims it
    by_uid = {}
    for h in harvest:
        u = _uid(h)
        if not u or u in pool:
            continue
        rec = by_uid.setdefault(u, dict(h, _authors=set()))
        rec["_authors"].add(h["author"])
    missing = list(by_uid.values())
    log(f"[append] {len(harvest):,} harvested rows -> {len(missing):,} papers "
        f"not in the pool of {len(rows):,}")
    if not missing:
        log("[append] nothing to add")
        return 0
    if args.dry_run:
        for m in sorted(missing, key=lambda m: -(m.get("cites") or 0))[:15]:
            log(f"    {m.get('cites'):>5} {m.get('year')}  {m['title'][:62]}")
        log("[append] --dry-run: nothing written")
        return 0

    # ---------------------------------------------------------- enrich
    oa_auth.preflight(log)
    dois = [m["doi"] for m in missing if m.get("doi")]
    extra = {}
    for i in range(0, len(dois), 100):
        b = dois[i:i + 100]
        try:
            rr = requests.get(
                WORKS, headers=oa_auth.headers({"User-Agent": "quant-digest/1.0"}),
                params={"filter": "doi:" + "|".join(
                            "https://doi.org/" + d.lower() for d in b),
                        "select": "id,doi,title,publication_year,type,"
                                  "cited_by_count,fwci,citation_normalized_percentile,"
                                  "counts_by_year,open_access,topics,keywords,"
                                  "abstract_inverted_index,referenced_works,"
                                  "related_works,authorships",
                        "per-page": 100}, timeout=120)
        except Exception as e:                                # noqa: BLE001
            log(f"[append] !! OpenAlex {type(e).__name__}")
            continue
        if not rr.ok:
            log(f"[append] !! OpenAlex HTTP {rr.status_code}: {rr.text[:120]}")
            continue
        for w in (rr.json().get("results") or []):
            d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
            extra["doi:" + d] = w
        time.sleep(0.2)
    log(f"[append] OpenAlex returned {len(extra):,} of {len(missing):,}")

    tax = _taxonomy()
    fam_sleeve = _family_sleeve()
    new_rows = []
    topics_add, oax_add, ids_add = {}, [], {}
    for m in missing:
        u = _uid(m)
        w = extra.get(u) or {}
        abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
        year = w.get("publication_year") or m.get("year") or ""
        cites = w.get("cited_by_count")
        if cites is None:
            cites = m.get("cites") or 0
        try:
            age = max(1, 2026 - int(year))
            cpy = round(cites / age, 1)
        except (TypeError, ValueError):
            cpy = ""
        # SAME taxonomy match as build_core: longest term against
        # normalised title + abstract.
        blob = textnorm.norm((m.get("title") or "") + " " + abstract)
        family = tag = sleeve = ""
        for term, fam, sl in tax:
            if term and term in blob:
                family, tag, sleeve = fam, term, sl
                break
        if not sleeve and family:
            sleeve = fam_sleeve.get(family, "")
        if not sleeve:
            sleeve = "other"
        routes = {"authors"}
        # SAME score formula as build_core.py:676-681.
        s = (math.log10(1 + (cites or 0))
             + math.log10(1 + (cpy or 0)) * 1.5
             + 0.0                                # seed_indegree 0 -> log10(1)=0
             + len(routes) * 0.6
             + 0.5)                               # s_pract: authors route
        row = {c: "" for c in cols}
        row.update({
            "title": m.get("title") or w.get("title") or "",
            "year": year, "doi": m.get("doi") or "",
            "cites": cites, "cites_per_year": cpy,
            "seed_indegree": 0, "n_routes": 1, "found_by": "authors",
            "sleeve": sleeve, "family": family, "tag": tag,
            "held": 0, "score": round(s, 3), "uid": u,
        })
        new_rows.append(row)

        pt = w.get("primary_topic") or ((w.get("topics") or [None])[0]) or {}
        if pt:
            topics_add[u] = {"t": pt.get("display_name"),
                             "sf": (pt.get("subfield") or {}).get("display_name"),
                             "f": (pt.get("field") or {}).get("display_name"),
                             "s": round(pt.get("score") or 0, 3),
                             "all": [x.get("display_name")
                                     for x in (w.get("topics") or [])]}
        if w:
            oax_add.append({
                "uid": u,
                "keywords": [k.get("display_name") for k in (w.get("keywords") or [])],
                "abstract": abstract,
                "cites": w.get("cited_by_count"), "fwci": w.get("fwci"),
                "pctl": (w.get("citation_normalized_percentile") or {}).get("value"),
                "by_year": [{"y": c.get("year"), "n": c.get("cited_by_count")}
                            for c in (w.get("counts_by_year") or [])],
                "oa_status": (w.get("open_access") or {}).get("oa_status"),
                "refs": [x.rsplit("/", 1)[-1] for x in (w.get("referenced_works") or [])],
                "related": [x.rsplit("/", 1)[-1] for x in (w.get("related_works") or [])],
                "authors": [{"name": (a.get("author") or {}).get("display_name"),
                             "inst": [i.get("display_name")
                                      for i in (a.get("institutions") or [])][:2],
                             "country": (a.get("countries") or [None])[0]}
                            for a in (w.get("authorships") or [])[:12]],
                "year": w.get("publication_year"), "type": w.get("type"),
            })
            if w.get("id"):
                ids_add[u] = w["id"].rsplit("/", 1)[-1]

    with io.open(CAND, "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols)
        wr.writeheader()
        wr.writerows(rows + new_rows)
    log(f"[append] {CAND}: {len(rows):,} -> {len(rows)+len(new_rows):,}")

    # keep the derived caches in step, so core_master.py sees the new papers
    if topics_add and TOPICS.exists():
        t = json.loads(TOPICS.read_text(encoding="utf-8"))
        t.update(topics_add)
        TOPICS.write_text(json.dumps(t), encoding="utf-8")
        log(f"[append] {TOPICS.name}: +{len(topics_add):,}")
    if oax_add and OAX.exists():
        with io.open(OAX, "a", encoding="utf-8") as fh:
            for r in oax_add:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        log(f"[append] {OAX.name}: +{len(oax_add):,}")
    if ids_add and IDS.exists():
        d = json.loads(IDS.read_text(encoding="utf-8"))
        d.update(ids_add)
        IDS.write_text(json.dumps(d), encoding="utf-8")
        log(f"[append] {IDS.name}: +{len(ids_add):,}")

    log(f"[append] held and seed_indegree are 0 on these rows -- both need a "
        f"full compile. Run core_forward_graph.py then core_master.py next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
