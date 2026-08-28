#!/usr/bin/env python3
"""Remove off-topic strays from the compiled core-candidate list.

WHERE THE STRAYS COME FROM. The sweep filters to fieldsOfStudy=
Economics,Business -- and "Business" admits the management literature, so
generic mega-cited papers ride in on generic method terms: "Firm Resources and
Sustained Competitive Advantage" (65k cites, strategy) arrived via an
econometrics term, "The Measurement of Organizational Commitment" via a
feature-selection-adjacent one. The most-cited rows per family are the most
contaminated, because the biggest papers match the broadest terms.

WHAT IS NEVER TOUCHED. Any row with independent evidence: held in the archive,
found by two or more routes, cited by a seed paper, or contributed by a curated
route (canon, NBER, snowball, PWB, QuantSeeker, authors, SignalDoc). Those are
finance by construction. Only SWEEP-ONLY rows are judged, and they are judged
on their title:

    keep   the title carries finance, macro, or quant-methods vocabulary --
           a wordlist that deliberately includes the method families (machine
           learning, GARCH, false discovery) because families G, H and J exist
           on purpose
    stray  an off-domain marker (organizational, marketing, tourism, clinical)
           with no finance signal, or no signal either way

A KEYWORD GATE FAILED HERE BEFORE -- kept "cardiovascular risk prediction" on
the word "risk". Two differences this time: the pool is already
citation-ranked finance-adjacent rather than the raw SSRN firehose, and the
gate REMOVES from a mostly-clean pool instead of admitting from a dirty one,
so an error costs one candidate rather than polluting the archive. Errors are
also inspectable: every removed row lands in export/core_strays.csv with the
reason, so the cut is reviewable and reversible.

    python tools/clean_core.py            # cleans export/core_candidates.csv
"""

import csv
import io
import json
import pathlib
import re
import sys

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
STRAYS = OUT / "core_strays.csv"

# The hand judgements are OUR work product, not harvested content, so unlike
# everything else under export/ they are COMMITTED -- data/ is tracked and
# export/ is gitignored. That distinction is load-bearing: CI checks out the
# repo and regenerates export/ from scratch, so a judgements file living only
# in export/ would simply not exist there, and 1,394 hand verdicts would be
# silently re-decided by the keyword gate on every cloud rebuild.
# The file holds identifiers (DOIs, arXiv ids) and no third-party text, which
# is why committing it does not reopen the public-repo problem export/ exists
# to solve.
JUDGED = pathlib.Path("data") / "core_judgments.json"
if not JUDGED.exists():
    JUDGED = OUT / "core_judgments.json"

CURATED = {"canon", "nber", "snowball", "pwb", "quantseeker", "authors",
           "signaldoc"}

# Finance / macro / markets vocabulary. Substring match on the normalised
# title. "alpha" and bare "factor" are deliberately absent -- Cronbach's alpha
# and factor analysis are psychology.
FIN = [
    "asset", "portfolio", "stock", "equit", "bond ", "bonds", "market",
    "trading", "trader", "investor", "investment", "price", "pricing",
    "return", " risk", "risk ", "volatil", "liquidity", "credit", "currenc",
    "exchange rate", "inflation", "monetary", "macroeconomic", "macro-",
    "interest rate", "yield", "term structure", "treasur", "mutual fund",
    "hedge fund", "pension", "bank", "financ", "dividend", "earnings",
    "valuation", "arbitrage", "hedg", "futures", "option", "swap",
    "derivativ", "beta", "sharpe", "momentum", "carry", "anomal", "premium",
    "premia", "crash", "bubble", "contagion", "systemic", "default",
    "sovereign", "commodity", "commodities", "crude oil", "oil price",
    "natural gas", "gold", "crypto", "bitcoin", "forecast", "econometric",
    "business cycle", "recession", "unemployment", "employment",
    "central bank", "capital flow", "capital structure", "cash flow",
    "takeover", "merger", "ipo", "microstructure", "order flow", "bid-ask",
    "limit order", "market maker", "esg", "climate", "carbon", "sentiment",
    "herd", "disposition effect", "prospect theory", "overreaction",
    "underreaction", "lottery", "speculat", "leverage", "margin",
    "collateral", "repo ", "securitiz", "etf", "index fund", "diversif",
    "drawdown", "value at risk", "expected shortfall", "stress test",
    "backtest", "out-of-sample", "trend following", "managed futures",
    "risk parity", "rebalanc", "allocation",
    # quant methods -- families G, H and J exist on purpose
    "machine learning", "statistical learning", "deep learning", "neural",
    "random forest", "gradient boosting", "lasso", "regulariz",
    "principal component", "autoencoder", "reinforcement learning",
    "cross-validation", "bootstrap", "cointegrat", "unit root",
    "autoregress", "garch", "kalman", "state space", "quantile regression",
    "copula", "extreme value", "false discovery", "multiple testing",
    "publication bias", "p-hacking", "data snooping", "spillover",
    "regime switching", "structural break", "instrumental variable",
    "panel data", "time series", "high-dimensional", "nowcast",
]

# Off-domain markers. A row is a stray only when one of these hits AND no
# finance term does -- "Organizational structure of banks" stays on "bank".
OFF = [
    "organizational", "organisational", "organizational commitment",
    "employee", "human resource", "job satisfaction", "leadership",
    "entrepreneur", "marketing", "brand equity", "consumer behavi",
    "supply chain", "tourism", "hospitality", "nursing", "clinical",
    "patient", "medical", "health care", "healthcare", "psychiatr",
    "education", "classroom", "teacher", "curriculum",
    "knowledge management", "competitive advantage", "firm resources",
    "strategic management", "sociolog", "information system",
    "software engineering", "questionnaire",
]


def norm(t):
    return " " + re.sub(r"[^a-z0-9-]+", " ", (t or "").lower()).strip() + " "


def _fin_terms():
    """FIN plus every taxonomy term -- one vocabulary, again.

    The first cut removed "Generalized Optimal Matching Methods for Causal
    Inference" while `causal inference` sat in the taxonomy the sweep had just
    searched: the hand-written keep-list was a partial copy of a vocabulary
    that already existed. Reading core_tags.csv makes the cleaner and the
    sweep agree by construction. A few finance stems the taxonomy phrases
    don't surface as substrings are added explicitly (debt, lending, fintech).
    """
    terms = list(FIN) + ["debt", "lend", "loan", "mortgage", "insur",
                         "fintech", "heteroskedastic", "autocorrelation",
                         "risk-free", "discount rate",
                         "taylor rule", "fiscal", "budget deficit",
                         "variable selection", "secular stagnation",
                         "exchange-rate", "risk premium"]
    tags = OUT / "core_tags.csv"
    if tags.exists():
        for r in csv.DictReader(io.open(tags, encoding="utf-8")):
            t = (r.get("term") or "").strip().lower()
            if len(t) >= 4:
                terms.append(t)
    # longest first so the reported match is the most specific one
    return sorted(set(terms), key=len, reverse=True)


def main():
    if not CAND.exists():
        print(f"[clean] {CAND} missing")
        return 1
    rows = list(csv.DictReader(io.open(CAND, encoding="utf-8")))
    # Idempotent: a prior clean leaves the removed rows in STRAYS, so re-merge
    # them before judging -- otherwise a refined vocabulary can never win back
    # a row the previous pass cut.
    if STRAYS.exists():
        back = list(csv.DictReader(io.open(STRAYS, encoding="utf-8")))
        held_uids = {r["uid"] for r in rows}
        rows += [r for r in back if r["uid"] not in held_uids]
        print(f"[clean] re-merged {len(back):,} previously removed rows for "
              f"re-judgement")
    fin_terms = _fin_terms()
    print(f"[clean] keep-vocabulary: {len(fin_terms)} terms "
          f"(FIN + taxonomy + finance stems)")

    # HAND JUDGEMENTS OUTRANK THE VOCABULARY. Every stray above the selection
    # cut was read and judged by hand; those verdicts are the most expensive
    # evidence in this pipeline and a keyword rule must never silently
    # overturn them. Keyed by uid, because _judge_strays.json is regenerated
    # by every rebuild and its row positions do not survive one.
    rescued = set()
    if JUDGED.exists():
        try:
            j = json.loads(JUDGED.read_text(encoding="utf-8"))
            rescued = set(j.get("rescue_uids") or [])
            print(f"[clean] {len(rescued):,} hand-rescued uids loaded from "
                  f"{JUDGED.name} -- these are kept regardless of vocabulary")
        except Exception as e:                              # noqa: BLE001
            print(f"[clean] {JUDGED.name} unreadable ({type(e).__name__}); "
                  f"proceeding WITHOUT the hand judgements")

    kept, strays = [], []
    for r in rows:
        routes = set((r.get("found_by") or "").split("+"))
        protected = (r.get("held") == "1"
                     or int(r.get("n_routes") or 0) >= 2
                     or int(r.get("seed_indegree") or 0) >= 1
                     or routes & CURATED)
        if protected:
            r["clean"] = "protected"
            kept.append(r)
            continue
        if r.get("uid") in rescued:
            r["clean"] = "rescued"
            kept.append(r)
            continue
        t = norm(r.get("title"))
        fin = next((w for w in fin_terms if w in t), None)
        if fin:
            r["clean"] = f"fin:{fin.strip()}"
            kept.append(r)
            continue
        off = next((w for w in OFF if w in t), None)
        r["stray_reason"] = f"offdomain:{off.strip()}" if off else "no_finance_signal"
        strays.append(r)

    cols = list(rows[0].keys()) + ["clean"]
    with io.open(CAND, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in cols} for r in kept)
    scols = list(rows[0].keys()) + ["stray_reason"]
    with io.open(STRAYS, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=scols)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in scols} for r in strays)

    import collections
    why = collections.Counter(r["stray_reason"].split(":")[0] for r in strays)
    print(f"[clean] {len(rows):,} rows -> kept {len(kept):,}, "
          f"removed {len(strays):,} ({100*len(strays)/len(rows):.1f}%)")
    print(f"[clean] removal reasons: {dict(why)}")
    fam = collections.Counter(r.get("family") or "?" for r in strays)
    print(f"[clean] strays by family: {dict(fam.most_common(8))}")
    print(f"[clean] removed rows preserved in {STRAYS} -- reviewable, reversible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
