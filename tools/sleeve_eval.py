#!/usr/bin/env python3
"""Score the sleeve classifiers against NBER's own taxonomy.

`carry` holds 8 papers of 3,361 labelled. Two explanations demand opposite
fixes and nothing so far could tell them apart:

  A  the DEFINITION is too narrow -- the classifier is right about what it
     tags, it just tags almost nothing.        -> high precision, near-zero recall
  B  the TAXONOMY is ambiguous -- the boundary is not one a reader can apply
     consistently, so labels land anywhere.    -> low precision

Ground truth is NBER's own topic assignment (tools/nber_topics.py), mapped to
sleeves. It is partial and it has no carry topic -- so read the macro sleeves
as calibration and treat carry separately, via tools/carry_probe.py.

Compares two classifiers on the same papers:
  sleeves        the LLM rubric
  sleeves_prop   label propagation over the similarity graph

  python tools/sleeve_eval.py
"""

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config     # noqa: E402
import store      # noqa: E402
from propagate import TOPIC_SLEEVE   # noqa: E402


def log(m):
    print(m, flush=True)


def prf(pred, gold):
    tp = len(pred & gold)
    p = tp / len(pred) if pred else None
    r = tp / len(gold) if gold else None
    f = (2 * p * r / (p + r)) if (p and r) else 0.0
    return p, r, f, tp


def main():
    con = store.connect()
    gold, llm, prop = {}, {}, {}
    for uid, meta in con.execute("SELECT uid, meta FROM items"):
        try:
            d = json.loads(meta)
        except Exception:                             # noqa: BLE001
            continue
        topics = d.get("nber_topics") or []
        if topics:
            g = {s for t in topics for s in TOPIC_SLEEVE.get(t, [])}
            if g:
                gold[uid] = g
        if d.get("sleeves"):
            llm[uid] = {s for s in d["sleeves"] if s != "other"}
        if d.get("sleeves_prop"):
            prop[uid] = set(d["sleeves_prop"])

    log(f"gold (NBER topics -> sleeves) : {len(gold):,} papers")
    log(f"  also carrying LLM sleeves   : {len(set(gold) & set(llm)):,}")
    log(f"  also carrying propagated    : {len(set(gold) & set(prop)):,}")
    if not gold:
        raise SystemExit("no nber_topics found -- run tools/nber_topics.py first")

    for name, pred in (("LLM rubric", llm), ("propagated", prop)):
        both = set(gold) & set(pred)
        if not both:
            log(f"\n{name}: no overlap with the gold set")
            continue
        log(f"\n{name}  (n={len(both):,})")
        log(f"  {'sleeve':<16} {'prec':>6} {'recall':>7} {'F1':>6} "
            f"{'gold':>6} {'pred':>6}   verdict")
        for sl in config.SLEEVES:
            if sl == "other":
                continue
            g = {u for u in both if sl in gold[u]}
            p_ = {u for u in both if sl in pred.get(u, ())}
            if not g and not p_:
                continue
            p, r, f, _ = prf(p_, g)
            # the diagnosis this file exists to produce
            if not g:
                verdict = "not in gold"
            elif r is not None and r < 0.15 and (p or 0) >= 0.5:
                verdict = "DEFINITION too narrow"
            elif (p or 0) < 0.4 and len(p_) > 5:
                verdict = "TAXONOMY ambiguous"
            elif r is not None and r >= 0.5 and (p or 0) >= 0.5:
                verdict = "healthy"
            else:
                verdict = "-"
            log(f"  {sl:<16} {('%.2f'%p) if p is not None else '   - ':>6} "
                f"{('%.2f'%r) if r is not None else '   - ':>7} {f:>6.2f} "
                f"{len(g):>6} {len(p_):>6}   {verdict}")

    # where does carry actually go? carry_probe predicts rates_credit/vol_options
    log("\ncarry: what the LLM assigned to papers whose text is unambiguously carry")
    from propagate import PHRASE_SEED
    phrases = PHRASE_SEED["carry"]
    seen = collections.Counter()
    n = 0
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        d = json.loads(meta)
        text = f"{title or ''} {d.get('abstract') or ''}".lower()
        if not any(p in text for p in phrases):
            continue
        n += 1
        for s in (d.get("sleeves") or ["(unscored)"]):
            seen[s] += 1
    log(f"  {n:,} papers contain an unambiguous carry phrase")
    for s, c in seen.most_common(8):
        log(f"    {s:<16} {c:>5}  ({100*c/max(1,n):.0f}%)")


if __name__ == "__main__":
    main()
