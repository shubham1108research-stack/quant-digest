#!/usr/bin/env python3
"""Merge every extracted source into ONE file per paper.

WHAT THIS SOLVES. The pool's data is spread across seven files keyed by uid --
core_candidates.csv (the spine), OpenAlex extra, S2 extra, topics, the forward
graph, graph metrics, and the Work-ID map. Each was fetched separately and none
of them is usable on its own. This joins them.

TWO OUTPUTS, BECAUSE ONE FORMAT CANNOT SERVE BOTH USES:

    core_master.csv     flat scalars, no abstract or TLDR body. The file you
                        open, sort and filter.
    core_master.ndjson  everything, including abstract, keywords, authors,
                        refs, related, counts_by_year. The file you script
                        against.

The CSV carries has_abstract/has_tldr as 0/1 rather than the text itself: at
~1,500 chars for the 135,313 papers that have an abstract, inlining it roughly
triples the file and makes the thing it exists to be -- browsable -- stop being
browsable. The text is one uid lookup away in the ndjson.

DOWNLOADABLE IS A UNION, AND THAT IS NOT COSMETIC. A paper counts as
downloadable when S2 returned an openAccessPdf url OR OpenAlex reports an
oa_status of green/diamond/gold/hybrid/bronze. Measured on this pool: 94,341
carry both signals, but 16,156 are S2-only and 15,362 are OpenAlex-only, so
either source alone loses ~31,500 papers. dl_source records which fired, so the
claim stays auditable rather than being a bare boolean nobody can check.

It means A LEGAL OPEN-ACCESS COPY IS ADVERTISED. It is not a promise that a
fetch succeeds, and this tool downloads nothing.

MEMORY. The OpenAlex extract is 466 MB of ndjson and parsing it into one dict
would cost several GB, so it is STREAMED twice -- once to collect the scalars
the CSV needs, once to write the ndjson -- while the small lookups (topics,
S2, metrics, ids) are held in memory. Everything else in this repo that reads
a whole table into a dict does so at 12k rows; this is 230k.

    python tools/core_master.py --dry-run   # join counts, write nothing
    python tools/core_master.py             # write both files
"""

import argparse
import collections
import csv
import io
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from progress import Progress                                 # noqa: E402

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
OAX = OUT / "core_openalex_extra.ndjson"
S2X = OUT / "core_s2_extra.ndjson"
TOPICS = OUT / "core_topics.json"
METRICS = OUT / "core_graph_metrics.json"
IDS = OUT / "core_openalex_ids.json"

CSV_OUT = OUT / "core_master.csv"
ND_OUT = OUT / "core_master.ndjson"

OPEN_STATUS = {"green", "diamond", "gold", "hybrid", "bronze"}

COLS = ["uid", "title", "year", "doi", "type", "sleeve", "family", "tag",
        "found_by", "n_routes", "seed_indegree", "rank", "score", "held",
        "cites", "cites_per_year", "s2_cites", "influential",
        "fwci", "pctl", "fwd_citers", "out_refs", "pagerank",
        "topic", "subfield", "field", "topic_score",
        "n_keywords", "has_abstract", "has_tldr",
        "oa_status", "pdf_url", "downloadable", "dl_source", "openalex_id"]


def log(m):
    print(m, flush=True)


def _require(*paths):
    """Refuse loudly on a missing input.

    A merge that runs anyway emits 230,804 rows with empty columns and looks
    like a success -- the exact defect class this repository has spent the
    session removing. core_topic_cut.py's _load() does the same thing for the
    same reason.
    """
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise SystemExit(
            f"[master] REFUSING to merge -- missing input(s): "
            f"{', '.join(missing)}. The merge would still produce a full-length "
            f"file with those columns silently blank, which is worse than no "
            f"file at all.")


def _load_small():
    topics = json.loads(TOPICS.read_text(encoding="utf-8"))
    metrics = json.loads(METRICS.read_text(encoding="utf-8"))
    ids = json.loads(IDS.read_text(encoding="utf-8"))
    s2 = {}
    with io.open(S2X, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:                                 # noqa: BLE001
                continue
            s2[r["uid"]] = r
    return topics, metrics, ids, s2


def _scan_oax_scalars():
    """Stream the 466 MB extract, keeping only what the CSV needs."""
    out = {}
    n = 0
    with io.open(OAX, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:                                 # noqa: BLE001
                continue
            n += 1
            out[r["uid"]] = (
                len(r.get("keywords") or []),
                1 if (r.get("abstract") or "").strip() else 0,
                r.get("fwci"),
                r.get("pctl"),
                r.get("oa_status"),
                r.get("type"),
                r.get("year"),
            )
    return out, n


def _downloadable(s2rec, oa_status):
    pdf = (s2rec or {}).get("pdf_url") or ""
    is_open = (oa_status or "") in OPEN_STATUS
    if pdf and is_open:
        return 1, "both", pdf
    if pdf:
        return 1, "s2", pdf
    if is_open:
        return 1, "openalex", ""
    return 0, "", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report join counts and write nothing")
    args = ap.parse_args()

    _require(CAND, OAX, S2X, TOPICS, METRICS, IDS)

    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))
    log(f"[master] spine: {len(rows):,} candidates from {CAND.name}")

    topics, metrics, ids, s2 = _load_small()
    oax, n_oax = _scan_oax_scalars()
    log(f"[master] loaded: openalex {n_oax:,} · s2 {len(s2):,} · "
        f"topics {len(topics):,} · metrics {len(metrics):,} · ids {len(ids):,}")

    uids = {r["uid"] for r in rows}
    log(f"[master] joins onto the pool: "
        f"openalex {len(uids & set(oax)):,} · s2 {len(uids & set(s2)):,} · "
        f"topics {len(uids & set(topics)):,} · metrics {len(uids & set(metrics)):,}")

    dl_counts = collections.Counter()
    for r in rows:
        u = r["uid"]
        _, src, _ = _downloadable(s2.get(u), (oax.get(u) or (None,)*7)[4])
        dl_counts[src or "(not downloadable)"] += 1
    n_dl = sum(v for k, v in dl_counts.items() if k != "(not downloadable)")
    log(f"[master] downloadable: {n_dl:,} of {len(rows):,} "
        f"({100*n_dl/len(rows):.1f}%) -- {dict(dl_counts)}")

    if args.dry_run:
        log("[master] --dry-run: nothing written")
        return 0

    # ------------------------------------------------------------------ csv
    prog = Progress(len(rows), "master-csv", every_s=20)
    with io.open(CSV_OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            u = r["uid"]
            o = oax.get(u) or (0, 0, None, None, None, None, None)
            n_kw, has_abs, fwci, pctl, oa_status, oa_type, oa_year = o
            s = s2.get(u) or {}
            t = topics.get(u) or {}
            m = metrics.get(u) or {}
            dl, dl_src, pdf = _downloadable(s, oa_status)
            w.writerow({
                "uid": u,
                "title": r.get("title") or "",
                "year": r.get("year") or oa_year or "",
                "doi": r.get("doi") or "",
                "type": oa_type or "",
                "sleeve": r.get("sleeve") or "",
                "family": r.get("family") or "",
                "tag": r.get("tag") or "",
                "found_by": r.get("found_by") or "",
                "n_routes": r.get("n_routes") or "",
                "seed_indegree": r.get("seed_indegree") or "",
                "rank": r.get("rank") or "",
                "score": r.get("score") or "",
                "held": r.get("held") or "0",
                "cites": r.get("cites") or "",
                "cites_per_year": r.get("cites_per_year") or "",
                "s2_cites": s.get("cites") if s.get("cites") is not None else "",
                "influential": s.get("influential")
                               if s.get("influential") is not None else "",
                "fwci": fwci if fwci is not None else "",
                "pctl": pctl if pctl is not None else "",
                "fwd_citers": m.get("fwd_citers", 0),
                "out_refs": m.get("out_refs", 0),
                "pagerank": m.get("pagerank", ""),
                "topic": t.get("t") or "",
                "subfield": t.get("sf") or "",
                "field": t.get("f") or "",
                "topic_score": t.get("s") if t.get("s") is not None else "",
                "n_keywords": n_kw,
                "has_abstract": has_abs,
                "has_tldr": 1 if s.get("tldr") else 0,
                "oa_status": oa_status or "",
                "pdf_url": pdf,
                "downloadable": dl,
                "dl_source": dl_src,
                "openalex_id": ids.get(u) or "",
            })
            prog.tick()
    prog.done()
    log(f"[master] {CSV_OUT} -- {CSV_OUT.stat().st_size/1e6:.0f} MB")

    # --------------------------------------------------------------- ndjson
    # Streamed, not held: this carries the abstract and the full reference
    # list, so the whole thing in memory at once is several GB.
    by_uid = {r["uid"]: r for r in rows}
    written = 0
    dup = 0
    nd_seen = set()
    prog = Progress(n_oax, "master-ndjson", every_s=20)
    with io.open(ND_OUT, "w", encoding="utf-8") as out, \
            io.open(OAX, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:                                 # noqa: BLE001
                continue
            u = o["uid"]
            r = by_uid.get(u)
            if r is None:
                prog.tick()
                continue          # in the extract but cut from the pool since
            if u in nd_seen:
                # THE EXTRACT IS APPEND-ONLY AND LEGITIMATELY REPEATS. Its
                # retry runs re-append a uid that a previous run already
                # fetched -- 837 uids, 840 extra lines here -- which is
                # correct behaviour for a resumable fetcher and wrong for a
                # merged master file. Without this the master ndjson ships
                # 225,872 lines for 225,032 papers and every count taken off
                # it is quietly ~840 too high.
                dup += 1
                prog.tick()
                continue
            nd_seen.add(u)
            s = s2.get(u) or {}
            t = topics.get(u) or {}
            m = metrics.get(u) or {}
            dl, dl_src, pdf = _downloadable(s, o.get("oa_status"))
            rec = {
                "uid": u,
                "title": r.get("title") or "",
                "year": r.get("year") or o.get("year"),
                "doi": r.get("doi") or "",
                "type": o.get("type"),
                "sleeve": r.get("sleeve") or "",
                "family": r.get("family") or "",
                "tag": r.get("tag") or "",
                "found_by": r.get("found_by") or "",
                "n_routes": r.get("n_routes") or "",
                "seed_indegree": r.get("seed_indegree") or "",
                "held": r.get("held") or "0",
                "cites": r.get("cites") or "",
                "s2_cites": s.get("cites"),
                "influential": s.get("influential"),
                "fwci": o.get("fwci"),
                "pctl": o.get("pctl"),
                "fwd_citers": m.get("fwd_citers", 0),
                "out_refs": m.get("out_refs", 0),
                "pagerank": m.get("pagerank"),
                "topic": t.get("t"), "subfield": t.get("sf"),
                "field": t.get("f"), "topic_score": t.get("s"),
                "all_topics": t.get("all") or [],
                "keywords": o.get("keywords") or [],
                "abstract": o.get("abstract") or "",
                "tldr": s.get("tldr") or "",
                "authors": o.get("authors") or [],
                "counts_by_year": o.get("by_year") or [],
                "refs": o.get("refs") or [],
                "related": o.get("related") or [],
                "oa_status": o.get("oa_status"),
                "pdf_url": pdf,
                "downloadable": dl,
                "dl_source": dl_src,
                "openalex_id": ids.get(u) or "",
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
            prog.tick()
    prog.done()
    log(f"[master] {ND_OUT} -- {written:,} rows, "
        f"{ND_OUT.stat().st_size/1e6:.0f} MB")
    if dup:
        log(f"[master] {dup:,} duplicate uid line(s) in the extract collapsed "
            f"to one row each (the extract is append-only and its retry runs "
            f"re-append)")
    log(f"[master] NOTE: the ndjson covers the {written:,} papers OpenAlex "
        f"returned, not all {len(rows):,} -- the CSV is the complete spine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
