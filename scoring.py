"""Monthly composite scoring, shared by the present-month recompute and the
backward backfill.

composite = base_quality * R * M_cred -- all deterministic, code-only (the LLM
only ever supplies anchored 0-3 rubric levels + a short justification; see
llm.py's module docstring for the "extract, never invent the rank" design):

  base_quality -- weighted avg of the LLM's anchored generality/contribution/
                  testability levels (0-3, rescaled 0-100). contribution is
                  capped at level 2 when the LLM marked it `provisional`
                  (abstract alone can't rule out a direct antecedent).
  R            -- soft robustness DISCOUNT (never a bonus; floored at
                  config.ROBUSTNESS_FLOOR): multiplies in
                  config.ROBUSTNESS_DISCOUNTS[flag] for each robustness flag the
                  LLM found EXPLICITLY stated in the abstract. A null flag
                  (abstract simply didn't say) is never penalised -- absence of
                  information isn't evidence of a problem.
  M_cred       -- bounded credibility multiplier in [1-CRED_BOUND, 1+CRED_BOUND]
                  (i.e. [0.85, 1.15]) from whichever of {S2 paper citations, S2
                  author h-index, JOURNAL_IMPACT} are available for that item --
                  prestige/traction can only NUDGE the ranking, never carry a
                  paper that's weak on base_quality.

Gate: an item needs relevance level >= 1 (not off-topic/no testable content) to
be composite-eligible at all.

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
    """Compute composite = base_quality * R * M_cred over every LLM-scored,
    on-topic item and return the top-n as clean monthly.json entries, highest
    composite first."""
    scored = [it for it in items
              if _level(it, "relevance") is not None
              and _level(it, "relevance") >= 1
              and not is_junk(it.get("title", ""))]
    if not scored:
        return []

    # in-pool normalisations for the credibility inputs
    nc = _norm_counts([(it.get("cites") or 0) for it in scored], logscale=True)
    na = _norm_counts([(it.get("author_h") or 0) for it in scored], logscale=False)
    if_max = max(config.JOURNAL_IMPACT.values()) or 1.0

    out = []
    for it, cnorm, anorm in zip(scored, nc, na):
        label = _label(it)
        in_table = label in config.JOURNAL_IMPACT
        if_raw = config.JOURNAL_IMPACT.get(label, 0.0)
        age = _age_years(it)
        too_young = (age is None or age < 1) and not (it.get("cites") or 0)

        # --- base_quality: the three anchored rank axes (never prestige) ---
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

        # --- R: soft robustness discount (abstract-derived, never a bonus) ---
        R = _robustness_discount(it)

        # --- M_cred: bounded credibility multiplier -- prestige nudges only,
        # never carries a paper weak on base_quality
        cred_inputs = []
        if it.get("cites") is not None and not too_young:
            cred_inputs.append(cnorm)
        if it.get("author_h") is not None:
            cred_inputs.append(anorm)
        if in_table:
            cred_inputs.append(100.0 * if_raw / if_max)
        cred_avg = sum(cred_inputs) / len(cred_inputs) if cred_inputs else 50.0
        M_cred = (1 - config.CRED_BOUND) + 2 * config.CRED_BOUND * (cred_avg / 100)

        composite = base_quality * R * M_cred

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
            "generality": gen_l,
            "contribution": contrib_l,
            "contribution_provisional": provisional,
            "testability": test_l,
            "base_quality": round(base_quality, 1),
            "robustness": round(R, 3),
            "credibility": round(M_cred, 3),
            "composite": round(composite, 1),
            "summary": it.get("summary", ""),
        })
    out.sort(key=lambda e: e["composite"], reverse=True)
    return out[:n]
