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
    args = ap.parse_args()

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
    # The seeds are the papers we already trust: the curated canon and NBER's
    # own editorial selection. What THEY cite, weighted by how many of them
    # agree, is the canon by the field's judgement rather than by taste.
    seeds = {u for u, c in cand.items()
             if c["routes"] & {"canon", "nber"}}
    indeg = collections.Counter()
    try:
        qmarks = ",".join("?" * min(len(seeds), 900))
        seed_list = list(seeds)
        for i in range(0, len(seed_list), 900):
            chunk = seed_list[i:i + 900]
            q = ",".join("?" * len(chunk))
            for (ref,) in con.execute(
                    f"SELECT ref FROM paper_refs WHERE src IN ({q})", chunk):
                indeg[ref] += 1
    except Exception as e:                                  # noqa: BLE001
        log(f"[core] route C unavailable ({type(e).__name__}) -- "
            f"run tools/graph.py cites first")
    if not indeg:
        log("[core] route C snowball  :      0  -- paper_refs is empty here; "
            "run tools/graph.py cites (339,411 rows exist on the live db)")
    if indeg:
        # paper_refs stores OpenAlex work ids; map back to uids where we hold
        # the paper. A ref we do not hold is a real signal but not a candidate
        # we can describe, so it is counted and reported, not invented.
        oa_map = {}
        try:
            for uid, _t, _u, meta in con.execute(
                    "SELECT uid, title, url, meta FROM items"):
                m = json.loads(meta or "{}")
                oid = m.get("openalex_id") or m.get("oa_id")
                if oid:
                    oa_map[oid] = uid
        except Exception:                                   # noqa: BLE001
            pass
        hits = unheld = 0
        for ref, n in indeg.items():
            if n < args.min_indegree:
                continue
            uid = oa_map.get(ref)
            if uid and uid in items:
                add(uid, "snowball", seed_indegree=n)
                hits += 1
            else:
                unheld += 1
        log(f"[core] route C snowball   : {hits:>6,}  "
            f"({unheld:,} highly-cited refs we do NOT hold -- the gap worth "
            f"filling next)")

    # ----------------------------------------------- F/G/D: harvested sources
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

    sig = _load("core_signaldoc.json")
    sig_hits = 0
    for r in sig:
        key = _norm(r.get("title_desc") or "")[:70]
        uid = by_title.get(key) or f"sig:{r.get('acronym')}"
        add(uid, "signaldoc", replication=r.get("replication"),
            predictability=r.get("predictability"),
            ext_title=r.get("title_desc"), ext_year=r.get("year"),
            author=r.get("authors"))
        sig_hits += 1 if uid in items else 0
    log(f"[core] route G signaldoc  : {sig_hits:>6,}  ({len(sig):,} predictors, "
        f"matched on title)")

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
        sleeve = (m.get("sleeves_prop") or m.get("sleeves") or [])
        sleeve = sleeve[0] if isinstance(sleeve, list) and sleeve else ""
        if not sleeve:
            # An UNHELD candidate has no sleeve label, and defaulting it to
            # "other" put 1,121 of 2,000 selections there -- which defeats the
            # per-sleeve quota that exists to stop equity research crowding out
            # a macro desk. PWB ships an asset-class tag per paper; using it is
            # better than a shrug, and it is external rather than inferred.
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

    # ------------------------------------------------- quotas, then the tail
    per = max(1, args.target // len(SLEEVES))
    picked, seen, bysleeve = [], set(), collections.Counter()
    for r in rows:
        if bysleeve[r["sleeve"]] < per:
            picked.append(r); seen.add(r["uid"]); bysleeve[r["sleeve"]] += 1
    for r in rows:                       # fill the remainder by pure score
        if len(picked) >= args.target:
            break
        if r["uid"] not in seen:
            picked.append(r); seen.add(r["uid"])
    picked.sort(key=lambda r: -r["score"])
    for i, r in enumerate(picked, 1):
        r["rank"] = i

    OUT.mkdir(parents=True, exist_ok=True)
    cols = ["rank", "title", "year", "doi", "cites", "cites_per_year",
            "seed_indegree", "n_routes", "found_by", "sleeve", "sharpe",
            "backtest_period", "publication_date", "replication",
            "predictability", "markets", "held", "score", "uid"]
    with io.open(OUT / "core_candidates.csv", "w", newline="",
                 encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in cols} for r in picked)
    (OUT / "core_candidates.json").write_text(
        json.dumps(picked, indent=1, ensure_ascii=False), encoding="utf-8")

    log(f"\n[core] {len(cand):,} candidates -> {len(picked):,} selected")
    log(f"[core] by sleeve: {dict(collections.Counter(r['sleeve'] for r in picked).most_common())}")
    log(f"[core] by routes: {dict(collections.Counter(r['n_routes'] for r in picked))}")
    multi = [r for r in picked if r["n_routes"] >= 3]
    log(f"[core] {len(multi):,} found by 3+ independent routes -- the strongest core")
    log(f"[core] written to {OUT}/core_candidates.csv -- nothing ingested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
