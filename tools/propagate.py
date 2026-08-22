#!/usr/bin/env python3
"""Spread sleeve labels across the similarity graph instead of guessing each
paper alone.

The per-paper classifier failed three times on the same sleeve: a keyword pass
found 2 carry papers of 162, an embedding pass drifted carry into EU fiscal
integration, and the LLM found 8 of 3,361. Each attempt asked the same question
-- "what is this paper, in isolation?" -- against eleven prose definitions
buried in an 18,600-character prompt.

Propagation asks a different question. Take the papers we are CONFIDENT about,
and let the label flow to their neighbours, weighted by how similar they
actually are. A paper with six carry neighbours is carry even if its own
abstract never says "roll yield".

Seeds, in descending authority:
  1. nber_topics   NBER's editors, mapped onto sleeves (tools/nber_topics.py)
  2. probe phrases the unambiguous vocabulary from sleeve_check.py's CARRY_PROBE
  3. LLM sleeves   weak: it is the thing being corrected, so it seeds at low
                   weight and only where it is not the sole voice

Writes `sleeves_prop` and `sleeves_prop_conf`, never touching `sleeves` --
they have to stay comparable for tools/sleeve_eval.py to mean anything.

  python tools/propagate.py --dry-run
  python tools/propagate.py --rounds 6
"""

import argparse
import collections
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config   # noqa: E402
import store    # noqa: E402
from graph import graph_con   # noqa: E402

# NBER topic -> desk sleeve. Deliberately partial: a topic with no honest
# sleeve is better left unmapped than forced.
TOPIC_SLEEVE = {
    "Portfolio Selection and Asset Pricing": ["equity_xs", "cross_asset"],
    "Financial Markets": ["microstructure"],
    "Monetary Policy": ["macro_regime", "rates_credit"],
    "Money and Interest Rates": ["rates_credit"],
    "Business Cycles": ["macro_regime"],
    "Macroeconomic Models": ["macro_regime"],
    "International Finance": ["fx"],
    "International Macroeconomics": ["fx", "macro_regime"],
    "Behavioral Finance": ["equity_xs"],
    "Financial History": ["macro_regime"],
    "Macroeconomic History": ["macro_regime"],
}

# Unambiguous vocabulary. These are the phrases sleeve_check.py:35-37 keeps as
# its carry probe -- the exact set a keyword classifier fired on wrongly, which
# makes them a good SEED (high precision) and a bad classifier (low recall).
PHRASE_SEED = {
    "carry": ["convenience yield", "forward premium", "roll yield", "backwardation",
              "carry trade", "uncovered interest", "term premium", "currency carry"],
    "trend_cta": ["time-series momentum", "time series momentum", "trend-following",
                  "managed futures", "moving average rule", "crisis alpha"],
    "vol_options": ["variance risk premium", "implied volatility", "volatility surface",
                    "option-implied"],
    "commodities": ["theory of storage", "hedging pressure", "futures curve",
                    "convenience yield"],
    "microstructure": ["order flow", "limit order book", "market impact",
                       "bid-ask spread", "price impact"],
}

W_NBER, W_PHRASE, W_LLM = 1.0, 0.9, 0.35
KEEP = 0.35             # final score needed to claim a sleeve
DECAY = 0.55            # how much of a neighbour's mass carries per hop


def log(m):
    print(m, flush=True)


def _seeds(con):
    """uid -> {sleeve: weight}, from the three sources."""
    seeds = collections.defaultdict(dict)
    n_nber = n_phrase = n_llm = 0
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            d = json.loads(meta)
        except Exception:                             # noqa: BLE001
            continue
        for t in (d.get("nber_topics") or []):
            for sl in TOPIC_SLEEVE.get(t, []):
                seeds[uid][sl] = max(seeds[uid].get(sl, 0), W_NBER)
                n_nber += 1
        text = f"{title or ''} {d.get('abstract') or d.get('summary') or ''}".lower()
        for sl, phrases in PHRASE_SEED.items():
            if any(p in text for p in phrases):
                seeds[uid][sl] = max(seeds[uid].get(sl, 0), W_PHRASE)
                n_phrase += 1
        for sl in (d.get("sleeves") or []):
            if sl != "other":
                seeds[uid].setdefault(sl, W_LLM)
                n_llm += 1
    log(f"[prop] seeds: {n_nber} from NBER topics, {n_phrase} from phrases, "
        f"{n_llm} from LLM labels -> {len(seeds):,} papers")
    return seeds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = graph_con(store.connect())
    adj = collections.defaultdict(list)
    for src, dst, w in con.execute("SELECT src,dst,w FROM g.edges WHERE kind='sim'"):
        adj[src].append((dst, w))
        adj[dst].append((src, w))          # similarity is symmetric
    if not adj:
        raise SystemExit("no sim edges -- run tools/graph.py sim first")
    log(f"[prop] graph: {len(adj):,} papers, "
        f"{sum(len(v) for v in adj.values())//2:,} undirected edges")

    score = {u: dict(s) for u, s in _seeds(con).items()}
    seeded = set(score)

    for rnd in range(args.rounds):
        nxt = collections.defaultdict(lambda: collections.defaultdict(float))
        for u, sl in score.items():
            for v, w in adj.get(u, ()):
                for k, val in sl.items():
                    nxt[v][k] += val * w * DECAY
        moved = 0
        for v, sl in nxt.items():
            # normalise so a paper with many neighbours does not simply win
            deg = sum(w for _, w in adj.get(v, ())) or 1.0
            for k, val in sl.items():
                new = val / deg
                if v in seeded:                       # seeds keep their authority
                    new = max(new, score[v].get(k, 0))
                if new > score.setdefault(v, {}).get(k, 0):
                    score[v][k] = new
                    moved += 1
        log(f"[prop] round {rnd+1}: {moved:,} label updates, "
            f"{len(score):,} papers carrying mass")

    out, dist = {}, collections.Counter()
    for u, sl in score.items():
        keep = sorted([(v, k) for k, v in sl.items() if v >= KEEP], reverse=True)
        keep = keep[:config.SLEEVES_MAX]
        if keep:
            out[u] = ([k for _, k in keep], round(keep[0][0], 3))
            for _, k in keep:
                dist[k] += 1
    log(f"\n[prop] {len(out):,} papers labelled (threshold {KEEP})")
    for k in config.SLEEVES:
        log(f"    {k:<16} {dist.get(k,0):>6}")

    if args.dry_run:
        log("[prop] dry run -- nothing written")
        return
    n = 0
    for uid, (sleeves, conf) in out.items():
        if store.update_meta(con, uid, {"sleeves_prop": sleeves,
                                        "sleeves_prop_conf": conf}):
            n += 1
    con.commit()
    log(f"[prop] wrote sleeves_prop onto {n:,} papers")


if __name__ == "__main__":
    main()
