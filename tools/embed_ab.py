#!/usr/bin/env python3
"""A/B an embedding model against the one in production, on THIS archive.

"Better embeddings" is unfalsifiable without a metric, so this scores two
properties that matter for how the index is actually used, both using labels
the archive already holds -- no hand-labelling, no judgement calls:

  TOPIC COHERENCE @5
    For a paper with an LLM-assigned topic, how many of its 5 nearest
    neighbours share that topic? Measures whether the space organises by
    subject. Directly predicts Ask's paper-level recall quality.

  SLEEVE PURITY @10
    Seed papers whose TITLE unambiguously marks a desk sleeve (trend, carry,
    rates...) should sit near other papers of that sleeve. Measures whether the
    space separates the distinctions this desk cares about -- which general
    models have no particular reason to do, and a finance-domain model should.

Run with no arguments to score the production index. Pass --voyage to embed the
same papers with voyage-finance-2 and print both side by side.

  python tools/embed_ab.py                 # baseline only (free, offline)
  python tools/embed_ab.py --voyage        # needs VOYAGE_API_KEY
"""

import argparse
import json
import os
import re
import sqlite3
import struct
import sys
import time

import numpy as np
import requests

SLEEVE_SEEDS = {
    "Trend / CTA": ["trend follow", "time series momentum", "managed futures"],
    "Carry": ["carry trade", "currency carry", "convenience yield",
              "forward premium"],
    "FX": ["exchange rate", "foreign exchange"],
    "Rates & Credit": ["term premium", "yield curve", "credit spread"],
    "Commodities": ["commodity futures", "backwardation", "oil price"],
    "Macro regime": ["monetary policy", "business cycle", "nowcast"],
    "Volatility & Options": ["implied volatility", "realized volatility",
                             "option pricing"],
    "Microstructure": ["market microstructure", "order flow", "bid-ask"],
}
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-finance-2"
MAX_CHARS = 1600          # matches tools/embed.py, so the comparison is fair


def log(m):
    print(m, flush=True)


def norm_title(s):
    return re.sub(r"[^a-z0-9 -]", " ", (s or "").lower())


def load_corpus(con):
    """Same text construction as tools/embed.py -- otherwise the A/B measures
    the text recipe rather than the model."""
    out = {}
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            m = json.loads(meta)
        except Exception:                                   # noqa: BLE001
            m = {}
        body = (m.get("abstract") or "").strip() or (m.get("summary") or "").strip()
        topic = (m.get("topic") or "").strip()
        parts = [title or ""]
        if topic:
            parts.append(topic)
        if body:
            parts.append(body)
        out[uid] = {"title": title or "", "topic": topic,
                    "text": " \n".join(p for p in parts if p)[:MAX_CHARS]}
    return out


def unit(a):
    return a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)


def score(name, uids, X, corpus):
    X = unit(X.astype(np.float32))
    S = X @ X.T
    np.fill_diagonal(S, -1.0)                # never count a paper as its own neighbour

    # --- topic coherence @5 ---
    topics = np.array([corpus[u]["topic"] for u in uids])
    has = np.where(topics != "")[0]
    nn5 = np.argsort(-S[has], axis=1)[:, :5]
    coh = float(np.mean([np.mean(topics[nn5[i]] == topics[h])
                         for i, h in enumerate(has)]))

    # --- sleeve purity @10 ---
    pos = {u: i for i, u in enumerate(uids)}
    per, total = {}, []
    for sleeve, pats in SLEEVE_SEEDS.items():
        sel = [pos[u] for u in uids
               if any(p in norm_title(corpus[u]["title"]) for p in pats)]
        if len(sel) < 4:
            continue
        sel_set = set(sel)
        nn10 = np.argsort(-S[sel], axis=1)[:, :10]
        pur = float(np.mean([np.mean([n in sel_set for n in row]) for row in nn10]))
        per[sleeve] = (pur, len(sel))
        total.append(pur)

    log(f"\n=== {name} ===")
    log(f"  topic coherence @5 : {coh*100:5.1f}%   "
        f"(neighbours sharing the paper's topic, n={len(has)})")
    log(f"  sleeve purity  @10 : {np.mean(total)*100:5.1f}%   "
        f"(seed neighbours in the same sleeve)")
    for s, (p, n) in sorted(per.items(), key=lambda kv: -kv[1][0]):
        log(f"      {s:<22} {p*100:5.1f}%  (n={n})")
    return {"coherence": coh, "purity": float(np.mean(total)), "per": per}


def voyage_embed(texts, key):
    out = []
    B = 64
    for i in range(0, len(texts), B):
        chunk = texts[i:i + B]
        for attempt in range(5):
            r = requests.post(VOYAGE_URL,
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"},
                              json={"model": VOYAGE_MODEL, "input": chunk,
                                    "input_type": "document"},
                              timeout=120)
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            if not r.ok:
                raise SystemExit(f"voyage HTTP {r.status_code}: {r.text[:300]}")
            out += [d["embedding"] for d in
                    sorted(r.json()["data"], key=lambda d: d["index"])]
            break
        if (i // B) % 10 == 0:
            log(f"  voyage {min(i+B, len(texts))}/{len(texts)}")
        time.sleep(0.3)
    return np.array(out, dtype=np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voyage", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="sample N papers (keeps a voyage trial cheap)")
    args = ap.parse_args()

    con = sqlite3.connect("state.db")
    corpus = load_corpus(con)
    rows = con.execute("SELECT uid, vec FROM embeddings WHERE dim=1024").fetchall()
    rows = [(u, v) for u, v in rows if u in corpus]
    if args.limit:
        rows = rows[:args.limit]
    uids = [u for u, _ in rows]
    base = np.array([struct.unpack("1024b", v) for _, v in rows], dtype=np.float32)
    log(f"corpus: {len(uids)} papers with a cached vector")
    a = score("mistral-embed (in production)", uids, base, corpus)

    if not args.voyage:
        log("\n(baseline only -- pass --voyage with VOYAGE_API_KEY set to compare)")
        return
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        raise SystemExit("VOYAGE_API_KEY not set")
    log(f"\nembedding the same {len(uids)} papers with {VOYAGE_MODEL} ...")
    vx = voyage_embed([corpus[u]["text"] for u in uids], key)
    if vx.shape[0] != len(uids):
        raise SystemExit(f"got {vx.shape[0]} vectors for {len(uids)} papers")
    log(f"  returned {vx.shape[1]}-dim vectors")
    b = score(f"{VOYAGE_MODEL}", uids, vx, corpus)

    log("\n=== verdict ===")
    dc = (b["coherence"] - a["coherence"]) * 100
    dp = (b["purity"] - a["purity"]) * 100
    log(f"  topic coherence : {dc:+.1f} pts")
    log(f"  sleeve purity   : {dp:+.1f} pts")
    log("  -> switch" if (dc + dp) > 1.0 else
        "  -> not worth the migration on these numbers")


if __name__ == "__main__":
    main()
