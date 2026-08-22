#!/usr/bin/env python3
"""Attach NBER's own topic taxonomy to the NBER papers in the archive.

The desk-sleeve classifier is broken -- `carry` holds 8 papers of 3,361
labelled and `other` is 70% of all tags -- and it cannot be fixed by reasoning
about the prompt, because there is no ground truth to measure against. Hand
labelling would produce some; NBER already did it.

The working-paper listing API does not return a paper's topics, but it accepts
them as a FILTER: `facet=topics:<Name>` returns exactly the papers NBER's own
editors assigned to that topic. Sweeping the taxonomy and inverting the result
gives a paper -> topics map for free, with no LLM calls and no guessing.

Joined to the archive on the deterministic DOI (10.3386/wNNNNN) that
tools/ingest_nber.py already assigns, this becomes:
  - a seed set for label propagation (tools/propagate.py)
  - the evaluation set for tools/sleeve_eval.py
  - few-shot examples for the scoring prompt

CAVEAT worth stating plainly: NBER has no "carry" topic. Carry is a
practitioner sleeve, not an academic one. This calibrates the macro sleeves and
tells you whether the classifier is broadly sane; carry still needs its own
probe (tools/carry_probe.py).

  python tools/nber_topics.py --dry-run
  python tools/nber_topics.py --since 1990
"""

import argparse
import collections
import json
import pathlib
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config   # noqa: E402
import store    # noqa: E402

UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}

# NBER's taxonomy, from https://www.nber.org/research/topics. Only the branches
# a systematic-macro desk would ever read -- sweeping all ~80 would spend most
# of the requests on labour, health and education.
TOPICS = [
    # Financial Economics
    "Financial Markets", "Financial Institutions", "Corporate Finance",
    "Behavioral Finance", "Portfolio Selection and Asset Pricing",
    # Macroeconomics
    "Macroeconomic Models", "Consumption and Investment", "Business Cycles",
    "Money and Interest Rates", "Monetary Policy", "Fiscal Policy",
    # International
    "International Finance", "International Macroeconomics", "Trade",
    # Econometrics
    "Estimation Methods", "Data Collection",
    # History (where the classics live)
    "Macroeconomic History", "Financial History",
]


def log(m):
    print(m, flush=True)


def sweep(topic, start, end, log=log):
    """Every working paper NBER filed under one topic in the window."""
    out, page = [], 1
    while True:
        r = requests.get(config.NBER_API, headers=UA, timeout=60, params={
            "page": page, "perPage": config.NBER_PER_PAGE, "sortBy": "public_date",
            "startDate": start, "endDate": end, "facet": f"topics:{topic}"})
        r.raise_for_status()
        j = r.json() or {}
        results = j.get("results") or []
        out += [(e.get("url") or "").rsplit("/", 1)[-1] for e in results]
        if len(results) < config.NBER_PER_PAGE:
            break
        page += 1
        # NBER_MAX_PAGES caps a program query at ~600; a full-history topic
        # sweep needs to page to exhaustion, so this deliberately does not use it
        if page > 60:
            log(f"    [{topic}] stopped at 60 pages")
            break
        time.sleep(0.4)
    return [w for w in out if w.startswith("w")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="1990-01-01")
    ap.add_argument("--until", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    import datetime as dt
    end = args.until or dt.date.today().isoformat()

    con = store.connect()
    # every NBER paper we hold, by working-paper number
    have = {}
    for uid, meta in con.execute("SELECT uid, meta FROM items"):
        try:
            d = json.loads(meta)
        except Exception:                             # noqa: BLE001
            continue
        wp = d.get("wp") or d.get("nber_wp")
        if not wp and str(d.get("doi", "")).startswith("10.3386/"):
            wp = d["doi"].split("/", 1)[1]
        if wp:
            have[str(wp)] = uid
    log(f"[topics] {len(have):,} NBER papers in the archive")

    by_wp = collections.defaultdict(list)
    for t in TOPICS:
        try:
            wps = sweep(t, args.since, end)
        except Exception as e:                        # noqa: BLE001
            log(f"  {t:<40} FAILED {type(e).__name__}")
            continue
        hit = sum(1 for w in wps if w in have)
        for w in wps:
            if w in have:
                by_wp[w].append(t)
        log(f"  {t:<40} {len(wps):>5} papers, {hit:>5} in archive")
        time.sleep(0.5)

    log(f"\n[topics] {len(by_wp):,} archive papers received at least one topic")
    dist = collections.Counter(t for ts in by_wp.values() for t in ts)
    for t, n in dist.most_common():
        log(f"    {t:<40} {n:>5}")

    if args.dry_run:
        log("[topics] dry run -- nothing written")
        return

    n = 0
    for wp, topics in by_wp.items():
        if store.update_meta(con, have[wp], {"nber_topics": sorted(set(topics))}):
            n += 1
    con.commit()
    log(f"\n[topics] wrote nber_topics onto {n:,} papers")


if __name__ == "__main__":
    main()
