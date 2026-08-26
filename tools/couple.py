#!/usr/bin/env python3
"""Bibliographic coupling edges from the stored reference lists.

WHY THIS IS THE EDGE KIND WORTH ADDING. The internal citation graph is 29,661
edges over 20,999 papers -- 1.4 each, far too thin for authority to propagate.
Coupling does not need the cited work to be in the archive: two papers we hold
are coupled when they cite the same outside thing, whatever that thing is.
Measured on the same reference lists, 41,871 shared references carry
**3,447,517 coupled pairs**, roughly 116x the citation graph.

AND 3.4M PAIRS IS NOT A GRAPH. It is an upper bound, and using it raw would be
worse than not having it. Two problems, both handled here:

  1. A reference cited by many held papers is uninformative and quadratically
     expensive. One cited by 1,000 of our papers contributes 499,500 pairs and
     tells you only that all 1,000 are finance. `--df-max` drops those, and
     the same threshold is what keeps the pair count tractable at all.

  2. Not all shared references are equally telling. Sharing a citation to
     Fama-French means almost nothing; sharing one to an obscure 1997 paper on
     convenience yields means a great deal. So each shared reference
     contributes IDF -- log(N/df) -- rather than 1, exactly as BM25 weights a
     term.

Then top-K per paper, mirroring the similarity graph's K=15, because a
weighted-pair list with no cap is a density problem wearing a ranking's
clothes.

    python tools/couple.py report                 # the distribution, no writes
    python tools/couple.py build --dry-run
    python tools/couple.py build --df-max 200 --k 15
"""

import argparse
import collections
import math
import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import store   # noqa: E402
from graph import graph_con, _SCHEMA   # noqa: E402

# A reference cited by more than this many HELD papers is dropped. Not tuned
# yet -- `report` prints what each threshold costs and keeps.
DF_MAX = 200
K = 15                        # coupled neighbours kept per paper


def log(m):
    print(m, flush=True)


def _postings(con):
    """{reference: [uid, ...]} for references shared by at least two papers."""
    by_ref = collections.defaultdict(list)
    for src, ref in con.execute("SELECT src, ref FROM paper_refs"):
        by_ref[ref].append(src)
    return {r: u for r, u in by_ref.items() if len(u) > 1}


def cmd_report(args):
    con = store.connect()
    shared = _postings(con)
    if not shared:
        log("[couple] paper_refs is empty -- run `graph.py cites` first")
        return 1
    n_papers = con.execute(
        "SELECT count(DISTINCT src) FROM paper_refs").fetchone()[0]
    counts = sorted(((len(u), r) for r, u in shared.items()), reverse=True)
    total = sum(c * (c - 1) // 2 for c, _ in counts)
    log(f"[couple] {n_papers:,} papers carry references")
    log(f"[couple] {len(shared):,} references shared by >1 held paper")
    log(f"[couple] {total:,} raw coupled pairs\n")
    log("[couple] the head, where the pairs and the noise both are:")
    for c, r in counts[:6]:
        log(f"[couple]   cited by {c:>5} held papers -> {c*(c-1)//2:>10,} pairs  {r[:40]}")
    log("")
    log(f"[couple] {'df-max':>8}{'refs kept':>12}{'pairs':>14}{'% of raw':>10}")
    for cap in (2, 3, 5, 10, 25, 50, 100, 200, 500):
        keep = [(c, r) for c, r in counts if c <= cap]
        p = sum(c * (c - 1) // 2 for c, _ in keep)
        log(f"[couple] {cap:>8}{len(keep):>12,}{p:>14,}{100.0*p/max(total,1):>9.1f}%")
    log("\n[couple] a reference cited by many held papers says only that they "
        "are all finance; the informative ones are shared by few")
    return 0


def cmd_build(args):
    con = store.connect()
    shared = _postings(con)
    if not shared:
        log("[couple] paper_refs is empty -- run `graph.py cites` first")
        return 1
    n_papers = con.execute(
        "SELECT count(DISTINCT src) FROM paper_refs").fetchone()[0]

    weights = collections.defaultdict(float)
    used = kept = 0
    for ref, uids in shared.items():
        df = len(uids)
        if df > args.df_max:
            continue
        kept += 1
        # IDF, as BM25 weights a term: a reference almost nobody else cites is
        # strong evidence two papers are on the same problem.
        idf = math.log(n_papers / float(df))
        u = sorted(set(uids))
        for i in range(len(u)):
            for j in range(i + 1, len(u)):
                weights[(u[i], u[j])] += idf
                used += 1
    log(f"[couple] {kept:,} references at df <= {args.df_max} produced "
        f"{len(weights):,} distinct pairs ({used:,} contributions)")

    # Top-K per paper, both directions, then the union -- an edge surviving in
    # either paper's top K is kept, which is how graph.py's kNN behaves too.
    best = collections.defaultdict(list)
    for (a, b), w in weights.items():
        best[a].append((w, b))
        best[b].append((w, a))
    edges = set()
    for uid, lst in best.items():
        lst.sort(reverse=True)
        for w, other in lst[:args.k]:
            edges.add((uid, other, round(w, 4)) if uid < other
                      else (other, uid, round(w, 4)))
    log(f"[couple] top-{args.k} per paper -> {len(edges):,} coupling edges "
        f"({len(edges)/max(n_papers,1):.1f} per paper)")

    if args.dry_run:
        log("[couple] dry run -- nothing written")
        top = sorted(edges, key=lambda e: -e[2])[:5]
        for a, b, w in top:
            log(f"[couple]   w={w:>8.2f}  {a[:34]:<36} {b[:34]}")
        return 0

    g = graph_con(con)
    g.executescript(_SCHEMA)
    g.execute("DELETE FROM g.edges WHERE kind='couple'")
    g.executemany(
        "INSERT OR REPLACE INTO g.edges (src,dst,kind,w) VALUES (?,?,'couple',?)",
        list(edges))
    con.commit()
    log(f"[couple] wrote {len(edges):,} edges of kind 'couple'")
    log("[couple] NOT exported to docs/edges.bin yet -- gate on "
        "tools/sleeve_eval.py first, an edge kind that does not move F1 does "
        "not ship")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("report")
    b = sub.add_parser("build")
    b.add_argument("--df-max", type=int, default=DF_MAX)
    b.add_argument("--k", type=int, default=K)
    b.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return cmd_report(args) if args.action == "report" else cmd_build(args)


if __name__ == "__main__":
    sys.exit(main())
