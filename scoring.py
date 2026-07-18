"""Monthly 5-parameter composite scoring, shared by the present-month recompute
and the backward backfill.

Sub-scores (each 0-100): innovation + relevance from the LLM (llm.py); paper
citations + author h-index from Semantic Scholar (abstracts.py); journal impact
factor from config.JOURNAL_IMPACT. The count-based two are min-max normalised
WITHIN the scored pool, so a lightly-cited but important recent paper still
ranks. composite = sum(weight * sub-score), weights in config.MONTHLY_WEIGHTS.

Flow: attach_s2() fills abstract/cites/author_h; llm_score() attaches
innovation/relevance/summary (respecting a per-run batch budget); then
composite_entries() ranks and returns the month's top-N portal entries.
"""

import math

import abstracts
import config
import llm


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
    return items


def llm_score(items: list[dict], log, max_batches: int | None = None) -> list[dict]:
    """LLM-score only the not-yet-scored items (innovation/relevance/summary),
    most-cited first so a batch budget spends on the strongest candidates.
    Mutates in place; leaves items past the budget unscored for a later run."""
    todo = [it for it in items if it.get("innovation") is None]
    todo.sort(key=lambda it: (it.get("cites") or 0), reverse=True)
    llm.rank(todo, log, max_batches=max_batches)
    return items


def score_new(items: list[dict], log, max_batches: int | None = None) -> list[dict]:
    """Full pass for freshly-fetched items: S2 enrich then LLM score."""
    attach_s2(items, log)
    llm_score(items, log, max_batches=max_batches)
    return items


def composite_entries(items: list[dict], n: int) -> list[dict]:
    """Compute the composite over every scored item (has innovation + relevance)
    and return the top-n as clean monthly.json entries, highest composite first."""
    scored = [it for it in items
              if it.get("innovation") is not None and it.get("rank_score") is not None]
    if not scored:
        return []
    nc = _norm_counts([(it.get("cites") or 0) for it in scored], logscale=True)
    na = _norm_counts([(it.get("author_h") or 0) for it in scored], logscale=False)
    if_max = max(config.JOURNAL_IMPACT.values()) or 1.0
    w = config.MONTHLY_WEIGHTS
    out = []
    for it, cnorm, anorm in zip(scored, nc, na):
        if_raw = config.JOURNAL_IMPACT.get(_label(it), 0.0)
        subs = {
            "innovation": float(it["innovation"]),
            "relevance": float(it["rank_score"]),
            "paper_cites": cnorm,
            "author_cites": anorm,
            "journal_if": 100.0 * if_raw / if_max,
        }
        comp = sum(w[k] * subs[k] for k in w)
        out.append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "authors": it.get("authors", ""),
            "journal": it.get("journal") or _label(it),
            "date": it.get("date") or it.get("seen", ""),
            "cites": it.get("cites"),
            "author_h": it.get("author_h"),
            "if": round(if_raw, 1),
            "innovation": round(subs["innovation"]),
            "relevance": round(subs["relevance"]),
            "paper_cites": round(cnorm),
            "author_cites": round(anorm),
            "journal_if": round(subs["journal_if"]),
            "composite": round(comp, 1),
            "summary": it.get("summary", ""),
        })
    out.sort(key=lambda e: e["composite"], reverse=True)
    return out[:n]
