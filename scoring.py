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
import math
import re

import abstracts
import config
import llm

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
    todo.sort(key=lambda it: (it.get("cites") or 0), reverse=True)
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
        # already in the weighted blend above, not here)
        rep_inputs = []
        if it.get("author_h") is not None:
            rep_inputs.append(anorm)
        if in_table:
            rep_inputs.append(100.0 * if_raw / if_max)
        rep_avg = sum(rep_inputs) / len(rep_inputs) if rep_inputs else 50.0
        M_rep = (1 - config.CRED_BOUND) + 2 * config.CRED_BOUND * (rep_avg / 100)

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
            "composite": round(composite, 1),
            "summary": it.get("summary", ""),
        })
    out.sort(key=lambda e: e["composite"], reverse=True)
    return out[:n]
