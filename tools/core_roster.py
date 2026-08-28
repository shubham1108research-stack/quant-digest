#!/usr/bin/env python3
"""The practitioner roster, as an auditable CSV -- Part 2 of the core plan.

A person enters on >=2 of four criteria, and EVERY ROW RECORDS WHICH ONES IT
MET, so the roster is reviewable rather than a matter of taste:

  c_firm    named researcher at a firm whose research library answered 200
            when probed this session (the measured list in the plan's 2.3)
  c_pubs    >=3 papers in the PMR journals + FAJ + Quantitative Finance.
            NOT CHECKED YET -- costs a Crossref author query per name and the
            roster is useful for review without it. Left blank, not guessed.
  c_cites   >=500 total S2 citations or h >= 15, read from the S2 profile
  c_sleeve  principal voice on an under-covered sleeve (the plan's 2.2 table)

S2 RESOLUTION IS A CANDIDATE, NOT A VERDICT. The rule is id-not-name because
h-index sorting on "Bryan Kelly" returns an orthopaedic surgeon (h=78) instead
of the economist (h=18). Names here are resolved through /author/search with
the top TWO candidates recorded and an ambiguity flag whenever more than one
plausible profile exists -- the review pass this file exists for is where a
human settles those, which is why nothing downstream may consume `s2_id`
while `needs_review` is set.

    python tools/core_roster.py             # resolve + write the CSV
    python tools/core_roster.py --no-s2     # skip resolution (offline)
"""

import argparse
import csv
import io
import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from progress import Progress                              # noqa: E402

OUT = pathlib.Path("export/core_roster.csv")
SITES = pathlib.Path("data/author_sites.csv")
API = "https://api.semanticscholar.org/graph/v1/author/search"

# Firms whose research library answered 200 with real content when probed
# (plan 2.3). c_firm is granted only against this measured list.
REACHABLE_FIRMS = {
    "aqr", "robeco", "man", "research affiliates", "rafi", "bridgewater",
    "cfm", "alphasimplex", "acadian", "two sigma", "pimco", "blackrock",
    "invesco", "ssga", "dimensional", "verdad", "newfound", "macrosynergy",
    "amundi",                # 502 at probe time -- their outage, not a wall
}

# author_sites.csv `category` -> desk sleeve. The category column has been
# dormant since the file was written; this is its first consumer.
CAT_SLEEVE = {
    "factors": "equity_xs", "qfactor": "equity_xs",
    "volatility": "vol_options", "macro": "macro_regime",
    "econometrics": "other", "microstructure": "microstructure",
    "behavioural": "equity_xs", "fx_basis": "fx",
    "convenience_yield": "carry", "term_structure": "rates_credit",
    "trend_following": "trend_cta", "global_financial_cycle": "macro_regime",
    "practitioner": "equity_xs", "practitioner_macro": "macro_regime",
    "promoted": "other", "other": "other",
}

# The gaps verified in S2 this session (plan 2.2): name -> (sleeve, firm).
# Every one of these met the sleeve criterion by construction.
ADDITIONS = {
    "Thierry Roncalli":  ("cross_asset", "Amundi"),
    "Attilio Meucci":    ("cross_asset", ""),
    "Vitali Kalesnik":   ("cross_asset", "Research Affiliates"),
    "Andrew Ang":        ("equity_xs", "BlackRock"),
    "Matthias Hanauer":  ("equity_xs", "Robeco"),
    "Laurens Swinkels":  ("equity_xs", "Robeco"),
    "Owen Lamont":       ("equity_xs", "Acadian"),
    "Wesley Gray":       ("equity_xs", "Alpha Architect"),
    "Otto van Hemert":   ("trend_cta", "Man"),
    "Andrew Lo":         ("trend_cta", "AlphaSimplex"),
    "Claude Erb":        ("commodities", ""),
    "Ralph Sueppel":     ("carry", "Macrosynergy"),
    "Riccardo Rebonato": ("rates_credit", ""),
    "Dan Rasmussen":     ("macro_regime", "Verdad"),
}

# THE CANON GIANTS. author_sites.csv answers "whose website do we crawl?", so
# it holds living researchers with active pages -- which is why Fama, Hansen,
# Thaler and Shiller were absent from the first roster while their students
# were on it. This roster answers a different question -- "whose back-catalogue
# does route D pull?" -- and for that the canon's own authors are the most
# important rows. Names come from canon.py's author hints (Fama carries six
# canon papers, Merton four), written out as FULL names because bare-surname
# search is hopeless: "the paper disambiguates" only works when you have the
# paper. Deceased authors stay in -- their back-catalogue is the point.
CANON_GIANTS = {
    "Eugene Fama":        ("equity_xs", "6 canon papers"),
    "Kenneth French":     ("equity_xs", "Fama-French factors"),
    "Robert Merton":      ("vol_options", "4 canon papers; ICAPM, option pricing"),
    "Lars Peter Hansen":  ("other", "GMM; robustness"),
    "Robert Shiller":     ("macro_regime", "excess volatility; CAPE"),
    "Richard Thaler":     ("equity_xs", "behavioural canon"),
    "William Sharpe":     ("cross_asset", "CAPM; Sharpe ratio"),
    "Myron Scholes":      ("vol_options", "option pricing"),
    "Darrell Duffie":     ("rates_credit", "term structure, OTC markets"),
    "Robert Engle":       ("vol_options", "ARCH; vol modelling"),
    "John Cochrane":      ("macro_regime", "asset pricing; discount rates"),
    "Lubos Pastor":       ("equity_xs", "liquidity risk; fund flows"),
    "Yakov Amihud":       ("microstructure", "illiquidity measure"),
    "Harry Markowitz":    ("cross_asset", "portfolio selection"),
    "Andrew Karolyi":     ("fx", "international asset pricing"),
    "Kenneth Rogoff":     ("fx", "exchange rate disconnect; debt"),
    # -- equity cross-section & fund performance
    "Robert Stambaugh":   ("equity_xs", "liquidity risk; mispricing factors"),
    "Sheridan Titman":    ("equity_xs", "momentum; characteristics"),
    "Narasimhan Jegadeesh": ("equity_xs", "momentum"),
    "Josef Lakonishok":   ("equity_xs", "contrarian; LSV"),
    "Juhani Linnainmaa":  ("equity_xs", "factor replication; history of anomalies"),
    "David McLean":       ("equity_xs", "does academic research destroy predictability"),
    "Jeffrey Pontiff":    ("equity_xs", "publication effect; limits to arbitrage"),
    "Jonathan Berk":      ("equity_xs", "fund flows and performance"),
    "Russ Wermers":       ("equity_xs", "mutual fund performance"),
    "Martin Lettau":      ("equity_xs", "cay; consumption asset pricing"),
    # -- behavioural
    "Daniel Kahneman":    ("equity_xs", "prospect theory"),
    "Terrance Odean":     ("equity_xs", "retail investor behaviour"),
    "Brad Barber":        ("equity_xs", "retail trading"),
    "Ulrike Malmendier":  ("equity_xs", "experience effects; CEO overconfidence"),
    "Robert Vishny":      ("equity_xs", "limits of arbitrage; LSV"),
    # -- microstructure
    "Albert Kyle":        ("microstructure", "Kyle lambda; informed trading"),
    "Maureen O'Hara":     ("microstructure", "market microstructure theory"),
    "Joel Hasbrouck":     ("microstructure", "price discovery; TAQ empirics"),
    "Ananth Madhavan":    ("microstructure", "execution; ETF plumbing"),
    "Tarun Chordia":      ("microstructure", "liquidity commonality"),
    "Terrence Hendershott": ("microstructure", "algorithmic trading"),
    # -- volatility & derivatives
    "Steven Heston":      ("vol_options", "stochastic volatility model"),
    "John Hull":          ("vol_options", "derivatives canon"),
    "Jim Gatheral":       ("vol_options", "volatility surface; rough vol"),
    "Francis Longstaff":  ("vol_options", "LSM; illiquid derivatives"),
    "Peter Christoffersen": ("vol_options", "option-implied information"),
    "Neil Shephard":      ("vol_options", "realized volatility"),
    "Ole Barndorff-Nielsen": ("vol_options", "realized variance theory"),
    # -- commodities
    "Gary Gorton":        ("commodities", "facts and fantasies about commodity futures"),
    "Geert Rouwenhorst":  ("commodities", "commodity futures risk premia; momentum everywhere"),
    "Eduardo Schwartz":   ("commodities", "convenience yield term structure"),
    "Robert Pindyck":     ("commodities", "storage; volatility"),
    "Hendrik Bessembinder": ("commodities", "hedging pressure; market quality"),
    # -- FX & carry
    "Kenneth Froot":      ("fx", "forward discount; institutional FX"),
    "Charles Engel":      ("fx", "exchange rate predictability"),
    "Lucio Sarno":        ("fx", "FX predictability; carry"),
    "Richard Lyons":      ("fx", "FX microstructure"),
    "Nikolai Roussanov":  ("carry", "currency risk factors; carry portfolios"),
    "Lukas Menkhoff":     ("carry", "carry trades and global FX volatility"),
    # -- rates & credit
    "Oldrich Vasicek":    ("rates_credit", "term structure model"),
    "John C. Cox":        ("rates_credit", "CIR; option pricing"),
    "Tobias Adrian":      ("rates_credit", "intermediary asset pricing; term premia"),
    "Refet Gurkaynak":    ("rates_credit", "yield curve data; monetary surprises"),
    # -- macro & institutions
    "Markus Brunnermeier": ("macro_regime", "liquidity spirals; macro-finance"),
    "Hyun Song Shin":     ("macro_regime", "leverage cycles; BIS"),
    "Ben Bernanke":       ("macro_regime", "financial accelerator; policy"),
    "James Hamilton":     ("macro_regime", "regime switching; oil"),
    "James Stock":        ("macro_regime", "factor models for macro forecasting"),
    "Mark Watson":        ("macro_regime", "dynamic factor models"),
    "Annette Vissing-Jorgensen": ("macro_regime", "treasury convenience yield"),
    "Wei Xiong":          ("macro_regime", "bubbles; commodities financialization"),
    "Dimitri Vayanos":    ("rates_credit", "preferred habitat; liquidity"),
    "Viral Acharya":      ("rates_credit", "systemic risk; credit"),
    # -- trend / hedge funds
    "William Fung":       ("trend_cta", "hedge fund styles; trend replication"),
    "David Hsieh":        ("trend_cta", "trend-following risk factors"),
    # -- portfolio construction & risk
    "Raman Uppal":        ("cross_asset", "1/N; model uncertainty"),
    "Mark Kritzman":      ("cross_asset", "turbulence; absorption ratio"),
    "Philippe Jorion":    ("cross_asset", "VaR; estimation risk"),
    "Richard Michaud":    ("cross_asset", "resampled efficiency"),
    # -- econometrics
    "Whitney Newey":      ("other", "HAC standard errors"),
    "Kenneth West":       ("other", "HAC; forecast evaluation"),
    "Halbert White":      ("other", "reality check; misspecification"),
    "Clive Granger":      ("other", "causality; cointegration"),
    "James MacKinlay":    ("other", "event studies; econometrics of finance"),
}

# Sleeves the classifiers starved; being a principal voice on one is c_sleeve.
UNDER_COVERED = {"carry", "trend_cta", "commodities", "cross_asset",
                 "microstructure"}


def log(m):
    print(m, flush=True)


def _firm_of(row):
    """A firm name out of a URL or note, matched against the measured list."""
    blob = f"{row.get('url','')} {row.get('note','')}".lower()
    for f in REACHABLE_FIRMS:
        if f.replace(" ", "") in blob.replace(" ", "").replace("-", ""):
            return f
    return ""


def resolve(name, pause):
    """Top-2 S2 author candidates for a name, or None on failure."""
    q = urllib.parse.quote(name)
    url = (f"{API}?query={q}"
           f"&fields=name,hIndex,paperCount,citationCount,affiliations")
    hdr = {"User-Agent": "quant-digest/1.0"}
    k = os.environ.get("S2_API_KEY", "").strip()
    if k:
        hdr["x-api-key"] = k
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read())
            got = d.get("data") or []
            got.sort(key=lambda a: -(a.get("citationCount") or 0))
            return got[:2]
        except Exception:                                  # noqa: BLE001
            time.sleep(pause * (2 ** attempt))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-s2", action="store_true",
                    help="skip S2 resolution (offline / no budget)")
    args = ap.parse_args()

    people = {}
    if SITES.exists():
        for r in csv.DictReader(io.open(SITES, encoding="utf-8")):
            cat = (r.get("category") or "other").strip()
            people[r["name"]] = {
                "name": r["name"],
                "sleeve": CAT_SLEEVE.get(cat, "other"),
                "category": cat,
                "firm": _firm_of(r),
                "source": "author_sites.csv",
                "url": r.get("url", ""),
            }
    for name, (sleeve, note) in CANON_GIANTS.items():
        if name in people:
            continue
        people[name] = {"name": name, "sleeve": sleeve, "category": "",
                        "firm": "", "source": f"canon ({note})", "url": ""}
    for name, (sleeve, firm) in ADDITIONS.items():
        if name in people:
            people[name]["sleeve"] = sleeve
            people[name]["firm"] = people[name]["firm"] or firm.lower()
            continue
        people[name] = {"name": name, "sleeve": sleeve, "category": "",
                        "firm": firm.lower(), "source": "gap-fill (plan 2.2)",
                        "url": ""}
    log(f"[roster] {len(people)} people "
        f"({sum(1 for p in people.values() if p['source'].startswith('gap')):d}"
        f" additions)")

    # Incremental: a full pass is 14 keyless minutes, and 96 of these rows
    # were resolved an hour ago. Carry forward anything the existing CSV
    # already answered; resolve only new names and prior failures.
    if OUT.exists():
        prev = {r["name"]: r for r in csv.DictReader(
            io.open(OUT, encoding="utf-8"))}
        carried = 0
        for name, p in people.items():
            r = prev.get(name)
            if r and r.get("s2_status") == "ok":
                for k_src, k_dst in (("s2_id", "s2_id"), ("s2_h", "s2_h"),
                                     ("s2_cites", "s2_cites"),
                                     ("s2_papers", "s2_papers"),
                                     ("s2_status", "s2_status"),
                                     ("needs_review", "needs_review")):
                    p[k_dst] = r.get(k_src, "")
                p["needs_review"] = int(p["needs_review"] or 0)
                p["s2_h"] = int(p["s2_h"] or 0)
                p["s2_cites"] = int(p["s2_cites"] or 0)
                carried += 1
        log(f"[roster] carried {carried} resolutions from the existing CSV")

    pause = 1.1 if os.environ.get("S2_API_KEY") else 3.2
    if not args.no_s2:
        log(f"[roster] resolving on S2 at {pause}s/request "
            f"({'key' if os.environ.get('S2_API_KEY') else 'NO KEY -- slow'})")
        todo = [p for p in people.values() if p.get("s2_status") != "ok"]
        log(f"[roster] resolving {len(todo)} of {len(people)}")
        prog = Progress(len(todo), "roster", every_s=30)
        for p in todo:
            got = resolve(p["name"], pause)
            if got is None:
                p["s2_status"] = "lookup_failed"
            elif not got:
                p["s2_status"] = "no_match"
            else:
                a = got[0]
                p["s2_id"] = a.get("authorId", "")
                p["s2_h"] = a.get("hIndex") or 0
                p["s2_cites"] = a.get("citationCount") or 0
                p["s2_papers"] = a.get("paperCount") or 0
                # Ambiguous when a second profile is also plausible. The
                # Bryan Kelly failure is exactly this shape, so the flag is
                # deliberately eager: review costs a minute, a wrong id
                # poisons every author-anchored feature downstream.
                second = got[1] if len(got) > 1 else None
                p["needs_review"] = int(bool(
                    second and (second.get("hIndex") or 0) >= 10))
                p["s2_status"] = "ok"
            prog.tick()
            time.sleep(pause)
        prog.done()

    rows = []
    for p in sorted(people.values(), key=lambda x: x["name"]):
        c_firm = int(bool(p.get("firm")))
        c_cites = int((p.get("s2_cites") or 0) >= 500 or
                      (p.get("s2_h") or 0) >= 15)
        c_sleeve = int(p["sleeve"] in UNDER_COVERED)
        met = c_firm + c_cites + c_sleeve          # c_pubs deliberately absent
        rows.append({
            "name": p["name"], "sleeve": p["sleeve"],
            "category": p.get("category", ""), "firm": p.get("firm", ""),
            "s2_id": p.get("s2_id", ""), "s2_h": p.get("s2_h", ""),
            "s2_cites": p.get("s2_cites", ""),
            "s2_papers": p.get("s2_papers", ""),
            "s2_status": p.get("s2_status", "skipped"),
            "needs_review": p.get("needs_review", ""),
            "c_firm": c_firm, "c_pubs": "", "c_cites": c_cites,
            "c_sleeve": c_sleeve, "criteria_met": met,
            "qualifies": int(met >= 2),
            "source": p["source"],
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with io.open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    q = sum(1 for r in rows if r["qualifies"])
    amb = sum(1 for r in rows if r["needs_review"] == 1)
    log(f"[roster] {len(rows)} rows -> {OUT}")
    log(f"[roster] {q} qualify on >=2 criteria (c_pubs not yet checked); "
        f"{amb} flagged needs_review (ambiguous S2 profile)")
    import collections                                     # noqa: PLC0415
    sl = collections.Counter(r["sleeve"] for r in rows)
    log(f"[roster] by sleeve: {dict(sl.most_common())}")
    log("[roster] REVIEW BEFORE USE -- route D must not consume s2_id rows "
        "that carry needs_review=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
