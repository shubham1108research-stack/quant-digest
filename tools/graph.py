#!/usr/bin/env python3
"""Build the paper graph: similarity edges, and citation edges where we can get
them.

The archive is a bag of independent rows. Nothing relates one paper to another,
so retrieval can only do similarity lookup and tagging has to guess each paper
in isolation -- which is why `carry` ended up with 8 papers out of 3,361
labelled. A graph fixes both: it lets a label spread from a few confident seeds
to their neighbourhood (tools/propagate.py), and it lets Ask expand a result set
along real relationships instead of a single cosine ranking.

Nodes are the papers already in `items`, carrying the scores already computed.
Two edge kinds:

  sim    k nearest neighbours by embedding cosine. tools/embed_ab.py already
         built this exact matrix to print two summary percentages and threw it
         away; here it is kept. Symmetric, weighted by cosine.

  cites  OpenAlex `referenced_works`, keyed on DOI. 9,939 of 11,583 items carry
         one -- the highest-coverage join in the archive. Only edges whose BOTH
         endpoints are in the archive are stored: a reference to a paper we do
         not hold cannot be traversed and would swamp the table.

  python tools/graph.py sim              # build similarity edges
  python tools/graph.py cites            # fetch + build citation edges
  python tools/graph.py report           # counts and degree distribution
  python tools/graph.py export           # docs/edges.bin for the browser
"""

import argparse
import collections
import json
import pathlib
import struct
import sys
import time

import numpy as np
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store   # noqa: E402

# The graph lives in its OWN database, not state.db. It is derived from the
# vector cache and rebuilds in ~12s, but 172k edges with TEXT endpoints and two
# indexes cost 33 MB -- which took the committed state.db from 55 MB to 88 MB,
# against GitHub's 100 MB hard limit. Derived data does not belong in the file
# that carries the archive.
# sim edges: 166k rows, 33 MB, rebuilt from the vector cache in ~12s -> keep
# them OUT of the committed database.
# cites edges: 6.5k rows, ~1 MB, and each one costs an OpenAlex round trip
# (~200 requests, several minutes) -> those stay IN state.db, because
# recomputing them on every deploy would be minutes of API calls for data that
# does not change.
GRAPH_DB = "state_graph.db"


def graph_con(con):
    """A connection with the graph attached, so joins against items still work."""
    con.execute("ATTACH DATABASE ? AS g", (GRAPH_DB,))
    return con

K = 15                      # neighbours kept per paper
SIM_FLOOR = 0.30            # on CENTRED cosine (see _vectors); the raw-space
                            # equivalent would be ~0.85 and drop nothing
BLOCK = 512                 # rows per similarity block; 512x11507 float32 = 23 MB
MAILTO = "upadhyays1108@gmail.com"
UA = {"User-Agent": f"quant-digest/1.0 (mailto:{MAILTO})"}

_CITES_SCHEMA = """
CREATE TABLE IF NOT EXISTS cites (
    src TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY (src, dst)
);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS g.edges (
    src  TEXT NOT NULL,
    dst  TEXT NOT NULL,
    kind TEXT NOT NULL,          -- sim | cites
    w    REAL,                   -- cosine for sim; 1.0 for cites
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS g.edges_src ON edges (kind, src);
CREATE INDEX IF NOT EXISTS g.edges_dst ON edges (kind, dst);
"""


def log(m):
    print(m, flush=True)


def _vectors(con):
    """Every cached vector, as a unit-normalised float32 matrix."""
    rows = con.execute(
        "SELECT uid, vec FROM embeddings WHERE model='mistral-embed' ORDER BY uid"
    ).fetchall()
    if not rows:
        raise SystemExit("no embeddings cached -- run tools/embed.py first")
    dim = len(rows[0][1])
    uids = [r[0] for r in rows]
    X = np.empty((len(rows), dim), dtype=np.float32)
    for i, (_, blob) in enumerate(rows):
        X[i] = np.frombuffer(blob, dtype=np.int8).astype(np.float32)
    # CENTRE before normalising. mistral-embed is strongly anisotropic: every
    # vector shares a large common direction, so raw cosine between two
    # unrelated papers is already ~0.87 and the 15th-nearest neighbour of
    # anything sits at 0.70. Measured on this corpus, subtracting the mean
    # takes the 15-NN cosine from mean 0.864 / spread 0.26 to mean 0.439 /
    # spread 0.78, and changes a quarter of the neighbours. Without it a
    # similarity floor is meaningless and label propagation smears a tag
    # across the whole archive instead of a neighbourhood.
    X -= X.mean(axis=0, keepdims=True)
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    return uids, X


def build_sim(con, args):
    uids, X = _vectors(con)
    n = len(uids)
    log(f"[graph] {n:,} vectors, {X.shape[1]}-dim -- kNN k={K}, floor={SIM_FLOOR}")
    con.executescript(_SCHEMA)
    con.execute("DELETE FROM g.edges WHERE kind='sim'")
    kept = 0
    t0 = time.monotonic()
    for i0 in range(0, n, BLOCK):
        i1 = min(i0 + BLOCK, n)
        S = X[i0:i1] @ X.T                       # (block, n) cosine
        # a paper is its own nearest neighbour; remove it before ranking
        for r in range(i1 - i0):
            S[r, i0 + r] = -1.0
        top = np.argpartition(-S, K, axis=1)[:, :K]
        rows = []
        for r in range(i1 - i0):
            for c in top[r]:
                w = float(S[r, c])
                if w >= SIM_FLOOR:
                    rows.append((uids[i0 + r], uids[int(c)], "sim", round(w, 4)))
        con.executemany("INSERT OR REPLACE INTO g.edges (src,dst,kind,w) VALUES (?,?,?,?)",
                        rows)
        kept += len(rows)
        if (i0 // BLOCK) % 5 == 0:
            el = time.monotonic() - t0
            log(f"[graph] {i1:,}/{n:,} rows - {kept:,} edges - {el:.0f}s")
    con.commit()
    log(f"[graph] sim: {kept:,} edges")


def build_cites(con, args):
    """OpenAlex referenced_works, keyed on DOI, internal endpoints only."""
    con.executescript(_SCHEMA)
    by_doi, uid_of = {}, {}
    for uid, meta in con.execute("SELECT uid, meta FROM items"):
        try:
            d = json.loads(meta)
        except Exception:                            # noqa: BLE001
            continue
        doi = (d.get("doi") or "").lower().strip()
        if doi:
            by_doi[doi] = uid
            uid_of[uid] = doi
    log(f"[graph] {len(by_doi):,} items joinable by DOI")

    # OpenAlex work-id -> our uid, so a reference can be resolved to a row we
    # actually hold. Built from the same DOI set in one pass.
    todo = sorted(by_doi)
    if args.limit:
        todo = todo[:args.limit]
    oa_to_uid, refs = {}, {}
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        try:
            r = requests.get(
                "https://api.openalex.org/works",
                params={"filter": "doi:" + "|".join(chunk),
                        "select": "id,doi,referenced_works",
                        "per-page": 50, "mailto": MAILTO},
                headers=UA, timeout=60)
            if not r.ok:
                continue
            for w in (r.json().get("results") or []):
                doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
                uid = by_doi.get(doi)
                if not uid:
                    continue
                oa_to_uid[w.get("id")] = uid
                refs[uid] = w.get("referenced_works") or []
        except Exception as e:                       # noqa: BLE001
            log(f"[graph] batch {i//50} failed: {type(e).__name__}")
        if (i // 50) % 20 == 0:
            log(f"[graph] {min(i+50,len(todo)):,}/{len(todo):,} DOIs resolved")
        time.sleep(0.35)

    con.executescript(_CITES_SCHEMA)
    con.execute("DELETE FROM cites WHERE 1=1")
    rows, outside = [], 0
    for uid, rs in refs.items():
        for oa in rs:
            dst = oa_to_uid.get(oa)
            if dst and dst != uid:
                rows.append((uid, dst, "cites", 1.0))
            else:
                outside += 1
    con.executemany("INSERT OR REPLACE INTO cites (src,dst) VALUES (?,?)",
                    [(a, b) for a, b, _, _ in rows])
    con.commit()
    total = len(rows) + outside
    log(f"[graph] cites: {len(rows):,} internal edges "
        f"({100*len(rows)/max(1,total):.1f}% of {total:,} references seen)")


def report(con, args):
    """The gate. Nothing should consume the graph before these numbers are read:
    a sparse citation layer means traversal adds latency and no recall."""
    con.executescript(_SCHEMA)
    n_items = con.execute("SELECT count(*) FROM items").fetchone()[0]
    log(f"archive: {n_items:,} papers\n")
    con.executescript(_CITES_SCHEMA)
    for kind in ("sim", "cites"):
        cnt = (con.execute("SELECT count(*) FROM g.edges WHERE kind='sim'").fetchone()[0]
               if kind == "sim" else
               con.execute("SELECT count(*) FROM cites").fetchone()[0])
        if not cnt:
            log(f"{kind:<6} no edges built")
            continue
        deg = collections.Counter()
        q = ("SELECT src FROM g.edges WHERE kind='sim'" if kind == "sim"
             else "SELECT src FROM cites")
        for (src,) in con.execute(q):
            deg[src] += 1
        d = sorted(deg.values())
        cov = 100.0 * len(deg) / max(1, n_items)
        med = d[len(d) // 2]
        p90 = d[int(len(d) * 0.9)]
        w = (con.execute("SELECT avg(w),min(w),max(w) FROM g.edges WHERE kind='sim'")
             .fetchone() if kind == "sim" else (1.0, 1.0, 1.0))
        log(f"{kind:<6} {cnt:>8,} edges   {len(deg):>6,} papers with any "
            f"({cov:.1f}% coverage)")
        log(f"       degree  median {med}  p90 {p90}  max {max(d)}")
        if kind == "sim":
            log(f"       cosine  mean {w[0]:.3f}  min {w[1]:.3f}  max {w[2]:.3f}")
        log("")


def export(con, args):
    """docs/edges.bin -- packed row-index pairs against docs/vec.json's uids, so
    the browser can traverse without another lookup table."""
    meta = json.loads(pathlib.Path("docs/vec.json").read_text(encoding="utf-8"))
    row = {u: i for i, u in enumerate(meta["uids"])}
    out = bytearray()
    kept = collections.Counter()
    con.executescript(_CITES_SCHEMA)
    for kind, code in (("sim", 0), ("cites", 1)):
        q = ("SELECT src,dst,w FROM g.edges WHERE kind='sim'" if kind == "sim"
             else "SELECT src,dst,1.0 FROM cites")
        for src, dst, w in con.execute(q):
            a, b = row.get(src), row.get(dst)
            if a is None or b is None:
                continue
            out += struct.pack("<IIBf", a, b, code, w or 1.0)
            kept[kind] += 1
    p = pathlib.Path("docs/edges.bin")
    p.write_bytes(bytes(out))
    log(f"[graph] wrote {p} - {sum(kept.values()):,} edges "
        f"({dict(kept)}), {len(out)/1e6:.1f} MB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("sim", "cites", "report", "export"))
    ap.add_argument("--limit", type=int, default=0, help="cites: cap DOIs looked up")
    args = ap.parse_args()
    con = graph_con(store.connect())
    {"sim": build_sim, "cites": build_cites,
     "report": report, "export": export}[args.action](con, args)


if __name__ == "__main__":
    main()
