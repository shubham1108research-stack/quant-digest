#!/usr/bin/env python3
"""Invert the citation graph within the pool: who cites this paper.

THE GAP THIS CLOSES. Route C (graph.py, build_core.py) is backward-only: a
seed's reference list is read to find what it cites, which reaches older
work. It never asks the opposite question -- what newer work cites a seed --
so the pool structurally leans old. Confirmed absent earlier: zero
`cited_by` usage anywhere in graph.py or build_core.py.

WHY THIS COSTS NOTHING FURTHER. core_openalex_extract.py already holds
referenced_works (Work IDs) for 225,872 papers, and core_openalex_ids.py now
holds a Work ID for 224,423 of our own papers. A paper A citing paper B is
already sitting in A's referenced_works list; "who cites B" is just that
edge list read in the other direction. No new requests -- this is pure local
computation over what is already on disk.

THE RESULT IS BOUNDED BY CONSTRUCTION, and that boundedness is the point, not
a limitation smuggled in quietly. "How many people cite Fama-French" is
tens of thousands and useless as a filter. "How many papers ALREADY IN THIS
POOL cite it" is bounded by the pool's own size and answers a sharper
question: which papers here are the ones later work in this SAME corpus
built on. A paper with zero in-pool forward citers is not necessarily
unimportant -- it may be cited heavily by work this pool has not collected --
this graph cannot tell the difference and does not claim to.

EXTENDING BEYOND THE POOL is a different, separate job: OpenAlex exposes a
`cited_by_api_url` per work, and querying it for a short list of the
highest-forward-degree papers here would surface real NEW candidates this
pool is missing. Not done in this tool -- it is discovery (another route),
not graph-building, and belongs to a reviewed decision the way every other
route addition here has.

OUTPUT: export/core_forward_graph.json -- {uid: [list of in-pool uids that
cite it]}. Nothing ingested.

    python tools/core_forward_graph.py
"""

import collections
import csv
import io
import json
import pathlib
import sys

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
IDS = OUT / "core_openalex_ids.json"
EXTRA = OUT / "core_openalex_extra.ndjson"
DEST = OUT / "core_forward_graph.json"
EDGES = OUT / "core_edges.csv"
METRICS = OUT / "core_graph_metrics.json"


def log(m):
    print(m, flush=True)


def _metrics(forward, by_uid, iters=20, damping=0.85):
    """In-degree, out-degree and PageRank over the in-pool citation graph.

    WHY PAGERANK AND NOT THE CITATION COUNT WE ALREADY HAVE. `cites` is a
    global number from OpenAlex, and sorting the pool by it returns the same
    1970s canon every time -- it measures fame, and it measures it outside
    this corpus. PageRank over IN-POOL edges answers a narrower and more
    useful question: which papers the later work *in this collection* is
    built on. A 1970s paper nobody here actually cites scores low, which is
    the desired behaviour, not a bug.

    THIS IS SPARSE AND CHEAP -- seconds over 2.5M edges. It is not the O(n^2)
    trap that makes tools/graph.py build_sim unusable at this scale
    (230k x 230k dense dot products); nothing here ever materialises an
    n x n anything.

    Dangling nodes (papers citing nothing in-pool) have their mass
    redistributed uniformly rather than silently leaked, which is the usual
    way a hand-rolled PageRank quietly stops summing to 1.
    """
    # forward[dst] = {srcs citing dst}. Build the transpose once: out[src] =
    # [dsts src cites], which is the direction PageRank pushes mass along.
    out_edges = collections.defaultdict(list)
    for dst, srcs in forward.items():
        for src in srcs:
            out_edges[src].append(dst)

    nodes = sorted(set(forward) | set(out_edges))
    n = len(nodes)
    if not n:
        return {}
    idx = {u: i for i, u in enumerate(nodes)}
    out_lists = [[idx[d] for d in out_edges.get(u, ())] for u in nodes]

    rank = [1.0 / n] * n
    for _ in range(iters):
        nxt = [0.0] * n
        dangling = 0.0
        for i, outs in enumerate(out_lists):
            if not outs:
                dangling += rank[i]
                continue
            share = rank[i] / len(outs)
            for j in outs:
                nxt[j] += share
        base = (1.0 - damping) / n + damping * dangling / n
        rank = [base + damping * v for v in nxt]

    log(f"[fwd] pagerank over {n:,} connected nodes, {iters} iterations "
        f"(sum={sum(rank):.4f} -- should be ~1.0)")

    return {u: {"fwd_citers": len(forward.get(u, ())),
                "out_refs": len(out_edges.get(u, ())),
                "pagerank": round(rank[idx[u]], 10)}
            for u in nodes}


def main():
    for p in (CAND, IDS, EXTRA):
        if not p.exists():
            raise SystemExit(f"[fwd] {p} missing -- run the extraction first")

    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))
    by_uid = {r["uid"]: r for r in rows}
    log(f"[fwd] pool: {len(rows):,} candidates")

    uid_to_work = json.loads(IDS.read_text(encoding="utf-8"))
    work_to_uid = {w: u for u, w in uid_to_work.items()}
    log(f"[fwd] {len(work_to_uid):,} pool papers have a known OpenAlex Work ID "
        f"({100*len(work_to_uid)/len(rows):.1f}% of the pool)")

    # forward[target_uid] = set of uids in the pool that cite target_uid
    forward = collections.defaultdict(set)
    n_papers = n_edges_total = n_edges_inpool = 0
    with io.open(EXTRA, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            uid = rec["uid"]
            refs = rec.get("refs") or []
            n_papers += 1
            n_edges_total += len(refs)
            for ref_work in refs:
                target = work_to_uid.get(ref_work)
                if target and target != uid:
                    forward[target].add(uid)
                    n_edges_inpool += 1

    log(f"[fwd] {n_papers:,} papers' reference lists scanned, "
        f"{n_edges_total:,} outbound edges total")
    log(f"[fwd] {n_edges_inpool:,} of those point at a paper ALSO in the pool "
        f"({100*n_edges_inpool/max(1,n_edges_total):.2f}%) -- this is the "
        f"forward-citation graph")
    log(f"[fwd] {len(forward):,} pool papers have at least one in-pool "
        f"forward citer ({100*len(forward)/len(rows):.1f}% of the pool)")

    out = {u: sorted(v) for u, v in forward.items()}
    DEST.write_text(json.dumps(out), encoding="utf-8")
    log(f"[fwd] written to {DEST} -- nothing ingested")

    # ------------------------------------------------------------ edge list
    # DIRECTION IS src CITES dst. Stated here and in the file's own header
    # because an inverted edge list silently inverts every metric built on
    # it -- PageRank on a reversed graph ranks papers by how many references
    # they HAVE rather than how often they are cited, which looks plausible
    # and is completely wrong.
    n_written = 0
    with io.open(EDGES, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["src_uid", "dst_uid"])   # src cites dst
        for dst, srcs in forward.items():
            for src in srcs:
                w.writerow([src, dst])
                n_written += 1
    log(f"[fwd] {n_written:,} edges -> {EDGES} (src cites dst)")

    # ------------------------------------------------------- graph metrics
    metrics = _metrics(forward, by_uid)
    METRICS.write_text(json.dumps(metrics), encoding="utf-8")
    log(f"[fwd] per-paper metrics -> {METRICS}")

    # -------------------------------------------------------------- report
    ranked = sorted(forward.items(), key=lambda kv: -len(kv[1]))
    log(f"\n[fwd] top 25 by IN-POOL forward citers -- the papers this "
        f"corpus's own later work builds on most:\n")
    log(f"    {'in-pool citers':>15}  title")
    for uid, citers in ranked[:25]:
        title = (by_uid.get(uid, {}).get("title") or uid)[:70]
        log(f"    {len(citers):>15,}  {title}")

    deg = collections.Counter(len(v) for v in forward.values())
    log(f"\n[fwd] degree distribution: "
        f"{sum(1 for v in forward.values() if len(v)==1):,} papers with "
        f"exactly 1 in-pool citer, "
        f"{sum(1 for v in forward.values() if len(v)>=10):,} with 10+, "
        f"{sum(1 for v in forward.values() if len(v)>=50):,} with 50+")
    return 0


if __name__ == "__main__":
    sys.exit(main())
