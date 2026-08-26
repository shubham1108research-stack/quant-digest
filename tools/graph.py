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
import concurrent.futures
import json
import pathlib
import struct
import sys
import time
import threading

import numpy as np
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store
from progress import Progress   # noqa: E402

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
ROOT = pathlib.Path(__file__).resolve().parent.parent


def graph_con(con):
    """A connection with the graph attached, so joins against items still work."""
    con.execute("ATTACH DATABASE ? AS g", (GRAPH_DB,))
    return con

K = 15                      # neighbours kept per paper
# On CENTRED cosine (see _vectors); the raw-space equivalent drops nothing.
#
# MEASURED across both embedding providers on this corpus, median 15th-NN
# cosine, 800-row sample:
#     mistral-embed @1024   raw 0.865 -> centred 0.375   floor 0.30 keeps 94%
#     text-embed-3-small@256 raw 0.638 -> centred 0.394  floor 0.30 keeps 99%
#
# So the constant TRANSFERS: OpenAI's space is much less anisotropic to start
# with, but after centring the two distributions land within 0.02 of each
# other. The new graph is slightly denser (99% vs 94% of candidate edges), not
# broken. Re-measure with `graph.py probe` after any embedding change rather
# than assuming that again -- it held here, it need not hold next time.
SIM_FLOOR = 0.30
BLOCK = 512                 # rows per similarity block; 512x11507 float32 = 23 MB
MAILTO = "upadhyays1108@gmail.com"
UA = {"User-Agent": f"quant-digest/1.0 (mailto:{MAILTO})"}

_REFS_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_refs (
  src TEXT NOT NULL,          -- a uid we hold
  ref TEXT NOT NULL,          -- an OpenAlex work id it cites, held or not
  PRIMARY KEY (src, ref)
);
CREATE INDEX IF NOT EXISTS paper_refs_ref ON paper_refs(ref);
"""

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


def _current_model(con) -> tuple[str, int]:
    """(model, dim) the LIVE index was built with.

    Read from docs/vec.json -- the manifest tools/embed.py writes beside the
    index -- and only from the cache as a fallback. The model was hardcoded
    here as 'mistral-embed', so when the embedder changed the graph carried on
    being built from the old provider's vectors: neighbours computed in one
    vector space, retrieval running in another, and nothing to say so. A second
    place naming the model is a second place for it to drift, which is exactly
    why the manifest exists.
    """
    man = ROOT / "docs" / "vec.json"
    if man.exists():
        try:
            m = json.loads(man.read_text(encoding="utf-8"))
            if m.get("model") and m.get("dim"):
                return m["model"], int(m["dim"])
        except Exception:                              # noqa: BLE001
            pass
    row = con.execute(
        "SELECT model, dim, COUNT(*) c FROM embeddings "
        "GROUP BY model, dim ORDER BY c DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("no embeddings cached -- run tools/embed.py first")
    log(f"[graph] no docs/vec.json; falling back to the most-populated cache "
        f"entry: {row[0]} @ {row[1]}d")
    return row[0], int(row[1])


def _vectors(con):
    """Every cached vector, as a unit-normalised float32 matrix."""
    model, dim_want = _current_model(con)
    rows = con.execute(
        "SELECT uid, vec FROM embeddings WHERE model=? AND dim=? ORDER BY uid",
        (model, dim_want),
    ).fetchall()
    if not rows:
        raise SystemExit(
            f"no embeddings cached for {model} @ {dim_want}d -- run "
            "tools/embed.py first. (This graph is derived from the vectors; it "
            "cannot be built from a model that is no longer in use.)")
    dim = len(rows[0][1])
    uids = [r[0] for r in rows]
    X = np.empty((len(rows), dim), dtype=np.float32)
    for i, (_, blob) in enumerate(rows):
        X[i] = np.frombuffer(blob, dtype=np.int8).astype(np.float32)
    # CENTRE before normalising. Embedding spaces are anisotropic -- every
    # vector shares a large common direction -- so raw cosine between two
    # unrelated papers is already high and a similarity floor is meaningless
    # without this. Measured on mistral-embed over this corpus, subtracting the
    # mean took the 15-NN cosine from mean 0.864 / spread 0.26 to mean 0.439 /
    # spread 0.78, and changed a quarter of the neighbours. Without it, label
    # propagation smears a tag across the whole archive instead of a
    # neighbourhood.
    #
    # NOTE those numbers were measured on mistral-embed, which is no longer the
    # provider. Centring is the right operation for any embedding space, but
    # SIM_FLOOR was tuned against that specific distribution -- `graph.py probe`
    # re-derives it for whatever model is live. Measured for both providers at
    # the SIM_FLOOR definition above; it transferred, but that was checked
    # rather than assumed.
    X -= X.mean(axis=0, keepdims=True)
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    return uids, X


def probe_floor(con, args) -> None:
    """Measure the k-NN similarity distribution of the CURRENT vectors.

    SIM_FLOOR is a constant tuned against mistral-embed's anisotropy. Carrying
    it unchanged to a different embedding model is guesswork: too high and the
    graph empties out, too low and every paper is everyone's neighbour and the
    graph hop stops discriminating. Neither failure announces itself -- the
    graph just quietly gets less useful.

    Sample rather than compute the full matrix: 800 rows is plenty to see the
    distribution and costs a second instead of minutes.
    """
    uids, X = _vectors(con)
    n = len(uids)
    rng = np.random.default_rng(0)
    idx = rng.choice(n, size=min(800, n), replace=False)
    S = X[idx] @ X.T
    for r, i in enumerate(idx):
        S[r, i] = -1.0
    kth = np.partition(-S, K, axis=1)[:, :K]
    kth = -kth                                   # top-K similarities per row
    knn = kth[:, -1]                             # the Kth (weakest kept) one
    qs = np.percentile(knn, [10, 25, 50, 75, 90])
    log(f"[graph] {n:,} vectors, {X.shape[1]}-dim, sampled {len(idx)} rows")
    log(f"[graph] {K}th-neighbour cosine: "
        f"p10={qs[0]:.3f} p25={qs[1]:.3f} median={qs[2]:.3f} "
        f"p75={qs[3]:.3f} p90={qs[4]:.3f}")
    for f in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50):
        frac = float((kth >= f).mean())
        log(f"[graph]   floor {f:.2f} -> keeps {frac * 100:5.1f}% of candidate "
            f"edges (~{int(frac * K * n):,} total)")
    log("[graph] pick a floor that keeps most of a paper's real neighbours "
        "without admitting the whole corpus; the current constant is "
        f"SIM_FLOOR={SIM_FLOOR}")


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
    chunks = [todo[i:i + 50] for i in range(0, len(todo), 50)]

    # PARALLEL, because this is I/O bound and was not. 418 sequential calls,
    # each a network round trip plus a 0.35s sleep, is most of an hour spent
    # waiting. requests releases the GIL while a socket is open, so threads are
    # the right tool -- the work is latency, not computation.
    #
    # Six workers against OpenAlex's polite pool, which asks for a mailto and
    # tolerates this comfortably. The per-request sleep stays: it now spaces
    # each WORKER's calls rather than the whole run, so aggregate load is
    # bounded by workers/delay instead of 1/delay.
    lock = threading.Lock()
    prog = Progress(len(chunks), "graph-cites", every_s=45)

    def fetch(chunk):
        try:
            r = requests.get(
                "https://api.openalex.org/works",
                params={"filter": "doi:" + "|".join(chunk),
                        "select": "id,doi,referenced_works",
                        "per-page": 50, "mailto": MAILTO},
                headers=UA, timeout=60)
            if not r.ok:
                return
            got = []
            for w in (r.json().get("results") or []):
                doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
                uid = by_doi.get(doi)
                if not uid:
                    continue
                got.append((w.get("id"), uid, w.get("referenced_works") or []))
            # Both dicts mutated under one lock. CPython dict writes are
            # individually atomic, but "check then set" across two dicts is not,
            # and a torn pairing here would attribute one paper's references to
            # another -- silently, and only visible much later as a wrong edge.
            with lock:
                for oid, uid, rw in got:
                    oa_to_uid[oid] = uid
                    refs[uid] = rw
        except Exception as e:                       # noqa: BLE001
            log(f"[graph] a batch failed: {type(e).__name__}")
        finally:
            time.sleep(0.35)
            with lock:
                prog.tick()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(fetch, chunks))
    prog.done()

    con.executescript(_CITES_SCHEMA)
    con.executescript(_REFS_SCHEMA)
    con.execute("DELETE FROM cites WHERE 1=1")
    con.execute("DELETE FROM paper_refs WHERE 1=1")

    # KEEP THE WHOLE REFERENCE LIST, not only the edges that land inside the
    # archive. Measured on the first full run: 350,218 references seen, 29,639
    # internal -- so 91.5% of what OpenAlex had already returned was being
    # counted and dropped.
    #
    # The dropped part is the useful part. A citation edge needs BOTH papers
    # held, which is why the internal graph is 1.4 edges per paper and too thin
    # for authority to propagate. BIBLIOGRAPHIC COUPLING needs neither end
    # held: two papers we have are coupled when they cite the same outside
    # work, and that work can be anything. Same for co-citation, and for
    # "cites a classic" -- the classic must be identifiable, not present.
    #
    # ~350k rows is about 14 MB. That is the whole cost of turning a sparse
    # graph into a dense one from data already paid for.
    ref_rows = [(uid, oa) for uid, rs in refs.items() for oa in rs]
    con.executemany("INSERT OR IGNORE INTO paper_refs (src,ref) VALUES (?,?)",
                    ref_rows)

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
    log(f"[graph] paper_refs: {len(ref_rows):,} reference rows kept "
        f"across {len(refs):,} papers")

    # How dense would coupling be? A shared reference is one co-citation pair;
    # counting the pairs directly is O(n^2) in the worst case, so this reports
    # the ingredients instead -- how many outside works are cited by more than
    # one paper we hold, and how much pair mass they carry.
    shared = con.execute(
        "SELECT count(*) FROM (SELECT ref FROM paper_refs "
        "GROUP BY ref HAVING count(DISTINCT src) > 1)").fetchone()[0]
    pairs = con.execute(
        "SELECT COALESCE(SUM(c*(c-1)/2),0) FROM ("
        "  SELECT count(DISTINCT src) AS c FROM paper_refs"
        "  GROUP BY ref HAVING c > 1)").fetchone()[0]
    log(f"[graph] coupling: {shared:,} references are cited by MORE THAN ONE "
        f"paper we hold, carrying {pairs:,} coupled pairs")


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
    """docs/edges.bin -- packed against docs/vec.json's uid order so the browser
    can traverse without a lookup table.

    Layout: an 8-byte header then fixed-width records.

        magic  "QDG1"   4 bytes
        nodes  uint32           how many rows vec.json declares
        edges  uint32
        w      uint8            index width in bytes: 2 while nodes < 65536
        pad    3 bytes

        record: src[w] dst[w] kind:uint8 weight:uint8   (weight = cos * 255)

    That is 6 bytes a record at today's size against the 13 it started at
    (two uint32 and a float32). The archive is 11.5k papers, so a uint16 row
    index is ample and the header carries the width so it widens automatically
    rather than silently truncating if the archive passes 65,535.
    """
    meta = json.loads(pathlib.Path("docs/vec.json").read_text(encoding="utf-8"))
    row = {u: i for i, u in enumerate(meta["uids"])}
    n_nodes = len(meta["uids"])
    width = 2 if n_nodes < 65536 else 4
    fmt = "<HHBB" if width == 2 else "<IIBB"

    con.executescript(_CITES_SCHEMA)
    recs = bytearray()
    kept = collections.Counter()
    for kind, code in (("sim", 0), ("cites", 1)):
        q = ("SELECT src,dst,w FROM g.edges WHERE kind='sim'" if kind == "sim"
             else "SELECT src,dst,1.0 FROM cites")
        for src, dst, w in con.execute(q):
            a, b = row.get(src), row.get(dst)
            if a is None or b is None:
                continue
            recs += struct.pack(fmt, a, b, code,
                                max(0, min(255, int(round((w or 1.0) * 255)))))
            kept[kind] += 1

    head = b"QDG1" + struct.pack("<IIB3x", n_nodes, sum(kept.values()), width)
    p = pathlib.Path("docs/edges.bin")
    p.write_bytes(head + bytes(recs))
    log(f"[graph] wrote {p} - {sum(kept.values()):,} edges ({dict(kept)}), "
        f"{len(head)+len(recs):,} bytes at {width*2+2} b/edge")


def build_local(con, args):
    """A graph built straight from a docs directory's vec.bin. No database.

    WHY THIS IS SEPARATE FROM build_sim. build_sim reads the `embeddings`
    table, keyed by (model, dim), because that is where tools/embed.py caches
    what it paid for. tools/embed_local.py caches nothing -- a local model is
    free to re-run, so there is no reason to store its output in the DB and a
    good reason not to: state.db is pushed to R2 and shared, and a bake-off is
    an experiment, not a deployment.

    WHY IT EXISTS AT ALL. The bake-off used to measure both embedders with the
    graph hop disabled, because docs/edges.bin is derived from the OpenAI
    vectors and letting one model's neighbours rescue the other model's misses
    measures a hybrid nobody would ship. Disabling it is one way out; giving
    each model ITS OWN graph is the better one, because the graph is downstream
    of the embedding and a good embedder should produce a good neighbourhood.
    That also measures the configuration that actually ships, which is with the
    graph on.

    SIM_FLOOR is a tuned constant and it does NOT transfer for free -- it was
    derived against one model's anisotropy, and a different model at a
    different width will sit somewhere else. The edge count is logged loudly
    for exactly that reason: an order-of-magnitude difference between the two
    sides means the floor is wrong for one of them, not that its neighbourhood
    is better or worse. `graph.py probe` re-derives it per model.
    """
    docs = pathlib.Path(args.docs)
    man = json.loads((docs / "vec.json").read_text(encoding="utf-8"))
    uids, dim = man["uids"], int(man["dim"])
    raw = np.frombuffer((docs / "vec.bin").read_bytes(), dtype=np.int8)
    if raw.size != len(uids) * dim:
        raise SystemExit(
            "[graph] %s/vec.bin is %d bytes, but vec.json declares %d rows of "
            "%d -- the manifest and the matrix disagree, so every neighbour "
            "below would be a different paper than it claims."
            % (docs, raw.size, len(uids), dim))
    X = raw.reshape(len(uids), dim).astype(np.float32)
    # identical treatment to _vectors: centre, then normalise
    X -= X.mean(axis=0, keepdims=True)
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)

    floor = float(args.floor if args.floor is not None else SIM_FLOOR)
    n = len(uids)
    log(f"[graph] {n:,} vectors, {dim}-dim from {docs} -- kNN k={K}, "
        f"floor={floor} ({man.get('model','?')})")

    width = 2 if n < 65536 else 4
    fmt = "<HHBB" if width == 2 else "<IIBB"
    recs = bytearray()
    kept = 0
    t0 = time.monotonic()
    for i0 in range(0, n, BLOCK):
        i1 = min(i0 + BLOCK, n)
        S = X[i0:i1] @ X.T
        for r in range(i1 - i0):
            S[r, i0 + r] = -1.0
        top = np.argpartition(-S, K, axis=1)[:, :K]
        for r in range(i1 - i0):
            for c in top[r]:
                w = float(S[r, int(c)])
                if w >= floor:
                    recs += struct.pack(fmt, i0 + r, int(c), 0,
                                        max(0, min(255, int(round(w * 255)))))
                    kept += 1
        if (i0 // BLOCK) % 5 == 0:
            log(f"[graph] {i1:,}/{n:,} rows - {kept:,} edges - "
                f"{time.monotonic()-t0:.0f}s")

    head = b"QDG1" + struct.pack("<IIB3x", n, kept, width)
    out = docs / "edges.bin"
    out.write_bytes(head + bytes(recs))
    log(f"[graph] wrote {out} - {kept:,} sim edges ({kept/max(n,1):.1f} per "
        f"paper), {len(head)+len(recs):,} bytes")
    log("[graph] NOTE no citation edges: those come from the DB and are "
        "identical for both models, so they are not what a bake-off compares.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action",
                    choices=("sim", "cites", "report", "export", "probe",
                             "local"))
    ap.add_argument("--docs", default="docs",
                    help="local: directory holding vec.bin/vec.json to build "
                         "a graph from, and to write edges.bin into")
    ap.add_argument("--floor", type=float, default=None,
                    help="local: override SIM_FLOOR for a model whose "
                         "anisotropy differs")
    ap.add_argument("--limit", type=int, default=0, help="cites: cap DOIs looked up")
    args = ap.parse_args()
    if args.action == "local":
        # Reads vec.bin directly and writes edges.bin directly. Opening the
        # database would be pure ceremony, and worse than that: it would make a
        # read-only experiment look like something that touches shared state.
        return build_local(None, args)
    con = graph_con(store.connect())
    {"sim": build_sim, "cites": build_cites, "report": report,
     "export": export, "probe": probe_floor}[args.action](con, args)


if __name__ == "__main__":
    main()
