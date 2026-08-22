#!/usr/bin/env python3
"""A knowledge map of the archive: docs/map.json for the portal's Map view.

This is OpenKnowledgeMaps/Headstart's output without its stack. Headstart needs
a Node frontend, a Python backend, Docker and a host; the portal is static
files on Cloudflare Pages. Everything a map needs is already here -- 11.5k
cached embeddings and a similarity matrix tools/embed_ab.py builds and discards.

Deliberately no new dependency. The projection is PCA via numpy's SVD, and the
clustering is k-means, both in a few lines. A neighbour-embedding method (UMAP,
t-SNE) would separate the clusters better -- PCA is linear and preserves global
variance rather than local neighbourhoods -- but neither ships with numpy and
sklearn is ~100 MB for one function. If the map turns out to be worth it, that
is the upgrade to make.

The point is diagnostic, not decorative: if the desk sleeves describe something
real, papers carrying a sleeve should occupy a region. `carry` scattering
uniformly is independent evidence that the taxonomy is wrong rather than the
classifier -- a second opinion on the question tools/sleeve_eval.py asks.

  python tools/map.py --clusters 24
"""

import argparse
import collections
import json
import pathlib
import re
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store   # noqa: E402
from graph import _vectors   # noqa: E402  (same centred vectors as the graph)

OUT = pathlib.Path("docs/map.json")
STOP = set("""the a an and or of for in on with to from by is are was were be been
this that these those we our their its it as at using use used study studies
paper papers evidence new model models data results effect effects analysis
approach method methods based show shows find finds finding findings""".split())


def log(m):
    print(m, flush=True)


def kmeans(X, k, iters=40, seed=7):
    """Plain Lloyd's algorithm. k-means++ init so a bad seed does not produce
    one giant cluster and k-1 singletons."""
    rng = np.random.default_rng(seed)
    C = np.empty((k, X.shape[1]), dtype=np.float32)
    C[0] = X[rng.integers(len(X))]
    d2 = ((X - C[0]) ** 2).sum(1)
    for i in range(1, k):
        C[i] = X[rng.choice(len(X), p=d2 / d2.sum())]
        d2 = np.minimum(d2, ((X - C[i]) ** 2).sum(1))
    lab = np.zeros(len(X), dtype=np.int32)
    for _ in range(iters):
        # (n,k) distances without materialising (n,k,d)
        D = (X * X).sum(1)[:, None] - 2 * X @ C.T + (C * C).sum(1)[None, :]
        new = D.argmin(1)
        if (new == lab).all():
            break
        lab = new
        for i in range(k):
            m = lab == i
            if m.any():
                C[i] = X[m].mean(0)
    return lab


def label_of(titles):
    """Name a cluster by the words that distinguish it, not the ones it has
    most of -- otherwise every cluster is called 'returns'."""
    words = collections.Counter()
    for t in titles:
        for w in re.findall(r"[a-z]{4,}", (t or "").lower()):
            if w not in STOP:
                words[w] += 1
    return ", ".join(w for w, _ in words.most_common(3)) or "misc"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clusters", type=int, default=24)
    args = ap.parse_args()

    con = store.connect()
    uids, X = _vectors(con)
    log(f"[map] {len(uids):,} vectors, {X.shape[1]} dims")

    # PCA: the top two right-singular vectors of the centred matrix
    U, S, Vt = np.linalg.svd(X - X.mean(0), full_matrices=False)
    xy = (X - X.mean(0)) @ Vt[:2].T
    var = (S[:2] ** 2).sum() / (S ** 2).sum()
    log(f"[map] PCA holds {100*var:.1f}% of variance in 2 components "
        f"(low is expected for text; the map is a layout, not a metric)")
    # scale to a stable [-1,1] box so the client needs no autoscaling
    xy = xy / (np.abs(xy).max(0) + 1e-9)

    lab = kmeans(X, args.clusters)
    log(f"[map] {args.clusters} clusters, sizes "
        f"{sorted(collections.Counter(lab.tolist()).values(), reverse=True)[:6]}...")

    meta = {}
    for uid, title, m in con.execute("SELECT uid, title, meta FROM items"):
        try:
            d = json.loads(m)
        except Exception:                            # noqa: BLE001
            d = {}
        meta[uid] = (title or "", d.get("sleeves") or [],
                     d.get("sleeves_prop") or [], d.get("desk_fit") or 0)

    pts, by_cluster = [], collections.defaultdict(list)
    for i, uid in enumerate(uids):
        t, sl, slp, fit = meta.get(uid, ("", [], [], 0))
        by_cluster[int(lab[i])].append(t)
        pts.append({"u": uid, "t": t[:120],
                    "x": round(float(xy[i, 0]), 4), "y": round(float(xy[i, 1]), 4),
                    "c": int(lab[i]), "s": sl, "p": slp, "f": fit})
    clusters = [{"c": c, "n": len(v), "label": label_of(v)}
                for c, v in sorted(by_cluster.items())]
    for c in sorted(clusters, key=lambda x: -x["n"])[:8]:
        log(f"    cluster {c['c']:>2}  {c['n']:>5}  {c['label']}")

    OUT.write_text(json.dumps({"n": len(pts), "clusters": clusters, "p": pts}),
                   encoding="utf-8")
    log(f"[map] wrote {OUT} ({OUT.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
