"""Monthly composite scoring, shared by the present-month recompute and the
backward backfill.

Design intent: the LLM is confined to purely SUBJECTIVE judgment (novelty,
generality, usefulness) -- everything quantifiable from real data (citation
count, citation velocity, author/venue track record) is computed here in code,
never guessed by the LLM (see llm.py's module docstring for the "extract,
never invent the rank" design). Concretely:

  composite = (QUALITY_WEIGHT*base_quality + CITES_WEIGHT*paper_cites_norm
               + VELOCITY_WEIGHT*velocity_norm) * R * M_rep

  base_quality (SUBJECTIVE, LLM)   -- weighted avg of the LLM's anchored
    generality/contribution/testability levels (0-3, rescaled 0-100).
    contribution is capped at level 2 when the LLM marked it `provisional`
    (abstract alone can't rule out a direct antecedent).
  paper_cites_norm (QUANTITATIVE)  -- this paper's own S2 citation count (log,
    min-max in-pool) -- direct empirical evidence, a real weight not a nudge.
  velocity_norm (QUANTITATIVE)     -- this paper's real citation velocity
    (S2 citationCount / years since publication, log, min-max in-pool),
    computed only when the paper is >=1 year old with a known citation count.
  Any of the two quantitative terms that's unavailable for an item (too new)
  is excluded and its weight redistributed onto base_quality -- a paper is
  never penalised for simply being new.

  R      -- soft robustness DISCOUNT (never a bonus; floored at
            config.ROBUSTNESS_FLOOR): multiplies in
            config.ROBUSTNESS_DISCOUNTS[flag] for each robustness flag the LLM
            found EXPLICITLY stated in the abstract. A null flag (abstract
            simply didn't say) is never penalised -- absence of information
            isn't evidence of a problem.
  M_rep  -- bounded REPUTATION multiplier in [1-CRED_BOUND, 1+CRED_BOUND] (i.e.
            [0.85, 1.15]) from S2 author h-index + JOURNAL_IMPACT ONLY (never
            this paper's own citations, which get a real weight above
            instead): these are priors about the author's/venue's general
            track record, not evidence about this specific paper, so they can
            only nudge, never carry.

Gate: an item needs a relevance_category other than "off_topic" (the LLM's
independent core_fit/adjacent/off_topic verdict -- see llm.relevance_posterior)
to be composite-eligible at all.

Flow: attach_s2() fills abstract/cites/author_h/pub_year; llm_score() attaches
the anchored rubric levels + robustness flags + topic + summary (respecting a
per-run batch budget); composite_entries() ranks and returns the top-N.
"""

import datetime as dt
import json
import math
import os
import re
import time

import requests

import abstracts
import config
import oa as oa_auth   # noqa: E402
import llm

_OA_UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}


def _openalex_cites(items: list[dict], log=print) -> None:
    """Fallback citation/author-h enrichment via OpenAlex (in place), for
    DOI-bearing items S2 left without citations -- S2's unauthenticated pool is
    heavily rate-limited and routinely returns nothing, silently starving the
    composite of its citation + reputation signals. OpenAlex's polite pool is
    reliable for the ~150 items/run the digest enriches. Bulk (<=50 DOIs)."""
    need = [it for it in items
            if it.get("doi") and (it.get("cites") is None or it.get("author_h") is None)]
    if not need:
        return
    mail = os.environ.get("CONTACT_EMAIL") or os.environ.get("GMAIL_ADDRESS")
    by_doi = {it["doi"].lower(): it for it in need}
    dois = list(by_doi)
    filled = 0
    batches = failed_batches = 0
    for i in range(0, len(dois), 50):
        chunk = dois[i:i + 50]
        params = {"filter": "doi:" + "|".join("https://doi.org/" + d for d in chunk),
                  "select": "doi,cited_by_count,publication_year,authorships",
                  "per-page": 50}
        if mail:
            params["mailto"] = mail
        data = None
        for attempt in range(4):
            try:
                r = requests.get("https://api.openalex.org/works", params=params,
                                 headers=oa_auth.headers(_OA_UA), timeout=45)
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1) + 1)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception:                          # noqa: BLE001
                time.sleep(1.5 * (attempt + 1))
        batches += 1
        if data is None:
            failed_batches += 1
        for w in (data or {}).get("results", []):
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            it = by_doi.get(doi)
            if not it:
                continue
            if it.get("cites") is None and w.get("cited_by_count") is not None:
                it["cites"] = w.get("cited_by_count")
                filled += 1
            # (author_h isn't on OpenAlex work authorships; S2 + prominence.py
            # cover it -- citations are the signal being rescued here)
            if it.get("pub_year") is None and w.get("publication_year"):
                it["pub_year"] = w.get("publication_year")
        time.sleep(0.4)
    if filled:
        log(f"[enrich] OpenAlex filled citations for {filled} items S2 missed")
    elif dois:
        # The ONLY log in this function used to be gated on success, so a
        # rejected key or a full outage produced zero output lines -- and this
        # function exists as the rescue for S2 already returning nothing, so
        # both failing silently loses the citation signal entirely.
        log(f"[enrich] OpenAlex filled NOTHING for {len(dois):,} items S2 "
            f"missed ({failed_batches} of {batches} batches failed outright)"
            + ("  -- check the key: python -c \"import oa; oa.preflight()\""
               if failed_batches else ""))
    if failed_batches:
        log(f"[enrich] !! {failed_batches} of {batches} OpenAlex batches "
            f"returned nothing after 4 attempts")

# Non-paper records that occasionally slip through a source (Crossref front
# matter, a blog's own link-roundup post) and would otherwise rank on
# journal_if or LLM enthusiasm alone despite not being a paper.
_JUNK_TITLE = re.compile(
    r"^(editorial board|table of contents|front matter|masthead|"
    r"issue information|recent quant links|weekly (?:roundup|links)|"
    r"links? (?:roundup|round-up))\b", re.I)


def is_junk(title: str) -> bool:
    return bool(_JUNK_TITLE.match((title or "").strip()))


def _label(it: dict) -> str:
    return (it.get("journal_label")
            or str(it.get("source", "")).replace("journal:", ""))


def _norm_counts(raw: list[float], logscale: bool) -> list[float]:
    """Min-max to 0-100 (log first for citations). All-zero -> all 0; all-equal
    nonzero -> all 100 (a ranking-neutral constant)."""
    if not raw:
        return []
    if max(raw) <= 0:
        return [0.0] * len(raw)
    vals = [math.log1p(max(0.0, x)) if logscale else float(max(0.0, x)) for x in raw]
    lo, hi = min(vals), max(vals)
    span = hi - lo
    return [100.0 * (v - lo) / span if span > 0 else 100.0 for v in vals]


def attach_s2(items: list[dict], log=print) -> list[dict]:
    """Fill missing abstract + citation count + author h-index from Semantic
    Scholar for DOI-bearing items (one batched call). Mutates in place."""
    need = [it for it in items if it.get("doi")]
    if not need:
        return items
    s2 = abstracts.s2_papers([it["doi"] for it in need], log)
    for it in need:
        d = s2.get(it["doi"].lower())
        if not d:
            continue
        if not it.get("abstract") and d.get("abstract"):
            it["abstract"] = d["abstract"][:1500]
        if it.get("cites") is None and d.get("cites") is not None:
            it["cites"] = d["cites"]
        if it.get("author_h") is None and d.get("author_h") is not None:
            it["author_h"] = d["author_h"]
        if it.get("pub_year") is None and d.get("year") is not None:
            it["pub_year"] = d["year"]
    # OpenAlex fallback for anything S2 left without citations (S2's free pool
    # is often fully rate-limited -- this keeps the composite's citation signal
    # alive instead of silently dropping it)
    try:
        _openalex_cites(need, log)
    except Exception as e:                             # noqa: BLE001
        log(f"[enrich] OpenAlex fallback failed: {type(e).__name__}: {e}")
    return items


# ---------------------------------------------------- author score
# A bounded 0-100 reputation signal per paper, scored on its STRONGEST author
# (a junior lead with a star co-author still benefits). Fed by career impact
# (h-index), recent momentum (2yr citations), being a tracked/seminal author
# (the watchlist roster), and venue track record. It only NUDGES the composite
# (via the bounded M_rep multiplier) -- it can lift a paper but never carry a
# weak one, keeping prestige from dominating quality.
_TOP_VENUES = {"journal of finance", "journal of financial economics",
               "review of financial studies", "journal of financial and "
               "quantitative analysis", "review of finance", "journal of "
               "financial and quantitative", "econometrica", "american economic "
               "review", "review of economic studies", "quarterly journal of "
               "economics", "journal of political economy"}
_SOURCE_BONUS = {"canon": 30, "seed": 20, "auto": 15}    # membership -> boost


def _name_key(name: str):
    n = re.sub(r"[^a-z\s-]", "", (name or "").lower().replace(".", " "))
    toks = [t for t in n.split() if t]
    return (toks[0], toks[-1]) if len(toks) >= 2 else None


def _load_roster() -> dict:
    """Roster keyed by (first,last) name-key -> author metadata, so a paper's
    authors can be matched to their watchlist/canon entry."""
    try:
        authors = (json.loads(open(os.path.join("docs", "watchlist.json"),
                   encoding="utf-8").read()) or {}).get("authors", {})
    except Exception:                                  # noqa: BLE001
        return {}
    by_key = {}
    for a in authors.values():
        k = _name_key(a.get("name", ""))
        if k:
            by_key[k] = a
    return by_key


def _venue_component(meta: dict) -> float:
    mix = meta.get("venue_mix") or {}
    tot = sum(mix.values()) or 1
    top = sum(n for v, n in mix.items() if v.lower() in _TOP_VENUES)
    return 100.0 * top / tot


def author_score(it: dict, roster: dict) -> float:
    """0-100 reputation for the paper's STRONGEST author, POOL-INDEPENDENT (an
    absolute h-index scale, not in-pool) so the SAME score applies everywhere --
    Monthly, Recent, For You, the email. Centred so 50 is neutral (no M_rep
    effect): h-index 25 (solid mid-career) = 50; being a tracked/seminal author
    (roster membership) + recent citations + top-venue track record ADD on top;
    an unknown author with an unknown/median h-index sits at neutral, never
    penalised for obscurity. The roster h-index is used when it exceeds the
    paper's (S2 often undercounts the best author's h)."""
    best = None                                        # strongest roster match
    for author in re.split(r"[;,]", it.get("authors", "") or ""):
        k = _name_key(author)
        m = roster.get(k) if k else None
        if m and (best is None
                  or _SOURCE_BONUS.get(m.get("source"), 0)
                  > _SOURCE_BONUS.get(best.get("source"), 0)):
            best = m

    h = it.get("author_h")
    if best and best.get("h_index") is not None:
        h = max(h or 0, best["h_index"])
    h_comp = 50.0 if h is None else min(100.0, h * 2.0)   # h=25 -> 50 neutral
    bonus = 0.0
    if best:
        h_comp = max(h_comp, 55.0)                     # a tracked author isn't sub-neutral
        rc = best.get("recent_cites") or 0
        bonus = (_SOURCE_BONUS.get(best.get("source"), 0)
                 + min(15.0, math.log10(rc + 1) / 4 * 15)   # recent momentum, +0..15
                 + 0.08 * _venue_component(best))           # top-venue rate, +0..8
    return max(0.0, min(100.0, h_comp + bonus))


def rep_multiplier(a_score: float, journal_if_pct: float | None = None) -> float:
    """The bounded ±CRED_BOUND reputation multiplier from the author score
    (optionally blended with the journal's impact-factor percentile). 50 ->
    1.0 (neutral). Shared by every ranking surface so reputation nudges
    consistently and can never carry a weak paper."""
    inputs = [a_score] + ([journal_if_pct] if journal_if_pct is not None else [])
    rep_avg = sum(inputs) / len(inputs)
    return (1 - config.CRED_BOUND) + 2 * config.CRED_BOUND * (rep_avg / 100)


def annotate_reputation(items: list[dict], log=print) -> list[dict]:
    """Attach author_score + reputation (M_rep) to every item in place, so the
    daily surfaces (Recent, For You, email, data.json) apply the SAME author
    nudge the Monthly composite does. Pool-independent, so values match across
    surfaces. No-op-safe: unknown authors get the neutral 50 / 1.0."""
    roster = _load_roster()
    if_max = max(config.JOURNAL_IMPACT.values()) or 1.0
    for it in items:
        a = author_score(it, roster)
        if_pct = None
        # _label() strips the "journal:" prefix that source carries; without it
        # the lookup key was "journal:Journal of Finance" against a table keyed
        # "Journal of Finance" -- 139 rows had a venue in the table and every
        # one of them missed. composite_entries already keys it this way.
        jif = config.JOURNAL_IMPACT.get(_label(it))
        if jif is not None:
            if_pct = 100.0 * jif / if_max
        it["author_score"] = round(a, 1)
        it["reputation"] = round(rep_multiplier(a, if_pct), 3)
    return items


def llm_score(items: list[dict], log, max_batches: int | None = None) -> list[dict]:
    """LLM-score only the not-yet-scored, non-junk items (anchored rubric
    levels + robustness flags + topic + summary), most-cited first so a batch
    budget spends on the strongest candidates. Mutates in place; leaves items
    past the budget unscored for a later run. Junk records (editorial front
    matter, a blog's own link-roundup post) are skipped entirely -- never worth
    spending LLM quota on."""
    todo = [it for it in items
            if it.get("relevance") is None and not is_junk(it.get("title", ""))]
    # watchlist items FIRST (never dropped by the batch budget), then most-cited
    todo.sort(key=lambda it: (bool(it.get("watchlist")), it.get("cites") or 0),
              reverse=True)
    llm.rank(todo, log, max_batches=max_batches)
    return items


def score_new(items: list[dict], log, max_batches: int | None = None) -> list[dict]:
    """Full pass for freshly-fetched items: S2 enrich then LLM score."""
    attach_s2(items, log)
    llm_score(items, log, max_batches=max_batches)
    return items


def _age_years(it: dict) -> float | None:
    yr = it.get("pub_year")
    if not yr:
        d = str(it.get("date") or "")[:4]
        yr = int(d) if d.isdigit() else None
    if not yr:
        return None
    return max(0.0, dt.date.today().year - int(yr))


def _level(it: dict, axis: str) -> int | None:
    node = it.get(axis)
    return node.get("level") if isinstance(node, dict) else None


def _robustness_discount(it: dict) -> float:
    """R: multiply in a discount for every EXPLICITLY-detected flag; a null
    flag (abstract didn't say) never contributes a penalty. Floored."""
    r = 1.0
    for flag, factor in config.ROBUSTNESS_DISCOUNTS.items():
        if it.get(flag) is True:
            r *= factor
    return max(config.ROBUSTNESS_FLOOR, r)


def composite_entries(items: list[dict], n: int) -> list[dict]:
    """Compute composite = (QUALITY_WEIGHT*base_quality + CITES_WEIGHT*cites_norm
    + VELOCITY_WEIGHT*velocity_norm) * R * M_rep over every LLM-scored, on-topic
    item and return the top-n as clean monthly.json entries, highest composite
    first. Missing quantitative terms (paper too new) redistribute their weight
    onto base_quality rather than penalising the item with a 0."""
    scored = [it for it in items
              if it.get("relevance_category") is not None
              and it["relevance_category"] != "off_topic"
              and not is_junk(it.get("title", ""))]
    if not scored:
        return []

    # in-pool normalisations
    nc = _norm_counts([(it.get("cites") or 0) for it in scored], logscale=True)
    na = _norm_counts([(it.get("author_h") or 0) for it in scored], logscale=False)
    if_max = max(config.JOURNAL_IMPACT.values()) or 1.0
    roster = _load_roster()               # for the author-reputation score

    # real citation velocity (cites/age) -- only for items old enough to have one
    ages = [_age_years(it) for it in scored]
    vel_raw = [((it.get("cites") or 0) / a) if a is not None and a >= 1
               and it.get("cites") is not None else None
               for it, a in zip(scored, ages)]
    have_vel = [v for v in vel_raw if v is not None]
    vel_pool = iter(_norm_counts(have_vel, logscale=True)) if have_vel else iter([])
    vnorm_list = [next(vel_pool) if v is not None else None for v in vel_raw]

    out = []
    for it, cnorm, anorm, vnorm in zip(scored, nc, na, vnorm_list):
        label = _label(it)
        in_table = label in config.JOURNAL_IMPACT
        if_raw = config.JOURNAL_IMPACT.get(label, 0.0)
        age = _age_years(it)
        too_young = (age is None or age < 1) and not (it.get("cites") or 0)
        cites_available = it.get("cites") is not None and not too_young

        # --- base_quality: the three anchored rank axes (SUBJECTIVE, LLM) ---
        gen_l = _level(it, "generality") or 0
        contrib_node = it.get("contribution") or {}
        contrib_l = contrib_node.get("level") or 0
        provisional = bool(contrib_node.get("provisional", True))
        if provisional:
            contrib_l = min(contrib_l, 2)          # capped: can't rule out an antecedent
        test_l = _level(it, "testability") or 0
        aw = config.AXIS_WEIGHTS
        base_quality = (aw["generality"] * (gen_l / 3 * 100)
                        + aw["contribution"] * (contrib_l / 3 * 100)
                        + aw["testability"] * (test_l / 3 * 100))

        # --- quantitative blend: subjective quality + real citations + real
        # velocity; a term unavailable for this item redistributes its weight
        # back onto base_quality (never penalised for simply being new)
        eff_w = {"quality": config.QUALITY_WEIGHT}
        terms = {"quality": base_quality}
        if cites_available:
            eff_w["cites"] = config.CITES_WEIGHT
            terms["cites"] = cnorm
        else:
            eff_w["quality"] += config.CITES_WEIGHT
        if vnorm is not None:
            eff_w["velocity"] = config.VELOCITY_WEIGHT
            terms["velocity"] = vnorm
        else:
            eff_w["quality"] += config.VELOCITY_WEIGHT
        blend = sum(eff_w[k] * terms[k] for k in eff_w)

        # --- R: soft robustness discount (abstract-derived, never a bonus) ---
        R = _robustness_discount(it)

        # --- M_rep: bounded reputation multiplier -- author/venue track
        # record can only nudge, never carry (this paper's own citations are
        # already in the weighted blend above, not here). The author component
        # is the strongest-author reputation score (h-index + recent citations
        # + watchlist/canon membership + venue track record).
        a_score = author_score(it, roster)
        M_rep = rep_multiplier(a_score, 100.0 * if_raw / if_max if in_table else None)

        # blend is already <=100 (weights sum to 1.0, each term capped at 100);
        # R only discounts (<=1) but M_rep can BOOST up to 1+CRED_BOUND, so the
        # product can exceed 100 -- clamp, since every consumer (the gauge,
        # "top N" displays) treats composite as an out-of-100 scale
        composite = min(100.0, blend * R * M_rep)

        out.append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "authors": it.get("authors", ""),
            "journal": it.get("journal") or label,
            "date": it.get("date") or it.get("seen", ""),
            "cites": it.get("cites"),
            "author_h": it.get("author_h"),
            "if": round(if_raw, 1),
            "topic": it.get("topic", ""),
            "relevance": _level(it, "relevance"),
            "relevance_category": it.get("relevance_category"),
            "relevance_posterior": it.get("relevance_posterior"),
            "generality": gen_l,
            "contribution": contrib_l,
            "contribution_provisional": provisional,
            "novelty_type": it.get("novelty_type"),
            "novelty_posterior": it.get("novelty_posterior"),
            "antecedent_match": it.get("antecedent_match"),
            "consensus_n": it.get("consensus_n"),
            "consensus_agree": it.get("consensus_agree"),
            "testability": test_l,
            "base_quality": round(base_quality, 1),
            "cites_norm": round(cnorm, 1) if cites_available else None,
            "velocity_norm": round(vnorm, 1) if vnorm is not None else None,
            "robustness": round(R, 3),
            "reputation": round(M_rep, 3),
            "author_score": round(a_score, 1),
            "composite": round(composite, 1),
            "summary": it.get("summary", ""),
        })
    out.sort(key=lambda e: e["composite"], reverse=True)
    return out[:n]
