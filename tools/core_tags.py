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

# --------------------------------------------------------------------- families
# 14 families. The family is kept on every row because selection uses per-family
# quotas: a global top-N returns equity cross-section and asset pricing and
# little else, and families like research integrity would never survive it.
TAXONOMY: dict[str, list[str]] = {
    "A_style_premia": [
        "value investing", "momentum", "time-series momentum",
        "cross-sectional momentum", "mean reversion", "short-term reversal",
        "long-term reversal", "carry trade", "roll yield", "convenience yield",
        "quality factor", "profitability factor", "investment factor",
        "size effect", "low beta", "betting against beta",
        "idiosyncratic volatility", "seasonality returns",
        "post-earnings announcement drift", "accruals anomaly",
        "analyst revisions", "short interest", "style premia",
        "defensive equity",
    ],
    "B_asset_classes": [
        "equity returns", "interest rates", "credit spreads",
        "foreign exchange", "commodity futures", "gold", "crude oil",
        "natural gas markets", "agricultural commodities", "cryptocurrency",
        "decentralized finance", "stablecoins", "inflation-linked bonds",
        "municipal bonds", "mortgage-backed securities", "securitization",
        "convertible bonds", "real estate investment trusts",
        "private equity returns", "emerging market debt", "sovereign debt",
    ],
    "C_systematic_macro": [
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
        "volatility forecasting", "realized volatility", "implied volatility",
        "volatility surface", "volatility smile", "variance risk premium",
        "VIX", "VIX futures", "variance swaps", "dispersion trading",
        "gamma exposure", "option pricing", "exotic options", "delta hedging",
        "tail risk", "skewness returns", "jump risk", "stochastic volatility",
        "volatility targeting",
    ],
    "E_portfolio_construction": [
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
        "machine learning finance", "deep learning finance",
        "neural networks forecasting", "transformer models",
        "large language models finance", "reinforcement learning trading",
        "random forest prediction", "gradient boosting",
        "LASSO regression", "ridge regression", "elastic net",
        "regularization high-dimensional", "dimension reduction",
        "principal component analysis", "autoencoder",
        "graph neural networks", "network analysis finance",
        "natural language processing finance", "textual analysis",
        "sentiment analysis returns", "feature selection",
        "model interpretability", "cross-validation time series",
        "walk-forward analysis", "synthetic data generation",
        "generative models finance", "transfer learning",
    ],
    "H_econometrics": [
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
        "data snooping", "replication crisis finance", "factor zoo",
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
        "behavioral finance", "limits to arbitrage", "investor sentiment",
        "overreaction underreaction", "disposition effect", "asset bubbles",
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
        "valuation based market timing", "cyclically adjusted price earnings",
        "sixty forty portfolio", "stock bond correlation",
        "risk-on risk-off", "glide path", "liability-driven investment",
        "endowment model", "overlay strategy", "long-run asset returns",
        "time diversification", "sequence of returns risk",
        "rebalancing premium", "asset class momentum",
        "cross-asset momentum", "cross-asset carry",
        "value and momentum everywhere", "lead-lag effect",
        "volatility spillover",
    ],
    "N_risk_premia": [
        "risk premia", "alternative risk premia", "risk premium harvesting",
        "risk premia timing", "equity risk premium", "bond risk premium",
        "credit risk premium", "currency risk premium",
        "commodity risk premium", "illiquidity premium",
        "volatility risk premium", "smart beta", "alternative beta",
        "factor investing", "multifactor portfolio", "factor momentum",
        "factor crowding", "factor rotation", "alpha decay",
        "strategy decay", "hedge fund replication",
        "managed futures replication",
    ],
}


def log(m):
    print(m, flush=True)


def _headers():
    h = {"User-Agent": "quant-digest/1.0 (research aggregator)"}
    k = os.environ.get("S2_API_KEY")
    if k:
        h["x-api-key"] = k
    return h


def probe(term):
    """(total results, top-5 titles) for one term, or (None, []) on failure."""
    try:
        r = requests.get(f"{API}/paper/search/bulk", headers=_headers(), params={
            "query": f'"{term}"' if " " in term else term,
            "fields": "title,year,citationCount",
            "fieldsOfStudy": "Economics,Business",
            "sort": "citationCount:desc"}, timeout=40)
    except Exception:                                      # noqa: BLE001
        return None, []
    if r.status_code != 200:
        return None, []
    j = r.json() or {}
    data = (j.get("data") or [])[:5]
    return j.get("total"), [
        # citationCount can be null; the sort key and this format both have to
        # tolerate it. The same omission crashes s2_harvest.py:607.
        f"{d.get('citationCount') or 0}|{(d.get('title') or '')[:70]}"
        for d in data]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true",
                    help="probe every term on S2 and record what came back")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    rows = [{"family": fam, "term": t}
            for fam, terms in TAXONOMY.items() for t in terms]
    if args.limit:
        rows = rows[:args.limit]
    log(f"[tags] {len(rows)} terms across {len(TAXONOMY)} families")

    if args.validate:
        log(f"[tags] validating at {PAUSE}s/request "
            f"({'key set' if os.environ.get('S2_API_KEY') else 'NO KEY -- slow'})")
        prog = Progress(len(rows), "tags", every_s=30)
        for r in rows:
            total, top = probe(r["term"])
            r["total"] = "" if total is None else total
            r["top5"] = " ;; ".join(top)
            r["keep"] = "" if total is None else int(total >= MIN_RESULTS)
            prog.tick()
            time.sleep(PAUSE)
        prog.done()
        kept = sum(1 for r in rows if r.get("keep") == 1)
        failed = sum(1 for r in rows if r.get("total") == "")
        log(f"[tags] {kept}/{len(rows)} terms cleared >= {MIN_RESULTS} results; "
            f"{len(rows)-kept-failed} too thin; {failed} probe failures")
        thin = [r for r in rows if r.get("keep") == 0]
        for r in sorted(thin, key=lambda x: x["total"])[:15]:
            log(f"[tags]   thin  {r['total']:>5}  {r['family']:<24} {r['term']}")

    dest = pathlib.Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cols = ["family", "term"] + (["total", "keep", "top5"] if args.validate else [])
    with io.open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    log(f"[tags] wrote {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
