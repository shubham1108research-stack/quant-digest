#!/usr/bin/env python3
"""Merge every candidate route into ONE reviewable core-paper list.

THE ARCHIVE IS A RECENCY SNAPSHOT. 68% of it is dated this year, 834 papers
predate 2015, and the `classic` flag covers 309 rows of which 114 have an
abstract. A graph built on that inherits the feed's bias: it has the leaves and
not the trunk. This assembles the trunk.

SEVEN ROUTES, AND OVERLAP IS THE POINT. A paper found by one route is a
candidate; a paper found by four is core, and `n_routes` says which is which
without anyone having to assert it:

  C  snowball     in-degree over paper_refs from the classics + NBER seeds.
                  The field's own judgement rather than mine -- a paper many
                  core papers cite IS core. 339,411 reference rows.
  E  canon        canon.py + classics.json, already flagged `classic` in items
  B  nber         NBER working papers, editorially curated, all PDF-reachable
  F  pwb          Papers With Backtest: 3,745 papers with a MEASURED Sharpe and
                  a publication date, so a backtest running past that date is a
                  real out-of-sample test rather than an LLM's guess at one
  G  signaldoc    Chen-Zimmermann: 331 predictors with replication grades,
                  including 14 whose published result does NOT hold
  D  authors      watched-author back catalogues
  D  quantseeker  a practitioner's hand-picked weekly recaps

SCORING KEEPS ITS EVIDENCE. Every input is a column, not a term folded into one
opaque number, because the reason a paper is on the list is the thing a reviewer
needs. cites_per_year is age-normalised: raw citation counts alone return a list
of the 1970s and nothing else.

QUOTAS PER SLEEVE, NOT A GLOBAL TOP-N. A global ranking returns equity
cross-section and asset pricing and little else -- `carry` holds 111 papers of
11,764 labelled, and microstructure is structurally quieter than factor
research. Quotas are what make the result usable for a macro/CTA desk.

PDF AVAILABILITY IS A COLUMN, NEVER A FILTER. Edges come from reference lists,
which need metadata only. Dropping Fama-French 1993 because Elsevier paywalls it
would remove one of the graph's largest hubs to save a parse we were never going
to do.

NOTHING IS INGESTED. Writes export/ only.

    python tools/build_core.py --target 2000
"""

import argparse
import collections
import csv
import io
import json
import math
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import store                                               # noqa: E402

OUT = pathlib.Path("export")
SLEEVES = ["trend_cta", "carry", "fx", "rates_credit", "commodities",
           "macro_regime", "cross_asset", "vol_options", "equity_xs",
           "microstructure", "other"]

# Papers With Backtest tags each paper with the asset class it trades. That is
# not the same axis as a desk sleeve, but for the six that map cleanly it is a
# real label from an external source rather than a guess.
# ONE VOCABULARY. Discovery searches the 299-term taxonomy in
# export/core_tags.csv; labelling reads THE SAME FILE. The first version
# hand-wrote a private _PHRASE dict here instead, which is exactly the
# "two different notions of the subject" drift the taxonomy exists to prevent
# -- and the way the audit found it is that this file had zero references to
# core_tags.csv while claiming to be taxonomy-driven.
TAGS_CSV = OUT / "core_tags.csv"

# Sleeve for a candidate that only the taxonomy describes. Tag-level overrides
# first (a term like "carry trade" names its sleeve outright), then a family
# default. This maps the taxonomy ONTO sleeves; it does not replace it.
TAG_SLEEVE = {
    "carry trade": "carry", "currency carry": "carry", "roll yield": "carry",
    "convenience yield": "carry", "backwardation": "carry",
    "time-series momentum": "trend_cta", "managed futures": "trend_cta",
    "trend following": "trend_cta", "crisis alpha": "trend_cta",
    "commodity futures": "commodities", "crude oil": "commodities",
    "gold": "commodities", "natural gas markets": "commodities",
    "agricultural commodities": "commodities",
    "theory of storage": "commodities",
    "foreign exchange": "fx", "covered interest parity": "fx",
    "cross-currency basis": "fx", "dollar exchange rate": "fx",
    "interest rates": "rates_credit", "credit spreads": "rates_credit",
    "yield curve": "rates_credit", "term premium": "rates_credit",
    "sovereign debt": "rates_credit",
    "inflation-linked bonds": "rates_credit",
}
FAMILY_SLEEVE = {
    "A_style_premia": "equity_xs", "B_asset_classes": "other",
    "C_systematic_macro": "macro_regime", "D_vol_derivatives": "vol_options",
    "E_portfolio_construction": "cross_asset",
    "F_risk_management": "cross_asset",
    "G_machine_learning": "other", "H_econometrics": "other",
    "I_microstructure": "microstructure", "J_research_integrity": "other",
    "K_institutions": "other", "L_behavioural_esg": "equity_xs",
    "M_asset_allocation": "cross_asset", "N_risk_premia": "equity_xs",
}


def _load_taxonomy():
    """[(term, family)] longest-first, for title matching."""
    if not TAGS_CSV.exists():
        return []
    rows = list(csv.DictReader(io.open(TAGS_CSV, encoding="utf-8")))
    terms = [(r["term"].lower(), r["family"]) for r in rows if r.get("term")]
    terms.sort(key=lambda t: -len(t[0]))
    return terms


_MARKET_SLEEVE = {
    "Equities": "equity_xs",
    "Bonds": "rates_credit",
    "Derivatives": "vol_options",
    "Commodities": "commodities",
    "Currencies": "fx",
    "Forex": "fx",
    # REITs and Cryptocurrencies have no desk sleeve; they stay "other" rather
    # than being forced into one.
}


def log(m):
    print(m, flush=True)


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _load(name):
    p = OUT / name
    if not p.exists():
        log(f"[core]   {name} absent -- that route contributes nothing")
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception as e:                                  # noqa: BLE001
        log(f"[core]   {name} unreadable: {type(e).__name__}")
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=2000)
    ap.add_argument("--min-indegree", type=int, default=2,
                    help="route C: how many seeds must cite a paper")
    ap.add_argument("--floor-frac", type=float, default=0.35,
                    help="share of the list reserved for per-sleeve coverage; "
                         "the rest goes to graph centrality")
    ap.add_argument("--resolve-gap", type=int, default=3000,
                    help="resolve this many unheld high-in-degree references "
                         "to titles via OpenAlex (needs OPENALEX_API_KEY); "
                         "0 disables")
    ap.add_argument("--from-pool", default="",
                    help="select from an existing compiled+cleaned pool CSV "
                         "(clean_core.py's output) instead of re-assembling "
                         "the routes. The cleaning verdicts in that file are "
                         "the point: re-running the routes would resurrect "
                         "every removed stray.")
    args = ap.parse_args()

    if args.from_pool:
        pool = _load_pool(args.from_pool)
        if not pool:
            log(f"[core] pool {args.from_pool} empty or unreadable")
            return 2
        log(f"[core] pool: {len(pool):,} cleaned candidates "
            f"from {args.from_pool}")
        if args.target == 0:
            # COMPILE mode over the cleaned pool: rank, do not reduce. The
            # whole pool IS the deliverable; selection stays a later decision.
            picked = sorted(pool, key=lambda r: (-r["seed_indegree"],
                                                 -r["score"]))
            for i, r in enumerate(picked, 1):
                r["rank"] = i
        else:
            picked = _select(pool, args.target, args.floor_frac)
        _write(picked, pool, None)
        return 0

    con = store.connect()

    # ---------------------------------------------------------------- archive
    items, by_doi, by_title = {}, {}, {}
    for uid, title, url, meta in con.execute(
            "SELECT uid, title, url, meta FROM items"):
        try:
            m = json.loads(meta) or {}
        except Exception:                                   # noqa: BLE001
            m = {}
        if m.get("retired"):
            continue
        rec = {"uid": uid, "title": title or m.get("title") or "",
               "url": url or "", "meta": m}
        items[uid] = rec
        doi = (m.get("doi") or "") or (uid[4:] if uid.startswith("doi:") else "")
        if doi:
            by_doi[doi.lower()] = uid
        if rec["title"]:
            by_title.setdefault(_norm(rec["title"])[:70], uid)
    log(f"[core] {len(items):,} live papers in the archive")

    cand: dict[str, dict] = {}

    def add(uid, route, **extra):
        """A candidate does NOT have to be in the archive.

        The first version gated every route on `uid in items`, which quietly
        inverted the purpose: Papers With Backtest contributed ZERO of its
        3,745 papers because none of them are held -- and the reason none are
        held is that the archive collects SSRN through a rolling 30-day window,
        so it has 2026 registrations while PWB has the historical literature.
        Those are precisely the papers a core list exists to surface.
        """
        c = cand.setdefault(uid, {"uid": uid, "routes": set(),
                                  "held": uid in items})
        c["routes"].add(route)
        for k, v in extra.items():
            if v not in (None, "", []) and not c.get(k):
                c[k] = v

    # ------------------------------------------------- E: the existing canon
    for uid, r in items.items():
        if r["meta"].get("classic"):
            add(uid, "canon")
    log(f"[core] route E canon      : {sum(1 for c in cand.values() if 'canon' in c['routes']):>6,}")

    # -------------------------------------------------------------- B: NBER
    n0 = len(cand)
    for uid, r in items.items():
        if (r["meta"].get("source") or "").upper().startswith("NBER"):
            add(uid, "nber")
    log(f"[core] route B nber       : {sum(1 for c in cand.values() if 'nber' in c['routes']):>6,}"
        f"  (+{len(cand)-n0:,} new)")

    # --------------------------------------- C: snowball over reference lists
    # The seeds are the papers we already trust -- the curated canon and NBER's
    # own editorial selection. What THEY cite, weighted by how many of them
    # agree, is the canon by the field's judgement rather than by taste.
    #
    # TWO TABLES, BECAUSE THEY ANSWER DIFFERENT QUESTIONS. graph.py writes
    # both: `cites` holds edges where BOTH ends are papers we hold, already
    # resolved to uids; `paper_refs` holds every reference including the ones
    # we do not hold, keyed by OpenAlex work id.
    #
    # The first version joined paper_refs back to uids through a
    # `meta["openalex_id"]` that NOTHING IN THIS REPO EVER WRITES, so the map
    # was empty and route C returned 0 while reporting 17,539 unheld
    # references -- the number was real, the zero was a bug.
    seeds = {u for u, c in cand.items() if c["routes"] & {"canon", "nber"}}
    log(f"[core] route C seeds      : {len(seeds):>6,} (canon + NBER)")

    held_deg = collections.Counter()
    unheld_deg = collections.Counter()
    seed_list = list(seeds)
    try:
        for i in range(0, len(seed_list), 900):
            chunk = seed_list[i:i + 900]
            q = ",".join("?" * len(chunk))
            for dst, n in con.execute(
                    f"SELECT dst, COUNT(*) FROM cites WHERE src IN ({q}) "
                    f"GROUP BY dst", chunk):
                held_deg[dst] += n
    except Exception as e:                                  # noqa: BLE001
        log(f"[core]   cites unavailable ({type(e).__name__})")
    try:
        for i in range(0, len(seed_list), 900):
            chunk = seed_list[i:i + 900]
            q = ",".join("?" * len(chunk))
            for (ref,) in con.execute(
                    f"SELECT ref FROM paper_refs WHERE src IN ({q})", chunk):
                unheld_deg[ref] += 1
    except Exception as e:                                  # noqa: BLE001
        log(f"[core]   paper_refs unavailable ({type(e).__name__})")

    hits = 0
    for uid, n in held_deg.items():
        if n >= args.min_indegree and uid in items:
            add(uid, "snowball", seed_indegree=n)
            hits += 1
    gap = [(r, n) for r, n in unheld_deg.items() if n >= args.min_indegree]
    gap.sort(key=lambda x: -x[1])
    log(f"[core] route C snowball   : {hits:>6,} held; {len(gap):,} cited by "
        f">={args.min_indegree} seeds that we do NOT hold")

    # THE UNHELD SET IS THE POINT. A paper many core papers cite, that the
    # archive does not contain, is exactly the hole a core list exists to
    # find. They arrive as bare OpenAlex ids, so resolve the top ones to real
    # titles -- 50 per request, which is why this needs the key.
    if gap and args.resolve_gap:
        import requests                                     # noqa: PLC0415
        import oa as oa_auth                                # noqa: PLC0415
        want = gap[:args.resolve_gap]
        log(f"[core]   resolving the top {len(want):,} unheld references "
            f"({(len(want)+49)//50} requests)")
        ids = [r for r, _ in want]
        deg = dict(want)
        got = 0
        for i in range(0, len(ids), 50):
            batch = [x.rsplit("/", 1)[-1] for x in ids[i:i + 50]]
            try:
                rr = requests.get(
                    "https://api.openalex.org/works",
                    headers=oa_auth.headers({"User-Agent": "quant-digest/1.0"}),
                    params={"filter": "openalex_id:" + "|".join(batch),
                            "select": "id,doi,title,publication_year,"
                                      "cited_by_count",
                            "per-page": 50},
                    timeout=60)
                if not rr.ok:
                    continue
                for w in (rr.json().get("results") or []):
                    doi = (w.get("doi") or "").replace("https://doi.org/", "")
                    uid = f"doi:{doi.lower()}" if doi else f"oa:{w.get('id','').rsplit('/',1)[-1]}"
                    add(uid, "snowball",
                        seed_indegree=deg.get(w.get("id"), 0),
                        ext_title=w.get("title"),
                        ext_year=w.get("publication_year"),
                        ext_cites=w.get("cited_by_count"))
                    got += 1
            except Exception as e:                          # noqa: BLE001
                log(f"[core]   gap batch failed: {type(e).__name__}: "
                    f"{str(e)[:90]}")
        log(f"[core]   resolved {got:,} unheld references into candidates")

    # ----------------------------------------------- F/G/D: harvested sources
    # ------------------------------------------------------ A: the tag sweep
    sweep = _load("core_sweep.json")
    for r in sweep:
        if r.get("uid"):
            add(r["uid"], "sweep", family=r.get("family"), tag=r.get("tag"),
                ext_title=r.get("title"), ext_year=r.get("year"),
                ext_cites=r.get("cites"))
    log(f"[core] route A sweep      : "
        f"{sum(1 for c in cand.values() if 'sweep' in c['routes']):>6,}"
        f"  ({len(sweep):,} harvested)")

    pwb = {r["uid"]: r for r in _load("core_pwb.json") if r.get("uid")}
    for uid, r in pwb.items():
        if True:
            add(uid, "pwb", sharpe=r.get("sharpe"),
                backtest_period=r.get("backtest_period"),
                publication_date=r.get("publication_date"),
                markets=r.get("markets"), pwb_title=r.get("title"),
                ext_title=r.get("title"))
    log(f"[core] route F pwb        : {sum(1 for c in cand.values() if 'pwb' in c['routes']):>6,}"
        f"  ({len(pwb):,} harvested, {len(pwb)-sum(1 for u in pwb if u in items):,} not in the archive)")

    # SignalDoc names papers by AUTHOR and YEAR, not by title --
    # `LongDescription` is a description of the signal ("Abnormal Accruals"),
    # which is why title-matching found 0 of 331. Join on the first author
    # surname plus year (tolerance 1: CZ dates the journal version), falling
    # back to a title-substring check to break ties.
    surname_ix: dict[str, list] = {}
    for uid_, rec_ in items.items():
        m_ = rec_["meta"]
        yr = m_.get("pub_year") or (m_.get("date") or "")[:4]
        try:
            yr = int(yr)
        except (TypeError, ValueError):
            continue
        for chunk in re.split(r"[,;&]| and ", m_.get("authors") or ""):
            parts = [w for w in chunk.strip().split() if len(w) > 2]
            if parts:
                surname_ix.setdefault(parts[-1].lower(), []).append(
                    (yr, uid_, _norm(rec_["title"])))

    sig = _load("core_signaldoc.json")
    sig_hits = 0
    for r in sig:
        first = re.split(r"[,;&]| and ", r.get("authors") or "")[0].strip()
        surname = (first.split()[-1] if first.split() else "").lower()
        try:
            want = int(r.get("year") or 0)
        except (TypeError, ValueError):
            want = 0
        uid = None
        best = None
        for yr, uid_, tnorm in surname_ix.get(surname, []):
            if want and abs(yr - want) > 1:
                continue
            score_ = (abs(yr - want),
                      0 if _norm(r.get("title_desc") or "")[:20] in tnorm else 1)
            if best is None or score_ < best[0]:
                best = (score_, uid_)
        if best:
            uid = best[1]
        uid = uid or f"sig:{r.get('acronym')}"
        add(uid, "signaldoc", replication=r.get("replication"),
            predictability=r.get("predictability"),
            ext_title=r.get("title_desc"), ext_year=r.get("year"),
            author=r.get("authors"))
        sig_hits += 1 if uid in items else 0
    log(f"[core] route G signaldoc  : {sig_hits:>6,} matched to held papers "
        f"({len(sig):,} predictors; unmatched enter as sig: rows)")

    for r in _load("core_quantseeker.json"):
        if r.get("uid"):
            add(r["uid"], "quantseeker", ext_title=r.get("post"))
    log(f"[core] route D quantseeker: {sum(1 for c in cand.values() if 'quantseeker' in c['routes']):>6,}")

    auth = _load("watched_author_papers.json")
    for r in auth:
        doi = (r.get("doi") or "").lower()
        uid = by_doi.get(doi) or (f"doi:{doi}" if doi else None)
        if uid:
            add(uid, "authors", author=r.get("author"),
                ext_title=r.get("title"), ext_cites=r.get("cites"),
                ext_year=r.get("year"))
    log(f"[core] route D authors    : {sum(1 for c in cand.values() if 'authors' in c['routes']):>6,}"
        f"  ({len(auth):,} candidates, most not yet in the archive)")

    taxonomy = _load_taxonomy()
    log(f"[core] taxonomy: {len(taxonomy)} terms loaded for labelling")

    # ------------------------------------------------------------ deduplicate
    # A paper reaches this pool under every identifier it has ever carried.
    # "Value and Momentum Everywhere" arrived FOUR times -- three SSRN preprint
    # ids from Papers With Backtest and the published JF doi from the author
    # harvest -- so its evidence split four ways and not one fragment scored
    # high enough to be selected. A bread-and-butter paper fell out of the core
    # list because it was too well documented.
    #
    # Merge on the normalised title, keep the richest identifier (a published
    # DOI beats a preprint id beats a bare OpenAlex id), and UNION the routes,
    # because route agreement is the signal this list is built on.
    def _rank_uid(u):
        if u.startswith("doi:10.2139"):      # SSRN preprint
            return 1
        if u.startswith("doi:"):             # published DOI
            return 3
        if u.startswith("arxiv:"):
            return 2
        return 0                             # oa:, sig:, title hash

    merged: dict[str, dict] = {}
    for uid, c in cand.items():
        title = ((items[uid]["title"] if uid in items else "")
                 or c.get("ext_title") or c.get("pwb_title") or "")
        key = _norm(title)[:70] or uid
        prev = merged.get(key)
        if prev is None:
            merged[key] = c
            continue
        prev["routes"] |= c["routes"]
        prev["seed_indegree"] = max(prev.get("seed_indegree", 0),
                                    c.get("seed_indegree", 0))
        for k, v in c.items():
            if k not in ("routes", "seed_indegree") and v not in (None, "", [])                     and not prev.get(k):
                prev[k] = v
        if _rank_uid(c["uid"]) > _rank_uid(prev["uid"]):
            prev["uid"] = c["uid"]
            prev["held"] = c.get("held", prev.get("held"))
    dropped = len(cand) - len(merged)
    log(f"[core] deduplicated {len(cand):,} -> {len(merged):,} "
        f"({dropped:,} were the same paper under another identifier)")
    cand = {c["uid"]: c for c in merged.values()}

    # ----------------------------------------------------------------- score
    rows = []
    for uid, c in cand.items():
        m = items[uid]["meta"] if uid in items else {}
        cites = m.get("cites") if m.get("cites") is not None else c.get("ext_cites")
        year = m.get("pub_year") or (m.get("date") or "")[:4] or c.get("ext_year")
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None
        age = max(1, 2026 - year) if year else None
        cpy = (cites / age) if (isinstance(cites, int) and age) else None
        # family/tag come from the taxonomy: either the term that FOUND the
        # paper (route A), or the longest taxonomy term its title contains.
        # One vocabulary for discovery and labelling.
        family, tag = c.get("family", ""), c.get("tag", "")
        if not tag:
            t = _norm(items[uid]["title"] if uid in items else
                      (c.get("ext_title") or ""))
            for term, fam in taxonomy:
                if term in t:
                    family, tag = fam, term
                    break
        # sleeve: the archive label if held; else the tag's own sleeve; else
        # the family default. `markets` stays its OWN column -- PWB's asset
        # class is additional evidence, never a substitute for the taxonomy.
        sleeve = (m.get("sleeves_prop") or m.get("sleeves") or [])
        sleeve = sleeve[0] if isinstance(sleeve, list) and sleeve else ""
        if not sleeve and tag:
            sleeve = TAG_SLEEVE.get(tag, "")
        if not sleeve and family:
            sleeve = FAMILY_SLEEVE.get(family, "")
        if not sleeve:
            sleeve = _MARKET_SLEEVE.get(
                (c.get("markets") or "").split(",")[0].strip(), "")
        sleeve = sleeve or "other"

        # log-scaled citations, age-normalised velocity, plus route agreement.
        # Route agreement carries real weight: independent sources converging
        # on a paper is evidence no single citation count provides.
        s_cites = math.log10(1 + (cites or 0))
        s_vel = math.log10(1 + (cpy or 0)) * 1.5
        s_seed = math.log10(1 + c.get("seed_indegree", 0)) * 2.0
        s_route = len(c["routes"]) * 0.6
        s_pract = 0.5 if c["routes"] & {"pwb", "authors", "quantseeker"} else 0
        s_repl = 0.8 if "signaldoc" in c["routes"] else 0

        rows.append({
            "uid": uid,
            "title": ((items[uid]["title"] if uid in items else "")
                      or c.get("ext_title") or "")[:200],
            "year": year or "",
            "doi": m.get("doi") or (uid[4:] if uid.startswith("doi:") else ""),
            "cites": cites if cites is not None else "",
            "cites_per_year": round(cpy, 1) if cpy else "",
            "seed_indegree": c.get("seed_indegree", 0),
            "n_routes": len(c["routes"]),
            "found_by": "+".join(sorted(c["routes"])),
            "sleeve": sleeve,
            "family": family, "tag": tag,
            "sharpe": c.get("sharpe", ""),
            "backtest_period": c.get("backtest_period", ""),
            "publication_date": c.get("publication_date", ""),
            "replication": c.get("replication", ""),
            "predictability": c.get("predictability", ""),
            "markets": c.get("markets", ""),
            "held": int(bool(c.get("held"))),
            "score": round(s_cites + s_vel + s_seed + s_route + s_pract + s_repl, 3),
        })

    rows.sort(key=lambda r: -r["score"])

    # ------------------------------------- centrality first, coverage second
    # THIS IS A GRAPH CORE, NOT A BALANCED READING LIST. What makes a paper
    # core is that the literature we already trust points AT it -- that is what
    # a hub is, and seed_indegree measures it directly. An equal-quota split
    # got this backwards: it forced in 181 microstructure papers to fill a
    # bucket while pushing out papers hundreds of core papers cite.
    #
    # So centrality takes the majority of the list, and the quota becomes a
    # FLOOR that guarantees no sleeve is empty rather than a cap that
    # guarantees they are equal.
    if args.target == 0:
        # COMPILE mode: no reduction. Every deduplicated candidate goes out
        # with its evidence columns, ranked but not cut -- selection is a
        # decision, and --target 0 leaves it with the reviewer.
        picked = rows
        picked.sort(key=lambda r: (-r["seed_indegree"], -r["score"]))
        for i, r in enumerate(picked, 1):
            r["rank"] = i
        _write(picked, cand, taxonomy)
        return 0

    picked = _select(rows, args.target, args.floor_frac)
    _write(picked, cand, taxonomy)
    return 0


def _select(rows, target, floor_frac):
    """Centrality first, coverage second. Shared by the full route-assembly
    path and --from-pool, so there is exactly ONE notion of selection."""
    rows = sorted(rows, key=lambda r: -r["score"])
    floor = max(1, int(target * floor_frac / len(SLEEVES)))
    picked, seen, bysleeve = [], set(), collections.Counter()

    # GUARANTEES BEFORE SCORE. Two classes of candidate are core by
    # construction, not by metric: the curated canon, and papers three or more
    # independent routes converged on. The canon needs this because its ingest
    # carried no citation metadata -- Fama-French 1993 sat in the pool at
    # score 0.6 with an empty cites column and a title-hash uid the citation
    # graph cannot attach to, so a score-only cut dropped the single most
    # canonical paper on the list. Score cannot be the gatekeeper for the
    # rows whose metadata is thinnest.
    for r in rows:
        if ("canon" in (r.get("found_by") or "")) or r.get("n_routes", 0) >= 3:
            picked.append(r); seen.add(r["uid"]); bysleeve[r["sleeve"]] += 1
    log(f"[core] guaranteed         : {len(picked):,} "
        f"(curated canon + found by >=3 routes)")

    central = [r for r in rows
               if r["seed_indegree"] > 0 and r["uid"] not in seen]
    central.sort(key=lambda r: (-r["seed_indegree"], -r["score"]))
    n_central = int(target * (1 - floor_frac))
    already = sum(1 for r in picked if r["seed_indegree"] > 0)
    for r in central[:max(0, n_central - already)]:
        picked.append(r); seen.add(r["uid"]); bysleeve[r["sleeve"]] += 1
    log(f"[core] + centrality picks : {len(picked):,} "
        f"(cited by >=1 seed; top by in-degree)")

    for sl in SLEEVES:                   # floor: nobody starves
        if bysleeve[sl] >= floor:
            continue
        for r in rows:
            if bysleeve[sl] >= floor:
                break
            if r["sleeve"] == sl and r["uid"] not in seen:
                picked.append(r); seen.add(r["uid"]); bysleeve[sl] += 1
    log(f"[core] after sleeve floor : {len(picked):,} (floor {floor}/sleeve)")

    for r in rows:                       # remainder by score
        if len(picked) >= target:
            break
        if r["uid"] not in seen:
            picked.append(r); seen.add(r["uid"])
    picked.sort(key=lambda r: (-r["seed_indegree"], -r["score"]))
    for i, r in enumerate(picked, 1):
        r["rank"] = i
    return picked


def _load_pool(path):
    """Read clean_core.py's pool CSV back into selection-ready rows."""
    p = pathlib.Path(path)
    if not p.exists():
        return []
    rows = []
    with io.open(p, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                r["score"] = float(r.get("score") or 0)
                r["seed_indegree"] = int(float(r.get("seed_indegree") or 0))
                r["n_routes"] = int(float(r.get("n_routes") or 0))
                r["held"] = int(float(r.get("held") or 0))
            except ValueError:
                continue
            if r.get("cites") not in (None, ""):
                try:
                    r["cites"] = int(float(r["cites"]))
                except ValueError:
                    r["cites"] = ""
            r["sleeve"] = r.get("sleeve") or "other"
            rows.append(r)
    return rows


def _write(picked, cand, taxonomy):
    import collections
    OUT.mkdir(parents=True, exist_ok=True)
    cols = ["rank", "title", "year", "doi", "cites", "cites_per_year",
            "seed_indegree", "n_routes", "found_by", "sleeve", "family",
            "tag", "sharpe",
            "backtest_period", "publication_date", "replication",
            "predictability", "markets", "held", "score", "uid"]
    with io.open(OUT / "core_candidates.csv", "w", newline="",
                 encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in cols} for r in picked)
    # Indented JSON is for reading a short list by eye. At pool scale it is
    # a few hundred MB of whitespace, so it goes out compact instead.
    (OUT / "core_candidates.json").write_text(
        json.dumps(picked, indent=None if len(picked) > 20000 else 1,
                   ensure_ascii=False, separators=(",", ":")
                   if len(picked) > 20000 else None),
        encoding="utf-8")

    # ------------------------------------------------ the human review file
    # The CSV is for sorting; this is for READING. Grouped by sleeve with the
    # evidence inline, so the review pass the plan requires -- spot-read the
    # top 100 before anything downstream is built -- does not require a
    # spreadsheet.
    md = ["# Core candidates -- review copy",
          f"\n{len(picked):,} papers selected from {len(cand):,} candidates. "
          f"Columns carry the evidence; a paper found by several independent "
          f"routes is core because they agree, not because anyone asserted it.",
          ""]
    bysl = {}
    for r in picked:
        bysl.setdefault(r["sleeve"], []).append(r)
    for sl in sorted(bysl, key=lambda k: -len(bysl[k])):
        grp = bysl[sl]
        md.append(f"\n## {sl} ({len(grp)})\n")
        for r in grp[:40]:
            ev = []
            if int(r.get("seed_indegree") or 0):
                ev.append(f"cited by {r['seed_indegree']} seeds")
            if r.get("cites"):
                ev.append(f"{r['cites']:,} cites" if isinstance(r["cites"], int)
                          else f"{r['cites']} cites")
            if r.get("sharpe") not in ("", None):
                ev.append(f"Sharpe {round(float(r['sharpe']), 2)}")
            if r.get("replication"):
                ev.append(f"replication {r['replication']}")
            if int(r.get("n_routes") or 0) >= 2:
                ev.append(f"{r['n_routes']} routes: {r['found_by']}")
            md.append(f"- **{(r.get('title') or '(untitled)')[:110]}** "
                      f"({r.get('year') or '?'}) -- {'; '.join(ev) or 'floor pick'}")
        if len(grp) > 40:
            md.append(f"- *... and {len(grp)-40} more (see the CSV)*")
    (OUT / "core_review.md").write_text("\n".join(md), encoding="utf-8")
    log(f"[core] review copy -> {OUT}/core_review.md")

    log(f"\n[core] {len(cand):,} candidates -> {len(picked):,} selected")
    log(f"[core] by sleeve: {dict(collections.Counter(r['sleeve'] for r in picked).most_common())}")
    log(f"[core] by routes: {dict(collections.Counter(r['n_routes'] for r in picked))}")
    multi = [r for r in picked if r["n_routes"] >= 3]
    log(f"[core] {len(multi):,} found by 3+ independent routes -- the strongest core")
    log(f"[core] written to {OUT}/core_candidates.csv -- nothing ingested")


if __name__ == "__main__":
    sys.exit(main())
