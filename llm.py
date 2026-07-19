"""Optional LLM triage layer with provider fallback.

Scores each item on ANCHORED 0-3 rubric levels (never a free-floating 0-100
guess) plus a short evidence-quoting justification -- the LLM extracts a
judgment against an explicit anchor, it never invents a number or computes the
final rank (that's scoring.composite_entries, pure deterministic code):
  relevance    -- topic-fit; a GATE (item must clear >=1), not a ranking weight
  generality   -- does the mechanism travel across assets/strategies/regimes,
                  or is it a narrow one-off
  contribution -- new mechanism vs. meaningful extension vs. incremental vs.
                  re-derivation (capped + `provisional` when antecedent-freeness
                  can't be established from the abstract alone)
  testability  -- sharp/falsifiable/cheap to test with public data, vs. vague
Plus best-effort robustness flags (bool-or-null; null when the abstract simply
doesn't say -- absence of information is never treated as a red flag):
  isolated_backtest_only, no_costs_mentioned, extreme_claimed_sharpe,
  weak_stat_support

Tries providers in order -- Gemini, then Groq, then Mistral -- so if one
provider's key expires or its quota is exhausted, scoring automatically fails
over to the next. Entirely optional: dormant unless at least one provider key
is set, and every failure path degrades to the plain no-LLM feed rather than
breaking the run.

Keys (any/all, free tiers):
  GEMINI_API_KEY (or GOOGLE_API_KEY)  -- https://aistudio.google.com/apikey
  GROQ_API_KEY                        -- https://console.groq.com/keys
  MISTRAL_API_KEY                     -- https://console.mistral.ai/api-keys
"""

import json
import os
import re
import time

import requests

import config

_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "{model}:generateContent")
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"

_SYSTEM = (
    "You are a research analyst EXTRACTING anchored rubric judgments for a "
    "quant practitioner's digest -- you classify against fixed anchors, you do "
    "NOT invent a free-floating score and you do NOT compute any final rank "
    "(that happens downstream, in code). For each item, assign FOUR anchored "
    "0-3 levels, flag any explicitly-stated robustness concerns, name a topic, "
    "and write a one-line summary.\n\nINTERESTS:\n"
    + config.RANK_INTERESTS
    + "\n\nAssign each of these 0-3, with a short justification (<=15 words) "
    "that names the concrete evidence for the level chosen:\n\n"
    "1) relevance -- fit to the interests above.\n"
    "   3 = directly testable finance result in these areas (asset pricing, "
    "factor/anomaly, portfolio construction, quant methods, ML in finance, vol/"
    "derivatives/microstructure, institutional).\n"
    "   2 = finance-adjacent and testable with public data, less central.\n"
    "   1 = tangential (macro narrative, case study, survey with no method).\n"
    "   0 = not finance, or no testable content.\n\n"
    "2) generality -- does the mechanism travel, or is it a one-off.\n"
    "   3 = travels across asset classes/strategies/regimes; a general tool.\n"
    "   2 = broad within one asset class; several use-cases.\n"
    "   1 = works but narrow; one specific setup.\n"
    "   0 = one-off; fragile to its exact sample/universe.\n\n"
    "3) contribution -- novel idea OR credible extension.\n"
    "   3 = a new mechanism with no obvious antecedent, OR a sharp, non-obvious "
    "extension that changes a prior result's sign/magnitude/scope.\n"
    "   2 = meaningful extension of recent work.\n"
    "   1 = incremental tweak on a well-worn method.\n"
    "   0 = re-derivation of a known result under new notation, or a survey.\n"
    "   Set provisional=true whenever the abstract alone doesn't let you rule "
    "out a direct antecedent (you have no citation graph here) -- default to "
    "true unless the abstract itself makes the novelty claim explicit and "
    "checkable.\n\n"
    "4) testability -- how sharp/falsifiable/cheap to test with public data.\n"
    "   3 = sharp, falsifiable, cheap with public data; clean identification.\n"
    "   2 = testable, some ambiguity in construction.\n"
    "   1 = testable in principle; heavy setup or weak identification.\n"
    "   0 = barely operationalizable.\n\n"
    "Be selective -- most items are NOT 3s on every axis; use the full 0-3 "
    "range honestly. Judge only from the title, authors, source, and abstract "
    "given -- if a level can't be judged, use the conservative (lower) level "
    "rather than guessing high.\n\n"
    "Also flag these ONLY when the abstract EXPLICITLY states them -- leave "
    "null (do not guess) when it simply doesn't say; the null case is not a "
    "penalty, it just means unknown:\n"
    "- isolated_backtest_only: abstract states the result is in-sample / no "
    "out-of-sample test.\n"
    "- no_costs_mentioned: abstract states returns are gross/before costs, or "
    "describes a turnover-heavy strategy with no cost-adjustment mentioned.\n"
    "- extreme_claimed_sharpe: abstract states a headline Sharpe ratio > 2.\n"
    "- weak_stat_support: abstract itself hedges the result (e.g. 'preliminary', "
    "'suggestive', only marginal significance stated).\n\n"
    "topic -- exactly one of: " + "; ".join(config.TOPICS) + ".\n\n"
    "Also write a crisp one-sentence summary (<= 30 words) of what the paper "
    "does and why it matters to a quant -- concrete about the method, finding, "
    "or asset class; not vague praise.\n\n"
    "Return ONLY a JSON array, one object per item, no prose:\n"
    '[{"i": <int>, '
    '"relevance": {"level": 0-3, "why": "..."}, '
    '"generality": {"level": 0-3, "why": "..."}, '
    '"contribution": {"level": 0-3, "why": "...", "provisional": bool}, '
    '"testability": {"level": 0-3, "why": "..."}, '
    '"isolated_backtest_only": bool_or_null, "no_costs_mentioned": bool_or_null, '
    '"extreme_claimed_sharpe": bool_or_null, "weak_stat_support": bool_or_null, '
    '"topic": "<topic>", "summary": "<one sentence>"}]'
)


def _prompt(batch: list[dict]) -> str:
    lines = []
    for i, it in enumerate(batch):
        abstract = (it.get("abstract") or "")[:500]
        lines.append(
            f"[{i}] {it.get('title', '')}\n"
            f"    source={it.get('source', '')} | authors={it.get('authors', '')}\n"
            f"    {abstract}")
    return "Items to score:\n\n" + "\n\n".join(lines)


def _level(v, fallback: int = 0) -> int:
    """Clamp to an anchored 0-3 level. Tolerates a stray 0-100 value from a
    model that ignores the anchor instruction by rescaling it down first."""
    try:
        n = int(round(float(v)))
    except Exception:                              # noqa: BLE001
        return fallback
    if n > 3:                                      # looks like a 0-100 stray value
        n = round(n / 100 * 3)
    return max(0, min(3, n))


def _axis(o: dict, key: str, fallback_level: int = 0) -> dict:
    node = o.get(key)
    if isinstance(node, dict):
        out = {"level": _level(node.get("level"), fallback_level),
               "why": str(node.get("why", "")).strip()[:120]}
        if key == "contribution":
            out["provisional"] = bool(node.get("provisional", True))
        return out
    # tolerate a bare number instead of {"level":...,"why":...}
    out = {"level": _level(node, fallback_level), "why": ""}
    if key == "contribution":
        out["provisional"] = True
    return out


def _bool_or_null(v):
    if v is None:
        return None
    if isinstance(v, str) and v.strip().lower() in ("null", "none", "unknown", ""):
        return None
    return bool(v)


def _parse(text: str) -> dict[int, dict]:
    """-> {index: {relevance, generality, contribution, testability
    (each {level, why[, provisional]}), 4 robustness flags (bool|None),
    topic, summary}}. Tolerates the pre-rubric shape (flat 'relevance'/'score'
    as a 0-100 number) by treating it as a single relevance level, degrading
    every other axis to fallback rather than failing the whole item."""
    m = re.search(r"\[.*\]", text, re.S)          # tolerate stray prose around it
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:                              # noqa: BLE001
        return {}
    out: dict[int, dict] = {}
    for o in arr:
        try:
            i = int(o["i"])
            rel = _axis(o, "relevance", fallback_level=0)
            topic = str(o.get("topic") or "").strip()
            if topic not in config.TOPICS:
                topic = "Other"
            out[i] = {
                "relevance": rel,
                "generality": _axis(o, "generality", fallback_level=rel["level"]),
                "contribution": _axis(o, "contribution", fallback_level=rel["level"]),
                "testability": _axis(o, "testability", fallback_level=rel["level"]),
                "isolated_backtest_only": _bool_or_null(o.get("isolated_backtest_only")),
                "no_costs_mentioned": _bool_or_null(o.get("no_costs_mentioned")),
                "extreme_claimed_sharpe": _bool_or_null(o.get("extreme_claimed_sharpe")),
                "weak_stat_support": _bool_or_null(o.get("weak_stat_support")),
                "topic": topic,
                "summary": str(o.get("summary") or o.get("why", "")).strip()[:280],
            }
        except Exception:                          # noqa: BLE001
            continue
    return out


# ------------------------------------------------------ providers
# Each returns a parsed {index: (score, summary)} dict when it ran, or None when
# it is not configured (no key) so the caller falls through to the next.
def _rank_gemini(batch: list[dict], log) -> dict | None:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    body = {
        "system_instruction": {"parts": [{"text": _SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": _prompt(batch)}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8192,
                             "responseMimeType": "application/json"},
    }
    url = _GEMINI_URL.format(model=config.LLM_MODEL)
    for attempt in range(config.LLM_MAX_RETRIES):
        # key in a header, never the URL, so exceptions can't leak it
        r = requests.post(url, headers={"x-goog-api-key": key}, json=body,
                          timeout=90)
        if r.status_code in (429, 500, 503):
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        cands = r.json().get("candidates") or []
        text = "".join(p.get("text", "")
                       for p in (cands[0].get("content") or {}).get("parts", [])
                       ) if cands else ""
        return _parse(text)
    r.raise_for_status()
    return {}


def _rank_groq(batch: list[dict], log) -> dict | None:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    body = {"model": config.GROQ_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": _prompt(batch)}]}
    for attempt in range(config.LLM_MAX_RETRIES + 2):
        r = requests.post(_GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                          json=body, timeout=90)
        if r.status_code in (429, 500, 503):       # rate/token limit -- wait it out
            wait = int(float(r.headers.get("retry-after", 0))) or 15 * (attempt + 1)
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        return _parse(r.json()["choices"][0]["message"]["content"])
    r.raise_for_status()
    return {}


def _rank_mistral(batch: list[dict], log) -> dict | None:
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        return None
    body = {"model": config.MISTRAL_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": _prompt(batch)}]}
    for attempt in range(config.LLM_MAX_RETRIES + 2):
        r = requests.post(_MISTRAL_URL, headers={"Authorization": f"Bearer {key}"},
                          json=body, timeout=90)
        if r.status_code in (429, 500, 503):       # rate limit -- wait it out
            wait = int(float(r.headers.get("retry-after", 0))) or 15 * (attempt + 1)
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        return _parse(r.json()["choices"][0]["message"]["content"])
    r.raise_for_status()
    return {}


_PROVIDERS = [("gemini", _rank_gemini), ("groq", _rank_groq),
              ("mistral", _rank_mistral)]


def have_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                or os.environ.get("GROQ_API_KEY")
                or os.environ.get("MISTRAL_API_KEY"))


def rank(items: list[dict], log, max_batches: int | None = None) -> list[dict]:
    """Attach the anchored rubric levels to each item in place; return the same
    list. Each scored item gets:
      relevance/generality/contribution/testability -- {level (0-3), why[, provisional]}
      isolated_backtest_only/no_costs_mentioned/extreme_claimed_sharpe/
        weak_stat_support -- bool or None (None = abstract didn't say, not a red flag)
      topic, summary
      rank_score -- relevance rescaled to 0-100 (level/3*100), kept for
        backward-compatible continuous consumers (e.g. Recent's ranking field)
    Unscored items get none of these, so callers can tell them apart via
    `it.get("relevance") is not None`.

    max_batches caps how many LLM batches are spent this call (the backfill's
    per-run budget); items past the budget are left unscored for a later run.

    max_batches caps how many LLM batches are spent this call (the backfill's
    per-run budget); items past the budget are left unscored for a later run.
    Items are processed in the order given -- order the most promising first."""
    if not items:
        return items
    if not have_key():
        log("[llm] no provider key set; skipping ranking (plain no-LLM feed)")
        return items

    b = config.LLM_RANK_BATCH
    ranked, used, dead, batches = 0, set(), set(), 0
    for start in range(0, len(items), b):
        if max_batches is not None and batches >= max_batches:
            log(f"[llm] batch budget {max_batches} reached; "
                f"{len(items) - start} items left unscored")
            break
        batch = items[start:start + b]
        scores = None
        for name, fn in _PROVIDERS:
            if name in dead:
                continue
            try:
                res = fn(batch, log)
            except Exception as e:                 # noqa: BLE001
                log(f"[llm] {name} failed on batch {start // b} "
                    f"({type(e).__name__}); failing over")
                dead.add(name)                     # stop retrying it this run
                continue
            if res is None:                        # provider not configured
                continue
            scores, _ = res, used.add(name)
            break
        if not scores:
            if len(dead) == len([n for n, _ in _PROVIDERS]):
                log("[llm] all providers exhausted; stopping")
                break
            continue
        batches += 1
        for i, it in enumerate(batch):
            if i in scores:
                s = scores[i]
                it["relevance"] = s["relevance"]
                it["generality"] = s["generality"]
                it["contribution"] = s["contribution"]
                it["testability"] = s["testability"]
                it["isolated_backtest_only"] = s["isolated_backtest_only"]
                it["no_costs_mentioned"] = s["no_costs_mentioned"]
                it["extreme_claimed_sharpe"] = s["extreme_claimed_sharpe"]
                it["weak_stat_support"] = s["weak_stat_support"]
                it["topic"] = s["topic"]
                it["summary"] = s["summary"]
                it["rank_score"] = round(s["relevance"]["level"] / 3 * 100)
                ranked += 1
        time.sleep(config.LLM_BATCH_PAUSE)         # stay under free-tier RPM
    log(f"[llm] ranked {ranked}/{len(items)} via {', '.join(sorted(used)) or 'none'}")
    return items
