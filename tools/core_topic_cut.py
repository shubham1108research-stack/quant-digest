#!/usr/bin/env python3
"""Remove candidates whose OpenAlex TOPIC is off-desk, by a named list.

WHY A LIST OF NAMES AND NOT A RULE. The obvious rule -- drop anything OpenAlex
files outside "Economics, Econometrics and Finance" -- deletes canonical
finance. Measured on this pool: Merton's "Lifetime Portfolio Selection by
Dynamic Stochastic Programming" is filed under Management Science, "Rollover
Risk and Credit Risk" under MEDICINE, and the Lucas critique under
Agricultural and Biological Sciences. Field-level labels are wrong in both
directions often enough that no threshold on them is safe.

The TOPIC names are a different matter. They are specific enough to read and
judge one at a time -- "Electric Power System Optimization" is not a borderline
call -- so the decision lives in data/core_topic_drops.csv where a person made
it, not in a heuristic here. Extending the cut means adding a row to that file.

THE INTERSECTION WITH THE KEYWORD GATE DOES NOT WORK, and it is worth recording
why. Requiring both an off-domain topic AND no finance vocabulary in the title
cuts 7 papers, of which "Two-Way Fixed Effects and Differences-in-Differences"
and "Income Smoothing and Consumption Smoothing" are ones we want. Meanwhile it
SPARES "Climate Change Impacts on Global Food Security", because clean_core's
keep-list contains `climate` and `insur`. Where the two disagree the topic is
usually right and the vocabulary usually wrong, so this does not defer to it.

PROTECTION IS UNCHANGED. A row held in the archive, found by two or more
routes, cited by a seed, or contributed by a curated route is never removed on
a topic label alone -- the same rule clean_core.py uses, for the same reason:
independent evidence outranks a single automated classification, and that
classification has a measured error rate.

    python tools/core_topic_cut.py --inventory   # every topic + counts, review
    python tools/core_topic_cut.py --dry-run     # what the list would remove
    python tools/core_topic_cut.py               # apply it
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

import textnorm                                              # noqa: E402

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
STRAYS = OUT / "core_strays.csv"
TOPICS = OUT / "core_topics.json"
INVENTORY = OUT / "core_topic_inventory.csv"
METRICS = OUT / "core_graph_metrics.json"
FORWARD = OUT / "core_forward_graph.json"
DROPS = pathlib.Path("data") / "core_topic_drops.csv"

CURATED = {"canon", "nber", "snowball", "pwb", "quantseeker", "authors",
           "signaldoc"}

# RESCUE: title carries one of these and the row is kept even under a DROP
# topic. Measured before this existed: the seven approved DROP topics remove
# 14,453 unprotected rows, and 2,917 of them (20.2%) name something this desk
# actually trades -- "Blockchain Technology Applications and Security" turned
# out to be mostly Bitcoin volatility and return-prediction papers (GARCH
# models, safe-haven tests, bubble dating), and "Sustainable Finance and Green
# Bonds" caught 220 monetary-policy and 117 central-bank papers that happen to
# mention climate. The topic name describes the bucket's centroid, not every
# row in it -- a paper is filed on ITS title+abstract, and a title using desk
# vocabulary is stronger evidence than the bucket it landed in.
#
# This is deliberately NARROWER than clean_core.FIN. That list is broad enough
# to rescue "Climate Change Impacts on Global Food Security" via `climate` and
# `insur`, which is the wrong answer for exactly the population this cut is
# aimed at. RESCUE holds only terms this desk trades or watches directly.
RESCUE = [
    "crypto", "cryptocurren", "bitcoin", "ethereum", "digital asset",
    "volatility", "garch", "stochastic volatility", "realized volatility",
    "monetary policy", "central bank", "federal reserve", " ecb ",
    "inflation", "interest rate", "term structure", "yield curve",
    "exchange rate", "currency", " fx ",
    "futures", "hedging", " hedge ", "derivative", "option pricing",
    "momentum", "carry trade", " carry ", "trend following",
    "commodity", "commodities",
    # ADDED AFTER THE ENGINEERING/HEALTH PASS. That cut caught "On Portfolio
    # Optimization: Forecasting Covariance" (583 cites) and "Quantifying the
    # uncertainty in VaR and expected shortfall" -- both filed by OpenAlex
    # under "Reservoir Engineering and Simulation Methods", both unmistakably
    # this desk's work. Also "Nowcasting Unemployment Using Neural Networks"
    # under disease surveillance and "Macroeconomic implications of population
    # ageing" under health care. The topic label is wrong often enough that the
    # rescue vocabulary has to carry the risk and portfolio words too, not just
    # the asset-class ones.
    "value at risk", "expected shortfall", "portfolio optimi",
    "portfolio selection", "asset allocation", "stress test",
    "nowcast", "tail risk", "extreme value", "covariance matrix",
    "macroeconomic", "risk premium", "sharpe ratio", "drawdown",
]


def log(m):
    print(m, flush=True)


def _protected(r):
    """Independent evidence that a paper belongs, whatever its topic says.

    HELD / MULTI-ROUTE / SEED-CITED ARE EVIDENCE ABOUT THE PAPER. A curated
    route is not, and treating it as such is what let a condensed-matter
    physics career into a quant-finance corpus. Route D harvests an author's
    ENTIRE bibliography, so "Memory and Chaos Effects in Spin Glasses" arrived
    under `authors` because Jean-Philippe Bouchaud is on the roster -- he is a
    statistical physicist who also does finance, and route D cannot tell his
    two careers apart. The roster is evidence about the AUTHOR; it says
    nothing about whether a given paper of theirs is about markets.

    So a curated route still protects a paper that carries a taxonomy tag --
    something in the finance vocabulary matched it -- but an UNTAGGED,
    single-route paper whose topic is explicitly on the off-desk list is
    judged on that topic. 619 of the 646 off-domain candidates came in this
    way, and 626 of them have no tag at all.
    """
    # THE CORPUS'S OWN VERDICT OUTRANKS THE TOPIC LABEL. fwd_citers counts
    # papers IN THIS POOL that cite this one, so >=5 means the collection
    # itself is built on it whatever OpenAlex filed it under. seed_indegree
    # only sees the original seed set and misses this entirely: Fama-Jensen
    # 1983 "Agency Problems and Residual Claims" has seed_indegree 0, no tag,
    # one route and a topic of "Islamic Finance and Banking Studies" -- it
    # would have been cut on all four counts, while 155 papers here cite it.
    # Also spares Barndorff-Nielsen's hyperbolic-distribution paper, filed
    # under "Aeolian processes and effects" with 120 in-pool citers, which is
    # foundational for the NIG models this desk actually uses.
    if int(float(r.get("fwd_citers") or 0)) >= 5:
        return True
    if (r.get("held") == "1"
            or int(float(r.get("n_routes") or 0)) >= 2
            or int(float(r.get("seed_indegree") or 0)) >= 1):
        return True
    if set((r.get("found_by") or "").split("+")) & CURATED:
        return bool((r.get("tag") or "").strip())
    return False


def _rescued(r):
    """True if the title carries desk vocabulary strong enough to override a
    DROP topic. Uses THE SAME NORMALISER as clean_core.py (textnorm.padded) --
    that file's own history is that a private normaliser here, subtly
    different from the shared one, is how "Detecting p-Hacking" and 45 other
    hyphenated titles went silently unmatched. `fx ` and `ecb ` only work as
    whole-word tests because textnorm strips punctuation to spaces first, so
    "FX:" and "Bitcoin," normalise the same as "fx " and "bitcoin "."""
    t = textnorm.padded(r.get("title"))
    return any(term in t for term in RESCUE)


def _load():
    if not TOPICS.exists():
        raise SystemExit(
            f"[cut] {TOPICS} missing. Run tools/core_topics.py (or dispatch "
            f"core-topics.yml) first -- with no topics this would remove "
            f"nothing and look like a clean result.")
    if not CAND.exists():
        raise SystemExit(f"[cut] {CAND} missing -- build the core list first")
    topics = json.loads(TOPICS.read_text(encoding="utf-8"))
    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))

    # fwd_citers LIVES IN THE METRICS FILE, NOT IN core_candidates.csv, and
    # this cost a real paper. The guard was first written as
    # r.get("fwd_citers") against these rows -- a column that does not exist
    # there -- so it returned None, became 0, and protected nothing while
    # looking exactly like a working guard. Fama-Jensen 1983 was cut with 155
    # in-pool citers. Loading it explicitly, and REFUSING when it is absent,
    # is the difference between a guard and the appearance of one.
    if not METRICS.exists():
        raise SystemExit(
            f"[cut] {METRICS} missing. REFUSING to cut: fwd_citers is the "
            f"signal that keeps papers this corpus actually builds on -- "
            f"without it the guard silently protects nothing. Run "
            f"tools/core_forward_graph.py first.")
    metrics = json.loads(METRICS.read_text(encoding="utf-8"))

    # COUNT ON-DESK CITERS, NOT ALL CITERS. A raw fwd_citers guard spared
    # "Memory and Chaos Effects in Spin Glasses" at 11 citers -- and those
    # citers are Bouchaud's OTHER condensed-matter papers, which route D
    # imported alongside it. An off-desk cluster citing itself produces exactly
    # the same number as genuine corpus relevance, so the raw count cannot tell
    # "the collection is built on this" from "a physics island lives here".
    # Excluding citers that are themselves in a dropped topic separates the
    # two: Fama-Jensen keeps 155 finance citers and survives; the spin-glass
    # papers drop to near zero and do not.
    drops_for_cite = set(_drops())
    fwd = {}
    if FORWARD.exists():
        fwd = json.loads(FORWARD.read_text(encoding="utf-8"))
    n_hit = 0
    for r in rows:
        uid = r["uid"]
        citers = fwd.get(uid) or []
        on_desk = sum(1 for c in citers
                      if (topics.get(c) or {}).get("t") not in drops_for_cite)
        r["fwd_citers"] = on_desk
        r["fwd_citers_all"] = (metrics.get(uid) or {}).get("fwd_citers", 0)
        if r["fwd_citers_all"]:
            n_hit += 1
    log(f"[cut] fwd_citers attached to {n_hit:,} of {len(rows):,} rows; "
        f"citers in a dropped topic are NOT counted (an off-desk cluster "
        f"citing itself is not evidence)")
    if n_hit == 0:
        raise SystemExit(
            f"[cut] {METRICS.name} joined ZERO rows -- the uid spaces do not "
            f"match. REFUSING rather than cutting with a dead guard.")
    return topics, rows


def _drops():
    if not DROPS.exists():
        raise SystemExit(
            f"[cut] {DROPS} missing. The off-desk decision lives in that file, "
            f"not in this script -- without it there is nothing to apply.")
    d = {}
    for r in csv.DictReader(io.open(DROPS, encoding="utf-8")):
        t = (r.get("topic") or "").strip()
        if t:
            d[t] = (r.get("reason") or "off-desk").strip()
    return d


def cmd_inventory(topics, rows):
    by_uid = {r["uid"]: r for r in rows}
    tot = collections.Counter()
    unp = collections.Counter()
    for u, v in topics.items():
        t = v.get("t")
        r = by_uid.get(u)
        if not t or r is None:
            continue
        tot[t] += 1
        if not _protected(r):
            unp[t] += 1
    drops = _drops()
    with io.open(INVENTORY, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["topic", "papers", "unprotected", "decision"])
        for t, n in tot.most_common():
            w.writerow([t, n, unp[t], "DROP" if t in drops else ""])
    log(f"[cut] {len(tot):,} distinct topics across {sum(tot.values()):,} "
        f"labelled papers -> {INVENTORY}")
    log(f"[cut] {len(drops)} currently marked DROP in {DROPS}")
    log(f"[cut] add a row to {DROPS} to extend the cut")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    topics, rows = _load()
    if args.inventory:
        return cmd_inventory(topics, rows)

    drops = _drops()
    log(f"[cut] {len(drops)} topics marked off-desk in {DROPS}")
    log(f"[cut] {len(RESCUE)} desk-vocabulary terms in RESCUE -- a title "
        f"match keeps the row even under a DROP topic")
    keep, cut = [], []
    spared = collections.Counter()
    rescued = collections.Counter()
    for r in rows:
        t = (topics.get(r["uid"]) or {}).get("t")
        if t in drops:
            if _protected(r):
                spared[t] += 1
                keep.append(r)
                continue
            if _rescued(r):
                rescued[t] += 1
                keep.append(r)
                continue
            r["stray_reason"] = f"topic:{t}"
            cut.append(r)
        else:
            keep.append(r)

    why = collections.Counter(r["stray_reason"] for r in cut)
    log(f"[cut] {len(rows):,} rows -> keep {len(keep):,}, "
        f"remove {len(cut):,} ({100*len(cut)/max(1,len(rows)):.2f}%)")
    for t, n in why.most_common():
        log(f"[cut]   {n:>6,}  {t[6:]}")
    if spared:
        log(f"[cut] {sum(spared.values()):,} SPARED on independent evidence "
            f"(held / 2+ routes / seed-cited / curated route):")
        for t, n in spared.most_common(6):
            log(f"[cut]   {n:>6,}  {t}")
    if rescued:
        log(f"[cut] {sum(rescued.values()):,} RESCUED on desk vocabulary in "
            f"the title:")
        for t, n in rescued.most_common(6):
            log(f"[cut]   {n:>6,}  {t}")
    if args.dry_run:
        log("[cut] --dry-run: nothing written")
        return 0

    cols = [c for c in rows[0].keys()]
    with io.open(CAND, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in cols} for r in keep)
    # APPEND, never replace. core_strays.csv is what makes every cut in this
    # pipeline reversible, and clean_core re-merges it on the next run so a
    # refined vocabulary can win a row back. Overwriting it here would drop
    # 62,648 keyword-judged strays on the floor.
    scols = cols + ["stray_reason"]
    old = []
    if STRAYS.exists():
        old = list(csv.DictReader(io.open(STRAYS, encoding="utf-8")))
        scols = list(dict.fromkeys(list(old[0].keys()) + scols)) if old else scols
    with io.open(STRAYS, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=scols)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in scols} for r in old + cut)
    log(f"[cut] {CAND} rewritten; {len(cut):,} rows appended to {STRAYS} "
        f"({len(old):,} were already there) -- reviewable, reversible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
