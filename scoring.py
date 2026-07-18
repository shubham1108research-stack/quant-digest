"""Monthly 5-parameter composite scoring, shared by the present-month recompute
and the backward backfill.

Sub-scores (each 0-100), weights in config.MONTHLY_WEIGHTS:
  velocity     -- real cites-per-year (S2 cites / age) when the paper is >=1
                  year old, min-max log-normalised in-pool; else the LLM's
                  expected-citation-velocity estimate
  downloads    -- the LLM's expected download/attention estimate
  paper_cites  -- S2 citationCount, log min-max in-pool
  author_cites -- S2 max author h-index, min-max in-pool
  journal_if   -- config.JOURNAL_IMPACT / table max
If a sub-score is missing for an item its weight is redistributed to
author_cites first, then journal_if (per user rule); if both are missing the
remaining weights are renormalised. The LLM's innovation/relevance/topic are
still attached (email tiers, Recent's top-10% filter, seminal promotion, and
the Archive tab's topics) -- they're just not composite terms.

Flow: attach_s2() fills abstract/cites/author_h/year; llm_score() attaches
relevance/innovation/velocity_est/downloads_est/topic/summary (respecting a
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
    """LLM-score only the not-yet-scored, non-junk items (innovation/relevance/
    summary/topic), most-cited first so a batch budget spends on the strongest
    candidates. Mutates in place; leaves items past the budget unscored for a
    later run. Junk records (editorial front matter, a blog's own link-roundup
    post) are skipped entirely -- never worth spending LLM quota on."""
    todo = [it for it in items
            if it.get("innovation") is None and not is_junk(it.get("title", ""))]
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


def composite_entries(items: list[dict], n: int) -> list[dict]:
    """Compute the composite over every LLM-scored item and return the top-n as
    clean monthly.json entries, highest composite first. Missing sub-scores
    redistribute their weight to author_cites, then journal_if."""
    scored = [it for it in items
              if it.get("innovation") is not None and it.get("rank_score") is not None
              and not is_junk(it.get("title", ""))]
    if not scored:
        return []
    # in-pool normalisations
    nc = _norm_counts([(it.get("cites") or 0) for it in scored], logscale=True)
    na = _norm_counts([(it.get("author_h") or 0) for it in scored], logscale=False)
    # real citation velocity (cites/age) where the paper is old enough to have one
    vel_real_raw = [((it.get("cites") or 0) / a) if (a := _age_years(it)) and a >= 1
                    and it.get("cites") is not None else None for it in scored]
    have_vel = [v for v in vel_real_raw if v is not None]
    vel_norm_pool = _norm_counts(have_vel, logscale=True) if have_vel else []
    vel_iter = iter(vel_norm_pool)
    vel_real = [next(vel_iter) if v is not None else None for v in vel_real_raw]

    if_max = max(config.JOURNAL_IMPACT.values()) or 1.0
    base_w = config.MONTHLY_WEIGHTS
    out = []
    for it, cnorm, anorm, vreal in zip(scored, nc, na, vel_real):
        label = _label(it)
        in_table = label in config.JOURNAL_IMPACT
        if_raw = config.JOURNAL_IMPACT.get(label, 0.0)
        # sub-scores; None = unavailable for this item. A paper under a year old
        # with zero citations is too young to judge on citations -- treat
        # paper_cites as unavailable so its weight shifts to author/journal
        # (daily-feed papers naturally score on author h-index + journal IF).
        age = _age_years(it)
        too_young = (age is None or age < 1) and not (it.get("cites") or 0)
        subs = {
            "velocity": (vreal if vreal is not None
                         else (float(it["velocity_est"])
                               if it.get("velocity_est") is not None else None)),
            "downloads": (float(it["downloads_est"])
                          if it.get("downloads_est") is not None else None),
            "paper_cites": (cnorm if it.get("cites") is not None and not too_young
                            else None),
            "author_cites": anorm if it.get("author_h") is not None else None,
            "journal_if": 100.0 * if_raw / if_max if in_table else None,
        }
        # weight redistribution: missing -> author_cites, then journal_if
        w = dict(base_w)
        missing = sum(w.pop(k) for k in [k for k, v in subs.items() if v is None])
        if missing:
            if subs["author_cites"] is not None:
                w["author_cites"] = w.get("author_cites", 0) + missing
            elif subs["journal_if"] is not None:
                w["journal_if"] = w.get("journal_if", 0) + missing
            elif w:                                # renormalise what's left
                tot = sum(w.values())
                w = {k: v / tot for k, v in w.items()} if tot else w
        comp = sum(w[k] * subs[k] for k in w if subs[k] is not None)
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
            "innovation": round(it["innovation"]),
            "relevance": round(it["rank_score"]),
            "velocity": None if subs["velocity"] is None else round(subs["velocity"]),
            "downloads": None if subs["downloads"] is None else round(subs["downloads"]),
            "paper_cites": None if subs["paper_cites"] is None else round(subs["paper_cites"]),
            "author_cites": None if subs["author_cites"] is None else round(subs["author_cites"]),
            "journal_if": None if subs["journal_if"] is None else round(subs["journal_if"]),
            "composite": round(comp, 1),
            "summary": it.get("summary", ""),
        })
    out.sort(key=lambda e: e["composite"], reverse=True)
    return out[:n]
