#!/usr/bin/env python3
"""The search vocabulary for building the core-paper list.

WHY THIS IS A SEPARATE FILE FROM config.TAGS. That vocabulary has 75 terms and
exists to LABEL papers already held -- a closed set with surface forms, tuned so
a regex pass can assign tags deterministically. This one exists to DISCOVER
papers we do not hold, which is a different job with a different failure mode:
whatever is not named here is invisible at any search depth.

The repo has already paid for that lesson once at small scale. SSRN_QUERIES held
seven topics, so "Is Sector Rotation Causal? A Geometric Test of the
Growth-to-Defensive Lead-Lag" passed every filter and was never seen -- no query
mentioned sector rotation, lead-lag or causality. The list went 7 -> 18.

At canon scale the same gap is wider. config.TAGS has no term for asset
allocation, none for risk premia, and none for backtest overfitting -- so the
literature that tells you which OTHER papers to believe (Harvey-Liu-Zhu, Lopez
de Prado, Hou-Xue-Zhang) is unreachable, and so is the entire space a systematic
macro desk actually operates in.

VALIDATE BEFORE SWEEPING. A term nobody writes costs a request and returns
nothing: measured on S2 this session, "cross-asset risk premia" returns ONE
result, while "strategic asset allocation" returns 757 with Campbell-Viceira on
top. The ideas behind the dud are real -- they live under carry, cross-asset
momentum and value-and-momentum-everywhere, which is why those are the terms and
it is not. `--validate` probes each term once and records what came back, so the
real sweep is not spent on phrases that do not exist.

    python tools/core_tags.py                  # write the taxonomy
    python tools/core_tags.py --validate       # probe every term on S2
    python tools/core_tags.py --validate --limit 20
"""

import argparse
import csv
import io
import json
import os
import pathlib
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from progress import Progress                              # noqa: E402

API = "https://api.semanticscholar.org/graph/v1"
OUT = pathlib.Path("export/core_tags.csv")
# S2's documented anonymous limit is 100 requests / 5 minutes; the key lifts it
# to 1 req/s. Pace for whichever we have rather than assuming the key is set.
PAUSE = 1.1 if os.environ.get("S2_API_KEY") else 3.2
MIN_RESULTS = 20           # below this a term is not worth five pages

# ----------------------------------------------------------------------- sleeve
# WHICH TERMS NAME A DESK SLEEVE OUTRIGHT. A family maps to a sleeve by default
# (see build_core.FAMILY_SLEEVE), but some individual terms are more specific
# than their family: "carry trade" sits in A_style_premia, whose default is
# equity_xs, while the paper belongs to the carry book.
#
# THIS LIVES HERE, NEXT TO THE TERMS, ON PURPOSE. It used to be a dict in
# build_core.py keyed by term STRING -- a foreign key into this file with
# nothing enforcing it. Three of its 25 keys ("trend following", "theory of
# storage", "backwardation") were never terms here at all, so route A never
# searched them and the labeller could never assign them: a sleeve mapping that
# was dead in both directions and silent about it. The commit that introduced
# that dict is the same one that deleted an earlier private term list and is
# titled "one vocabulary".
#
# Keyed off TAXONOMY below, so a sleeve can only ever be attached to a term
# that actually exists. _check_sleeve_keys() fails loudly if that stops holding.
TERM_SLEEVE: dict[str, str] = {
    "carry trade": "carry", "currency carry": "carry", "roll yield": "carry",
    "convenience yield": "carry",
    "time-series momentum": "trend_cta", "managed futures": "trend_cta",
    "crisis alpha": "trend_cta",
    "commodity futures": "commodities", "crude oil": "commodities",
    "gold": "commodities", "natural gas markets": "commodities",
    "agricultural commodities": "commodities",
    "foreign exchange": "fx", "covered interest parity": "fx",
    "cross-currency basis": "fx", "dollar exchange rate": "fx",
    "interest rates": "rates_credit", "credit spreads": "rates_credit",
    "yield curve": "rates_credit", "term premium": "rates_credit",
    "sovereign debt": "rates_credit",
    "inflation-linked bonds": "rates_credit",
    # Added after measuring: "trend following" is the most common phrase in
    # 2,963 practitioner articles and 896 papers on S2, and was in neither the
    # taxonomy nor any sleeve -- the sleeve it names held 293 papers.
    "trend following": "trend_cta", "moving average rule": "trend_cta",
    "commodity trading advisor": "trend_cta",
    "forward premium": "carry", "interest rate differential": "carry",
    "contango": "commodities", "theory of storage": "commodities",
    "commodity returns": "commodities",
    "treasury market": "rates_credit",
    # B_asset_classes defaults to "other", which is right for crypto, DeFi,
    # stablecoins and REITs -- no desk trades those as a book -- but wrong for
    # these six. 11,385 B papers sat in "other"; these are ~5,600 of them.
    "securitization": "rates_credit",
    "mortgage-backed securities": "rates_credit",
    "convertible bonds": "rates_credit",
    "municipal bonds": "rates_credit",
    "emerging market debt": "rates_credit",
    "equity premium": "equity_xs",
}

# --------------------------------------------------------------------- families
# 14 families. The family is kept on every row because selection uses per-family
# quotas: a global top-N returns equity cross-section and asset pricing and
# little else, and families like research integrity would never survive it.
TAXONOMY: dict[str, list[str]] = {
    "A_style_premia": [
        # The taxonomy had NO cross-section term at all -- only
        # cross-sectional momentum. Stored unhyphenated because S2 normalises
        # hyphens in phrase search, so one spelling is enough.
        "cross section of stock returns", "cross section of returns",
        "cross section of expected returns",
        "cross section of currency returns",
        # route H (2026-08-29): validated on S2 before adding.
        "BAB factor", "CTA returns", "lookback straddle",
        "low volatility anomaly", "low-risk anomaly", "momentum crashes",
        "quality minus junk", "trend-following returns", "value premium",
        "value investing", "momentum", "time-series momentum",
        "cross-sectional momentum", "mean reversion", "short-term reversal",
        "long-term reversal", "carry trade", "roll yield", "convenience yield",
        "forward premium", "interest rate differential", "momentum crash",
        "quality factor", "profitability factor", "investment factor",
        "size effect", "low beta", "betting against beta",
        "idiosyncratic volatility", "seasonality",
        "post-earnings announcement drift", "accruals anomaly",
        "analyst revisions", "short interest", "style premia",
        "defensive equity",
    ],
    "B_asset_classes": [
        # route H (2026-08-29): validated on S2 before adding.
        "basis trade", "bond carry", "cash-futures basis",
        "commodity carry", "futures roll", "safe asset",
        "seasonal futures", "swap spread",
        "equity premium", "interest rates", "credit spreads",
        "foreign exchange", "commodity futures", "gold", "crude oil",
        "natural gas markets", "agricultural commodities", "cryptocurrency",
        "contango", "theory of storage", "commodity returns", "treasury market",
        "decentralized finance", "stablecoins", "inflation-linked bonds",
        "municipal bonds", "mortgage-backed securities", "securitization",
        "convertible bonds", "real estate investment trusts",
        "private equity returns", "emerging market debt", "sovereign debt",
    ],
    "C_systematic_macro": [
        # validated 2026-08-29: macroeconomic factors 9,202 ("Currency crashes
        # in emerging markets"), macro factors 1,337 ("Macro Factors in Bond
        # Risk Premia"), macroeconomic risk 1,238, announcements 577.
        "macroeconomic factors", "macro factors", "macroeconomic risk",
        "macroeconomic announcements",
        # route H (2026-08-29): validated on S2 before adding.
        "CIP deviation", "MIDAS regression", "Markov regime switching",
        "affine term structure", "bond risk premia",
        "change-point detection", "data vintages", "dynamic factor model",
        "economic surprise index", "excess bond returns",
        "exchange rate predictability", "expectations hypothesis",
        "factor-augmented", "financial conditions index",
        "forward premium puzzle", "forward rate regression",
        "hidden Markov", "high-frequency identification",
        "mixed frequency", "monetary policy shock",
        "multiple structural changes", "no-arbitrage term structure",
        "purchasing power parity", "real exchange rate", "real-time data",
        "regime switching model", "structural break",
        "term structure of interest rates", "uncovered interest parity",
        "yield curve factors",
        "inflation dynamics", "inflation expectations", "real interest rates",
        "monetary policy", "federal reserve", "european central bank",
        "bank of japan", "quantitative easing", "quantitative tightening",
        "central bank balance sheet", "term premium", "yield curve",
        "yield curve inversion", "recession forecasting", "business cycle",
        "nowcasting", "economic surprises", "output growth", "labour market",
        "fiscal policy", "fiscal dominance", "economic policy uncertainty",
        "geopolitical risk", "capital flows", "global financial cycle",
        "dollar exchange rate", "safe assets", "flight to quality",
        "cross-currency basis", "covered interest parity",
        "emerging markets", "zero lower bound",
    ],
    "D_vol_derivatives": [
        # route H (2026-08-29): validated on S2 before adding.
        "FX carry", "delta-hedged gains", "long volatility",
        "model-free implied volatility", "options on futures",
        "volatility carry", "volatility skew",
        "volatility forecasting", "realized volatility", "implied volatility",
        "volatility surface", "volatility smile", "variance risk premium",
        "VIX", "VIX futures", "variance swaps", "dispersion trading",
        "gamma exposure", "option pricing", "exotic options", "delta hedging",
        "tail risk", "skewness returns", "jump risk", "stochastic volatility",
        "volatility targeting",
    ],
    "E_portfolio_construction": [
        # route H (2026-08-29): validated on S2 before adding.
        "cross-asset carry", "naive diversification",
        "shrinkage estimator", "volatility-managed",
        "portfolio optimization", "mean-variance optimization",
        "Black-Litterman", "risk parity", "hierarchical risk parity",
        "risk budgeting", "equal risk contribution",
        "minimum variance portfolio", "maximum diversification",
        "covariance matrix estimation", "shrinkage estimation",
        "factor risk model", "position sizing", "Kelly criterion",
        "growth optimal portfolio", "portfolio rebalancing",
        "multi-period portfolio choice", "transaction cost optimization",
        "portfolio turnover", "strategy capacity", "tax-aware investing",
        "performance attribution", "portable alpha", "factor timing",
        "diversification",
    ],
    "F_risk_management": [
        "value at risk", "expected shortfall", "drawdown control",
        "maximum drawdown", "stress testing", "scenario analysis",
        "tail risk hedging", "leverage constraints", "margin requirements",
        "liquidity risk", "counterparty risk", "model risk",
        "correlation risk", "regime switching", "financial contagion",
        "systemic risk", "market turmoil", "crisis alpha",
    ],
    "G_machine_learning": [
        # route H (2026-08-29): validated on S2 before adding.
        "empirical asset pricing", "high-dimensional prediction",
        "machine learning asset pricing",
        "machine learning", "deep learning",
        "neural networks", "transformer models",
        "large language models", "reinforcement learning",
        "random forest", "gradient boosting",
        "LASSO regression", "ridge regression", "elastic net",
        "regularization", "dimension reduction",
        "principal component analysis", "autoencoder",
        "graph neural networks", "financial networks",
        "natural language processing", "textual analysis",
        "sentiment analysis", "feature selection",
        "model interpretability", "cross-validation",
        "walk-forward analysis", "synthetic data generation",
        "generative adversarial network", "transfer learning",
    ],
    "H_econometrics": [
        # route H (2026-08-29): validated on S2 before adding.
        "Fama-MacBeth", "Newey-West standard errors", "false discoveries",
        "half-life of PPP deviations",
        "financial econometrics", "return forecasting", "GARCH",
        "stochastic volatility models", "state space models", "Kalman filter",
        "cointegration", "unit root tests", "vector autoregression",
        "panel data models", "Bayesian econometrics", "Bayesian VAR",
        "bootstrap inference", "generalized method of moments",
        "event study", "high-dimensional inference", "mixed frequency data",
        "quantile regression", "extreme value theory", "copula models",
        "causal inference", "instrumental variables",
        "difference-in-differences", "structural breaks",
    ],
    "I_microstructure": [
        # route H (2026-08-29): validated on S2 before adding.
        "market impact", "slippage",
        "square-root law",
        "market microstructure", "limit order book", "order flow",
        "order flow imbalance", "price impact", "Kyle lambda",
        "market making", "high-frequency trading", "optimal execution",
        "implementation shortfall", "transaction costs", "bid-ask spread",
        "market liquidity", "market depth", "dark pools",
        "market fragmentation", "trading latency", "adverse selection",
        "trade crowding", "investor positioning", "short selling",
    ],
    "J_research_integrity": [
        "backtest overfitting", "multiple testing", "p-hacking",
        "data snooping", "replication crisis", "factor zoo",
        "out-of-sample performance", "publication bias",
        "false discovery rate", "deflated Sharpe ratio",
    ],
    "K_institutions": [
        "bank lending", "shadow banking", "collateral", "repo market",
        "central clearing", "central counterparty", "pension funds",
        "insurance companies", "exchange traded funds", "index funds",
        "mutual fund performance", "hedge fund performance", "fund flows",
        "financial regulation", "Basel capital",
    ],
    "L_behavioural_esg": [
        # route H (2026-08-29): validated on S2 before adding.
        "January effect", "calendar effects", "commodity seasonality",
        "day-of-week effect", "harvest cycle", "return seasonality",
        # "overreaction underreaction" returned 24 results: nobody writes
        # those two words adjacently, and an exact-phrase search asks for
        # exactly that. Split into the phrase people do write, plus the
        # British spelling of the family's own name and the concept the
        # family was missing outright.
        "behavioral finance", "behavioural finance", "investor overreaction",
        "overconfidence", "limits to arbitrage", "investor sentiment",
        "disposition effect", "asset bubbles",
        "herding behavior", "investor attention", "retail investors",
        "ESG investing", "climate risk", "transition risk",
        "alternative data", "satellite data", "credit card transaction data",
    ],
    # Two families that were entirely absent from the 75, and that a systematic
    # macro / CTA desk lives inside.
    "M_asset_allocation": [
        "asset allocation", "strategic asset allocation",
        "tactical asset allocation", "global tactical asset allocation",
        "dynamic asset allocation", "multi-asset portfolio", "market timing",
        "return predictability", "cyclically adjusted price earnings",
        "balanced portfolio", "stock bond correlation",
        "risk-on risk-off", "glide path", "liability-driven investment",
        "endowment model", "overlay strategy", "long-run asset returns",
        "time diversification", "sequence of returns risk",
        "rebalancing premium", "asset class momentum",
        "cross-asset momentum", "currency carry",
        "value and momentum everywhere", "lead-lag effect",
        "volatility spillover",
    ],
    # ROUTE H'S ONE STRUCTURAL ADDITION. Positioning and flows -- who is
    # holding what, and what happens when they unwind -- had no home in the 14
    # families, so 10 desk-relevant terms had nowhere to live. For a systematic
    # macro/CTA book this is not a niche: COT positioning, hedging pressure and
    # commodity financialisation are read weekly. Every term below was probed
    # on S2 first; "CTA replication" (2) and "broker-dealer leverage" (16) are
    # thin but real, and a thin term is a cheap one -- one request, and it
    # returns the canonical paper for its concept.
    # "crowding" and "capacity constraints" were validated, looked strong on
    # volume (24,365 and 5,016) and were REJECTED on their top hits:
    # crowding returns "Incentives and Prosocial Behavior" -- the crowding-OUT
    # literature -- and capacity constraints returns "The Theory of Industrial
    # Organization" and "Vehicle Routing Problem". Volume alone cannot tell a
    # technical term from a common English one; the titles can.
    "O_positioning_flows": [
        "CFTC",
        "CTA replication",
        "broker-dealer leverage",
        "commitments of traders",
        "commodity financialization",
        
        "dealer balance sheet",
        "hedging pressure",
        "intermediary asset pricing",
        "speculative positions",
    ],
    "N_risk_premia": [
        "risk premia", "alternative risk premia", "risk premium",
        "time-varying risk premia", "equity risk premium", "bond risk premium",
        "credit risk premium", "currency risk premium",
        "commodity risk premium", "illiquidity premium",
        "trend following", "moving average rule", "commodity trading advisor",
        "volatility risk premium", "smart beta", "alternative beta",
        "factor investing", "multifactor portfolio", "factor momentum",
        "factor crowding", "factor rotation", "alpha decay",
        "strategy decay", "hedge fund replication",
        "managed futures",
    ],
}


def log(m):
    print(m, flush=True)


def _total(r):
    """The probed result count as an int, whatever type it arrived as."""
    v = r.get("total")
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _headers():
    h = {"User-Agent": "quant-digest/1.0 (research aggregator)"}
    k = os.environ.get("S2_API_KEY")
    if k:
        h["x-api-key"] = k
    return h


def probe(term, tries=4):
    """(total results, top-5 titles) for one term, or (None, []) on failure.

    RETRIES, because the first validation pass lost 25 terms in contiguous runs
    -- seven straight in derivatives, seven in machine learning -- and those
    included "market microstructure" and "option pricing", which are obviously
    not rare. A run of adjacent failures is rate limiting, not a verdict on the
    vocabulary, and recording it as `total=NULL` would have quietly dropped
    real topics from the sweep.
    """
    r = None
    for attempt in range(tries):
        try:
            r = requests.get(f"{API}/paper/search/bulk", headers=_headers(),
                             params={
                "query": f'"{term}"' if " " in term else term,
                "fields": "title,year,citationCount",
                "fieldsOfStudy": "Economics,Business",
                "sort": "citationCount:desc"}, timeout=40)
        except Exception:                                  # noqa: BLE001
            r = None
        if r is not None and r.status_code == 200:
            break
        if r is not None and r.status_code not in (429, 500, 502, 503, 504):
            return None, []
        time.sleep(PAUSE * (2 ** attempt))
    if r is None or r.status_code != 200:
        return None, []
    j = r.json() or {}
    data = (j.get("data") or [])[:5]
    return j.get("total"), [
        # citationCount can be null; the sort key and this format both have to
        # tolerate it. The same omission crashes s2_harvest.py:607.
        f"{d.get('citationCount') or 0}|{(d.get('title') or '')[:70]}"
        for d in data]


def _check_sleeve_keys():
    """Every TERM_SLEEVE key must be a real term. Fail loudly, not silently.

    This is the assertion whose absence caused the bug: a term-keyed mapping in
    another file drifted from the vocabulary and nothing noticed for the life
    of the taxonomy.
    """
    terms = {t for ts in TAXONOMY.values() for t in ts}
    orphans = sorted(set(TERM_SLEEVE) - terms)
    if orphans:
        raise SystemExit(
            "[tags] TERM_SLEEVE names terms that are not in TAXONOMY: "
            + ", ".join(orphans)
            + "\n       Add the term to a family, or drop the sleeve mapping. "
              "A sleeve on a term nobody searches for cannot ever apply.")
    return len(TERM_SLEEVE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true",
                    help="probe every term on S2 and record what came back")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-missing", action="store_true",
                    help="keep rows that already validated; re-probe the rest")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    n_sleeve = _check_sleeve_keys()
    rows = [{"family": fam, "term": t, "sleeve": TERM_SLEEVE.get(t, "")}
            for fam, terms in TAXONOMY.items() for t in terms]
    log(f"[tags] {n_sleeve} terms carry an explicit sleeve; the rest take "
        f"their family's default")
    if args.only_missing and pathlib.Path(args.out).exists():
        # Carry forward what already validated cleanly; re-probe only blanks
        # and terms whose text changed. A full re-run costs 26 minutes to
        # re-learn what the CSV already knows.
        prev = {r["term"]: r for r in csv.DictReader(
            io.open(args.out, encoding="utf-8"))}
        keep, todo = [], []
        for r in rows:
            old_row = prev.get(r["term"])
            if old_row and (old_row.get("total") or "") != "":
                r.update({k: old_row.get(k, "")
                          for k in ("total", "keep", "pages", "top5")})
                keep.append(r)
            else:
                todo.append(r)
        log(f"[tags] {len(keep)} already validated; re-probing {len(todo)}")
        rows = keep + todo
        rows_to_probe = todo
    else:
        rows_to_probe = rows
    if args.limit:
        rows = rows[:args.limit]
        rows_to_probe = [r for r in rows_to_probe if r in rows]
    log(f"[tags] {len(rows)} terms across {len(TAXONOMY)} families")

    if args.validate:
        log(f"[tags] validating at {PAUSE}s/request "
            f"({'key set' if os.environ.get('S2_API_KEY') else 'NO KEY -- slow'})")
        prog = Progress(len(rows_to_probe), "tags", every_s=30)
        for r in rows_to_probe:
            total, top = probe(r["term"])
            r["total"] = "" if total is None else total
            r["top5"] = " ;; ".join(top)
            # A thin term is not a useless one -- it is a CHEAP one. "deflated
            # Sharpe ratio" returns 10 results and one of them is the Lopez de
            # Prado paper the research-integrity family exists to find; "crisis
            # alpha" returns 17 and is a core CTA concept. One request collects
            # all of them. So the count decides how many PAGES to spend, not
            # whether to ask at all, and only a term with zero results is cut.
            r["keep"] = "" if total is None else int(total > 0)
            r["pages"] = "" if total is None else (
                1 if total < 100 else (3 if total < 1000 else 5))
            prog.tick()
            time.sleep(PAUSE)
        prog.done()
        # Carried-forward rows come back from the CSV as STRINGS, freshly
        # probed ones are ints, so `keep == 1` counted only the fresh ones and
        # reported 40/299 cleared when the file actually held 283. Normalise
        # before counting rather than trusting either type.
        kept = sum(1 for r in rows if _total(r) is not None and _total(r) >= MIN_RESULTS)
        thin = [r for r in rows if _total(r) is not None and 0 < _total(r) < MIN_RESULTS]
        dead = [r for r in rows if _total(r) == 0]
        failed = sum(1 for r in rows if _total(r) is None)
        log(f"[tags] {kept}/{len(rows)} deep (>= {MIN_RESULTS}); {len(thin)} thin "
            f"(1 page each); {len(dead)} dead; {failed} probe failures")
        for r in sorted(thin, key=lambda x: _total(x)):
            log(f"[tags]   thin  {_total(r):>5}  {r['family']:<24} {r['term']}")
        for r in dead:
            log(f"[tags]   DEAD  {'0':>5}  {r['family']:<24} {r['term']}")

    dest = pathlib.Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cols = ["family", "term", "sleeve"] + (
        ["total", "keep", "pages", "top5"] if args.validate else [])
    with io.open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    log(f"[tags] wrote {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
