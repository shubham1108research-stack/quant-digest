"""Optional LLM triage layer with provider fallback.

Scores each item on ANCHORED 0-3 rubric levels (never a free-floating 0-100
guess) plus a short evidence-quoting justification -- the LLM extracts a
judgment against an explicit anchor, it never invents a number or computes the
final rank (that's scoring.composite_entries, pure deterministic code):
  relevance    -- topic-fit, 0-3 graded judgment (kept for its "why" and as an
                  axis fallback level; NOT what drives the displayed rating or
                  the Recent/Monthly gate anymore -- see relevance_category)
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

Two passes: (1) rank() TRIAGES every item with the first provider in the chain
that responds (OpenRouter/DeepSeek -> Mistral -> Groq -> OpenAI). DeepSeek leads
because a blind bake-off found every candidate refused the fabrication trap
correctly, which left cost and concision to decide; the free tiers behind it
rescue anything it drops. (2) consensus() re-scores just the promising SHORTLIST
with the voting providers together and combines their votes, flagging
disagreement as provisional. Entirely optional: dormant unless a provider key
is set, and every failure path degrades to the plain no-LLM feed rather than
breaking the run.

Keys (any/all, free tiers):
  GEMINI_API_KEY (or GOOGLE_API_KEY)  -- https://aistudio.google.com/apikey
  GROQ_API_KEY                        -- https://console.groq.com/keys
  MISTRAL_API_KEY                     -- https://console.mistral.ai/api-keys
  OPENROUTER_API_KEY                  -- https://openrouter.ai/keys
  OPENAI_API_KEY                      -- https://platform.openai.com/api-keys
"""

import json
import os
import re
import time
from collections import Counter

import requests

import canon
import config

_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "{model}:generateContent")
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def _known_frameworks_block() -> str:
    """A compact reference of already-established measurement/modeling
    frameworks, drawn straight from the curated seminal canon's Method-type
    entries (canon.py) -- the literal "cross-examine against history"
    mechanism: it grows automatically as the canon grows, no separate list to
    maintain. Used to stop the contribution rubric from mistaking "known
    framework applied to a new dataset/asset/label" for a new mechanism."""
    methods = [(title, why) for papers in canon.CANON.values()
               for (title, _author, _year, typ, why) in papers if typ == "Method"]
    lines = "\n".join(f"- {title}: {why}" for title, why in methods)
    return (
        "KNOWN, ALREADY-ESTABLISHED FRAMEWORKS (for calibration only -- this "
        "list is illustrative, not exhaustive; the same caution applies to any "
        "well-established framework/measurement-technique even if it isn't "
        "listed here):\n" + lines
    )

def _sleeve_block() -> str:
    """Desk-sleeve anchors, generated from config.SLEEVES so the taxonomy has
    one home. MULTI-LABEL by design: sleeves overlap, and every single-label
    attempt failed by forcing a paper to lose one true tag to win another."""
    lines = "\n".join(f"  {k} -- {v}" for k, v in config.SLEEVES.items())
    return (
        "DESK SLEEVES -- which parts of a systematic macro / CTA book this "
        "paper touches. These OVERLAP: tag EVERY sleeve that genuinely "
        "applies, not just the closest one. A paper on bond convenience yields "
        "is both 'carry' and 'rates_credit'; one on currency exposure under "
        "interest-parity deviations is both 'carry' and 'fx'. Typically 1-3 "
        f"tags, at most {config.SLEEVES_MAX}. Use 'other' alone only when none "
        "genuinely apply.\n" + lines +
        "\n\ndesk_fit -- usability on a systematic macro/CTA desk, judged "
        "SEPARATELY from research quality (a first-rate microstructure paper "
        "is desk_fit 1):\n" + config.DESK_FIT_ANCHORS
    )


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
    "relevance_category -- a SEPARATE, coarser verdict used for calibration "
    "(independent of the relevance level above, judged fresh):\n"
    "   'core_fit' = squarely the kind of result this digest exists for -- a "
    "testable quant-finance result in the interest areas above.\n"
    "   'off_topic' = not finance, or no testable content, or purely a macro/"
    "policy narrative with no quant-finance application.\n"
    "   'adjacent' = everything in between -- finance-relevant or uses a "
    "relevant technique, but not squarely core (e.g. a macro-policy estimation "
    "problem that happens to use the same toolkit a quant would).\n\n"
    "2) generality -- does the mechanism travel, or is it a one-off.\n"
    "   3 = travels across asset classes/strategies/regimes; a general tool.\n"
    "   2 = broad within one asset class; several use-cases.\n"
    "   1 = works but narrow; one specific setup.\n"
    "   0 = one-off; fragile to its exact sample/universe.\n\n"
    "3) contribution -- how novel the paper's HEADLINE CONTRIBUTION is. Novelty "
    "comes in THREE distinct kinds, and a paper needs only ONE to be genuinely "
    "novel -- always credit the STRONGEST kind; a familiar element on the other "
    "axes never caps it:\n"
    "     THEORY -- a new economic mechanism/explanation (e.g. WHY a factor is "
    "priced: a new risk story, a new ICAPM state variable).\n"
    "     METHOD -- a new estimator/test/model (e.g. a new way to estimate "
    "time-varying factor loadings, a new multiple-testing correction).\n"
    "     EMPIRICAL -- a new fact/factor/signal/regularity (e.g. a NEW priced "
    "factor that adds explanatory power beyond the existing set).\n"
    "   Report novelty_type = which kind carries the contribution ('theory' | "
    "'method' | 'empirical', or 'none' if nothing is new), and score the level "
    "of that strongest kind:\n"
    "   3 = a genuinely new theory, method, OR empirical finding with no "
    "obvious antecedent, OR a sharp, non-obvious extension that changes a prior "
    "result's sign/magnitude/scope.\n"
    "   2 = meaningful extension of recent work.\n"
    "   1 = incremental tweak on a well-worn approach.\n"
    "   0 = re-derivation of a known result under new notation, or a survey "
    "(novelty_type='none').\n"
    "   Extending an established model (e.g. adding a factor to Fama-French) CAN "
    "be a 3 -- if the ADDED factor (empirical), the estimator (method), or the "
    "explanation (theory) is itself genuinely new. The FF regression it's tested "
    "in is the yardstick, not the contribution.\n"
    "   CRITICAL -- judge ONLY the paper's HEADLINE CONTRIBUTION (the new thing "
    "it claims to add), in ANY topic/domain. If that claimed contribution is "
    "ITSELF an already-established framework/measurement technique (see the "
    "reference list below, or any other well-known one) merely re-pointed at a "
    "new dataset/asset/sample/label, that is an APPLICATION, not a new mechanism "
    "-- cap it at 1-2 regardless of framing ('novel dataset', 'first study of "
    "X', 'introduces a new index for Y' when Y is just a new underlying for an "
    "old construction method, e.g. a VIX-style index built for a new asset).\n"
    "   BUT -- using an established framework as a TEST, BENCHMARK, or "
    "ESTIMATION TOOL is normal, rigorous practice and does NOT lower "
    "contribution. A paper proposing a NEW factor/signal/model that evaluates "
    "it with Fama-French 3/5-factor, Fama-MacBeth regressions, GMM, "
    "Newey-West, a GARCH baseline, etc. is using those as the yardstick, not as "
    "its contribution -- score the novelty of the NEW thing being tested. "
    "Beating or surviving an established benchmark (e.g. a new factor with alpha "
    "after Fama-French) is evidence FOR a real contribution, never against it. "
    "Only the MECHANISM's novelty matters, never the application/dataset/label "
    "alone -- and never penalise a paper for the standard tests it runs.\n"
    "   Set provisional=true whenever the abstract alone doesn't let you rule "
    "out a direct antecedent (you have no citation graph here) -- default to "
    "true unless the abstract itself makes the novelty claim explicit and "
    "checkable.\n\n"
    + _known_frameworks_block() + "\n\n"
    "4) testability -- how sharp/falsifiable/cheap to test with public data.\n"
    "   3 = sharp, falsifiable, cheap with public data; clean identification.\n"
    "   2 = testable, some ambiguity in construction.\n"
    "   1 = testable in principle; heavy setup or weak identification.\n"
    "   0 = barely operationalizable.\n\n"
    "Be selective -- most items are NOT 3s on every axis; use the full 0-3 "
    "range honestly. Judge only from the title, authors, source, and abstract "
    "given -- if a level can't be judged, use the conservative (lower) level "
    "rather than guessing high.\n\n"
    "antecedent_match -- classify ONLY the paper's HEADLINE CONTRIBUTION (the "
    "new thing it claims to add) against the known framework list above (and "
    "any other well-established framework/technique you recognise), "
    "independently of the contribution level:\n"
    "   'matches_known' = the claimed contribution ITSELF is an established "
    "framework/measurement technique merely re-pointed at a new dataset/asset/"
    "label/market (mechanism is old, only the application is new -- e.g. a "
    "VIX-style index built for a new underlying and called novel).\n"
    "   'no_antecedent' = a genuinely new mechanism, factor, signal, or "
    "measurement approach with no identifiable established antecedent.\n"
    "   'ambiguous' = partial resemblance, or the abstract doesn't let you tell.\n"
    "   Judge this against the contribution's OWN kind (novelty_type): for an "
    "EMPIRICAL contribution, 'matches_known' means the factor/signal ITSELF is a "
    "known one relabeled (NOT that it was tested with a known model); for "
    "METHOD, that the estimator/test is a known one; for THEORY, that the "
    "mechanism is a restatement.\n"
    "   DO NOT mark 'matches_known' just because the paper USES an established "
    "framework to TEST/BENCHMARK/ESTIMATE (Fama-French, Fama-MacBeth, GMM, "
    "Newey-West, GARCH baselines, etc.) -- that is standard practice, orthogonal "
    "to novelty. Judge only whether the CONTRIBUTION is a repackaged old one, "
    "not which tools it is tested with.\n"
    "   When unsure, use 'ambiguous' (the neutral, non-committal verdict).\n\n"
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
    + "\n\n" + _sleeve_block() + "\n\n"
    "Return ONLY a JSON array, one object per item, no prose:\n"
    '[{"i": <int>, '
    '"relevance": {"level": 0-3, "why": "..."}, '
    '"relevance_category": "core_fit|adjacent|off_topic", '
    '"generality": {"level": 0-3, "why": "..."}, '
    '"contribution": {"level": 0-3, "why": "...", "provisional": bool}, '
    '"novelty_type": "theory|method|empirical|none", '
    '"testability": {"level": 0-3, "why": "..."}, '
    '"antecedent_match": "matches_known|ambiguous|no_antecedent", '
    '"isolated_backtest_only": bool_or_null, "no_costs_mentioned": bool_or_null, '
    '"extreme_claimed_sharpe": bool_or_null, "weak_stat_support": bool_or_null, '
    '"sleeves": ["<sleeve key>", ...], '
    '"desk_fit": {"level": 0-3, "why": "..."}, '
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
            match = str(o.get("antecedent_match") or "").strip().lower()
            if match not in config.NOVELTY_LR:
                match = "ambiguous"                    # neutral default
            rel_cat = str(o.get("relevance_category") or "").strip().lower()
            if rel_cat not in config.RELEVANCE_LR:
                rel_cat = "adjacent"                   # neutral default
            ntype = str(o.get("novelty_type") or "").strip().lower()
            if ntype not in ("theory", "method", "empirical", "none"):
                ntype = "none"
            raw = o.get("sleeves") or []
            if isinstance(raw, str):           # tolerate a bare string
                raw = [raw]
            sleeves, seen_s = [], set()
            for k in raw:
                k = str(k).strip().lower()
                if k in config.SLEEVES and k not in seen_s:
                    seen_s.add(k)
                    sleeves.append(k)
            sleeves = [k for k in sleeves if k != "other"][:config.SLEEVES_MAX] \
                or ["other"]                   # 'other' only when alone
            out[i] = {
                "relevance": rel,
                "relevance_category": rel_cat,
                "generality": _axis(o, "generality", fallback_level=rel["level"]),
                "contribution": _axis(o, "contribution", fallback_level=rel["level"]),
                "novelty_type": ntype,
                "testability": _axis(o, "testability", fallback_level=rel["level"]),
                "antecedent_match": match,
                "isolated_backtest_only": _bool_or_null(o.get("isolated_backtest_only")),
                "no_costs_mentioned": _bool_or_null(o.get("no_costs_mentioned")),
                "extreme_claimed_sharpe": _bool_or_null(o.get("extreme_claimed_sharpe")),
                "weak_stat_support": _bool_or_null(o.get("weak_stat_support")),
                "sleeves": sleeves,
                "desk_fit": _axis(o, "desk_fit", fallback_level=0),
                "topic": topic,
                "summary": str(o.get("summary") or o.get("why", "")).strip()[:280],
            }
        except Exception:                          # noqa: BLE001
            continue
    return out


def novelty_posterior(topic: str, antecedent_match: str) -> float:
    """Bayesian posterior P(seminal-caliber | topic, antecedent verdict):
    combine the topic prior (how often work in this area proves seminal, from
    the canon) with the LLM's independent antecedent classification as a
    likelihood ratio. Pure arithmetic -- the LLM never sees this number."""
    p = config.NOVELTY_PRIOR.get(topic, config.NOVELTY_PRIOR_FALLBACK)
    p = min(max(p, 1e-6), 1 - 1e-6)                # keep odds finite
    lr = config.NOVELTY_LR.get(antecedent_match, 1.0)
    post_odds = (p / (1 - p)) * lr
    return post_odds / (1 + post_odds)


def relevance_posterior(topic: str, relevance_category: str) -> float:
    """Bayesian posterior P(core-fit | topic, relevance_category verdict):
    same shape as novelty_posterior, but the prior blends the topic's canon
    density AND its historical archive core-fit rate (config.RELEVANCE_PRIOR),
    updated by the LLM's independent relevance_category as a likelihood ratio.
    This IS rank_score (rescaled to 0-100) and the Recent/Monthly gate -- it
    replaces the old flat relevance.level/3*100 rescale, which only ever took
    4 values and conflated a gate with a ranking signal."""
    p = config.RELEVANCE_PRIOR.get(topic, config.RELEVANCE_PRIOR_FALLBACK)
    p = min(max(p, 1e-6), 1 - 1e-6)
    lr = config.RELEVANCE_LR.get(relevance_category, 1.0)
    post_odds = (p / (1 - p)) * lr
    return post_odds / (1 + post_odds)


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


def _groq_call(sub: list[dict], key: str) -> dict:
    """One Groq request for a small sub-batch -> {localidx: parsed}."""
    body = {"model": config.GROQ_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": _prompt(sub)}]}
    for attempt in range(config.LLM_MAX_RETRIES + 2):
        r = requests.post(_GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                          json=body, timeout=90)
        # 413 here is NOT a payload bug -- it's Groq's tokens-per-MINUTE cap
        # ("Request too large ... on tokens per minute"). It won't clear within
        # a run when the free tier is drained, so DON'T retry it: give up on the
        # chunk immediately and let rank()'s cross-provider failover rescue those
        # items via Mistral. (Retrying 413 with backoff x every chunk x every
        # batch is what wedged a run for 6h; fail-fast is the fix.)
        if r.status_code == 413:
            return {}
        if r.status_code in (429, 500, 503):
            wait = int(float(r.headers.get("retry-after", 0))) or 6 * (attempt + 1)
            time.sleep(min(wait, 20))
            continue
        r.raise_for_status()
        return _parse(r.json()["choices"][0]["message"]["content"])
    r.raise_for_status()
    return {}


def _rank_groq(batch: list[dict], log) -> dict | None:
    # Groq's free tier has a low per-request token cap (413 on ~25 items) and a
    # tight per-minute budget, so we sub-chunk to config.GROQ_BATCH and merge.
    # A chunk that fails (429/413) is skipped, not fatal -- Groq contributes
    # whatever chunks succeed, and consensus tolerates a partial vote.
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    out: dict[int, dict] = {}
    chunk = max(1, config.GROQ_BATCH)
    for start in range(0, len(batch), chunk):
        try:
            for k, v in _groq_call(batch[start:start + chunk], key).items():
                out[start + k] = v
        except Exception as e:                     # noqa: BLE001
            log(f"[groq] chunk {start // chunk} skipped ({_err(e)})")
        if start + chunk < len(batch):
            time.sleep(2)                          # ease the per-minute budget
    return out or None


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


def _rank_openrouter(batch: list[dict], log) -> dict | None:
    # OpenRouter is OpenAI-compatible; one key fronts many models (incl. free
    # tiers). The optional Referer/Title headers are just attribution, ignored
    # by scoring. Last in the chain -- resilience when the others are exhausted.
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    body = {"model": config.OPENROUTER_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": _prompt(batch)}]}
    headers = {"Authorization": f"Bearer {key}",
               "HTTP-Referer": "https://quant-digest-e62.pages.dev",
               "X-Title": "quant-digest"}
    for attempt in range(config.LLM_MAX_RETRIES + 2):
        r = requests.post(_OPENROUTER_URL, headers=headers, json=body, timeout=90)
        if r.status_code in (429, 500, 503):       # rate/token limit -- wait it out
            wait = int(float(r.headers.get("retry-after", 0))) or 15 * (attempt + 1)
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        return _parse(r.json()["choices"][0]["message"]["content"])
    r.raise_for_status()
    return {}


def _rank_openai(batch: list[dict], log) -> dict | None:
    # OpenAI (paid) -- reliable, high-quality vote. Last in the chain so free
    # providers carry the bulk triage; consensus (shortlist only) always
    # includes it. No `temperature` field: the gpt-5 family rejects any value
    # other than the default on chat/completions.
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    body = {"model": config.OPENAI_MODEL,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": _prompt(batch)}]}
    for attempt in range(config.LLM_MAX_RETRIES + 2):
        r = requests.post(_OPENAI_URL, headers={"Authorization": f"Bearer {key}"},
                          json=body, timeout=120)
        if r.status_code in (429, 500, 503):
            wait = int(float(r.headers.get("retry-after", 0))) or 15 * (attempt + 1)
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        return _parse(r.json()["choices"][0]["message"]["content"])
    r.raise_for_status()
    return {}


# Gemini disabled for now (the key's service account is deleted -> 401 every
# batch). _rank_gemini is kept above; re-add ("gemini", _rank_gemini) here with
# a valid AIza key to re-enable.
# OpenRouter (fronting DeepSeek) leads: it is the chosen primary, and at
# DeepSeek prices the whole bulk pass costs cents. Mistral and Groq follow as
# free-tier rescue for anything it drops; OpenAI trails and is currently dead
# (no credits) but costs nothing to leave in the chain.
_PROVIDERS = [("openrouter", _rank_openrouter),
              ("mistral", _rank_mistral), ("groq", _rank_groq),
              ("openai", _rank_openai)]
# Free tiers only -- used for the consensus multi-vote so the PAID provider
# (OpenAI) isn't billed on every shortlist item. OpenAI stays in _PROVIDERS as
# the last-resort TRIAGE rescuer (free-first, so you only pay for the few items
# Groq/Mistral/OpenRouter couldn't score) -- the cheapest reliable setup.
# Providers cheap enough to vote on EVERY shortlist item. Not the same as
# "free" any more -- OpenRouter/DeepSeek is paid, but a consensus round over the
# ~250-item shortlist runs to a few cents, so excluding it would cost accuracy
# to save nothing. OpenAI stays out: it is the one provider priced high enough
# for per-item voting to matter.
_VOTE_PROVIDERS = [p for p in _PROVIDERS if p[0] != "openai"]


def have_key() -> bool:
    return bool(os.environ.get("GROQ_API_KEY")
                or os.environ.get("MISTRAL_API_KEY")
                or os.environ.get("OPENROUTER_API_KEY")
                or os.environ.get("OPENAI_API_KEY"))


def _n_configured() -> int:
    """How many distinct providers have a key set -- consensus needs >= 2 to be
    worth the extra calls (a single vote is just a re-score of the triage)."""
    return sum(bool(k) for k in (
        os.environ.get("GROQ_API_KEY"),
        os.environ.get("MISTRAL_API_KEY"),
        os.environ.get("OPENROUTER_API_KEY"),
        os.environ.get("OPENAI_API_KEY")))


def _n_vote_configured() -> int:
    """Providers eligible to vote in consensus with a key set -- the >=2 guard
    counts these, since a single vote is just a re-score of the triage."""
    return sum(bool(k) for k in (
        os.environ.get("GROQ_API_KEY"),
        os.environ.get("MISTRAL_API_KEY"),
        os.environ.get("OPENROUTER_API_KEY")))


def _err(e) -> str:
    """Compact provider-error string incl. HTTP status + body snippet (which
    reveals e.g. a decommissioned Groq model id or a Gemini quota message);
    credentials in the body are scrubbed by main.log's redaction regex."""
    resp = getattr(e, "response", None)
    body = (getattr(resp, "text", "") or "")[:160].replace("\n", " ")
    code = getattr(resp, "status_code", "")
    return f"{type(e).__name__} {code}: {body}".strip() if body else type(e).__name__


_run_deadline: float | None = None      # single wall-clock cap across ALL passes


def start_run_budget(seconds: float | None = None) -> None:
    """Stamp ONE wall-clock deadline shared by every LLM pass this run -- the
    main triage + consensus AND the monthly re-scores. Per-call budgets don't
    compose (four stacked passes overran the CI job timeout by seconds); this
    global cap keeps total LLM time bounded no matter how many passes run.
    Call once before the first llm.rank(). seconds=None uses LLM_RUN_BUDGET_S;
    0/falsy disables (local runs)."""
    global _run_deadline
    s = seconds if seconds is not None else getattr(config, "LLM_RUN_BUDGET_S", 0)
    _run_deadline = (time.monotonic() + s) if s else None


def _run_budget_left() -> bool:
    return _run_deadline is None or time.monotonic() < _run_deadline


def rank(items: list[dict], log, max_batches: int | None = None) -> list[dict]:
    """Attach the anchored rubric levels to each item in place; return the same
    list. Each scored item gets:
      relevance/generality/contribution/testability -- {level (0-3), why[, provisional]}
      relevance_category -- independent core_fit/adjacent/off_topic verdict,
        the Bayesian evidence combined with the topic prior (config.
        RELEVANCE_PRIOR) into relevance_posterior
      isolated_backtest_only/no_costs_mentioned/extreme_claimed_sharpe/
        weak_stat_support -- bool or None (None = abstract didn't say, not a red flag)
      topic, summary
      relevance_posterior -- Bayesian P(core-fit | topic, relevance_category);
        drives both the displayed rating and the Recent/Monthly gate
      rank_score -- relevance_posterior rescaled to 0-100, kept for
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
    budget = getattr(config, "LLM_RANK_BUDGET_S", 0)
    t0 = time.monotonic()
    for start in range(0, len(items), b):
        if max_batches is not None and batches >= max_batches:
            log(f"[llm] batch budget {max_batches} reached; "
                f"{len(items) - start} items left unscored")
            break
        if budget and time.monotonic() - t0 > budget:
            log(f"[llm] time budget {budget // 60}min reached; "
                f"{len(items) - start} items left unscored (next run picks them up)")
            break
        if not _run_budget_left():
            log(f"[llm] run-wide LLM budget spent; {len(items) - start} items "
                f"left unscored (watchlist/promising items sorted first)")
            break
        batch = items[start:start + b]
        # Fill the batch across providers: the first provider scores what it
        # can, then the NEXT provider is asked only for the items still missing,
        # and so on. This rescues items a provider drops (e.g. Groq skipping its
        # tokens-per-minute-limited chunk) instead of leaving them unscored --
        # important because the watchlist items sort first.
        scores: dict[int, dict] = {}
        for name, fn in _PROVIDERS:
            if name in dead:
                continue
            missing = [i for i in range(len(batch)) if i not in scores]
            if not missing:
                break
            sub = [batch[i] for i in missing]
            try:
                res = fn(sub, log)
            except Exception as e:                 # noqa: BLE001
                log(f"[llm] {name} failed on batch {start // b} "
                    f"({_err(e)}); failing over")
                dead.add(name)                     # stop retrying it this run
                continue
            if res is None:                        # provider not configured
                continue
            used.add(name)
            for local_i, s in res.items():         # map sub-index -> batch index
                if 0 <= local_i < len(missing):
                    scores[missing[local_i]] = s
        if not scores:
            if len(dead) == len([n for n, _ in _PROVIDERS]):
                log("[llm] all providers exhausted; stopping")
                break
            continue
        batches += 1
        for i, it in enumerate(batch):
            if i in scores:
                _apply_score(it, scores[i])
                ranked += 1
        time.sleep(config.LLM_BATCH_PAUSE)         # stay under free-tier RPM
    log(f"[llm] ranked {ranked}/{len(items)} via {', '.join(sorted(used)) or 'none'}")
    return items


def _apply_score(it: dict, s: dict) -> None:
    """Write one parsed score dict onto an item, incl. the Bayesian novelty
    posterior that overwrites the LLM's guessed `provisional` (topic prior x
    antecedent likelihood; non-provisional only when the posterior clears
    NOVELTY_CONFIDENCE). Shared by the triage pass and the consensus merge."""
    it["relevance"] = s["relevance"]
    it["relevance_category"] = s["relevance_category"]
    it["generality"] = s["generality"]
    it["contribution"] = s["contribution"]
    it["novelty_type"] = s["novelty_type"]
    it["testability"] = s["testability"]
    it["antecedent_match"] = s["antecedent_match"]
    it["isolated_backtest_only"] = s["isolated_backtest_only"]
    it["no_costs_mentioned"] = s["no_costs_mentioned"]
    it["extreme_claimed_sharpe"] = s["extreme_claimed_sharpe"]
    it["weak_stat_support"] = s["weak_stat_support"]
    it["topic"] = s["topic"]
    # the SECOND classification: which part of the desk's book this belongs to,
    # and how usable it is there -- judged separately from research quality
    it["sleeves"] = s.get("sleeves") or ["other"]
    it["desk_fit"] = (s.get("desk_fit") or {}).get("level", 0)
    it["summary"] = s["summary"]
    rel_post = relevance_posterior(s["topic"], s["relevance_category"])
    it["relevance_posterior"] = round(rel_post, 3)
    it["rank_score"] = round(rel_post * 100)
    post = novelty_posterior(s["topic"], s["antecedent_match"])
    it["novelty_posterior"] = round(post, 3)
    it["contribution"]["provisional"] = post < config.NOVELTY_CONFIDENCE


# ------------------------------------------- ensemble consensus (shortlist)
_ANT_ORDER = ["matches_known", "ambiguous", "no_antecedent"]   # conservative-first
_REL_ORDER = ["off_topic", "adjacent", "core_fit"]             # conservative-first
_ROBUST_FLAGS = ("isolated_backtest_only", "no_costs_mentioned",
                 "extreme_claimed_sharpe", "weak_stat_support")


def _median_level(picks: list[dict], axis: str) -> int:
    vals = sorted(p[axis]["level"] for p in picks)
    return vals[(len(vals) - 1) // 2]              # lower median (conservative)


def _majority(vals, tie, order=None):
    counts = Counter(vals)
    best = counts.most_common(1)[0][1]
    winners = [v for v, c in counts.items() if c == best]
    if len(winners) == 1:
        return winners[0]
    if order:                                      # tie -> most conservative
        for v in order:
            if v in winners:
                return v
    return tie


def _merge_votes(picks: list[dict]) -> tuple[dict, bool]:
    """Combine >=1 provider score dicts into one merged score (median levels,
    majority verdicts, majority-True robustness flags) + whether they converged
    on contribution (spread <= CONSENSUS_AGREE_SPREAD)."""
    def axis(a):
        return {"level": _median_level(picks, a), "why": picks[0][a]["why"]}
    c_levels = [p["contribution"]["level"] for p in picks]
    agree = (len(picks) == 1
             or max(c_levels) - min(c_levels) <= config.CONSENSUS_AGREE_SPREAD)
    merged = {
        "relevance": axis("relevance"),
        "relevance_category": _majority(
            [p["relevance_category"] for p in picks], "adjacent", _REL_ORDER),
        "generality": axis("generality"),
        "contribution": {"level": _median_level(picks, "contribution"),
                         "why": picks[0]["contribution"]["why"],
                         "provisional": True},
        "testability": axis("testability"),
        "novelty_type": _majority([p["novelty_type"] for p in picks], "none"),
        "antecedent_match": _majority([p["antecedent_match"] for p in picks],
                                      "ambiguous", _ANT_ORDER),
        "topic": _majority([p["topic"] for p in picks], picks[0]["topic"]),
        # consensus rebuilds the score dict from scratch, so the sleeve fields
        # have to be merged here too -- omitting them silently reset the
        # shortlist (the papers that matter most) to sleeve="other"
        # multi-label: keep a tag when at least half the voters chose it
        "sleeves": ([k for k in config.SLEEVES
                     if sum(k in (p.get("sleeves") or []) for p in picks) * 2
                     >= len(picks)] or ["other"]),
        "desk_fit": {"level": _median_level(picks, "desk_fit"),
                     "why": (picks[0].get("desk_fit") or {}).get("why", "")},
        "summary": picks[0]["summary"],
    }
    for f in _ROBUST_FLAGS:
        trues = sum(1 for p in picks if p.get(f) is True)
        falses = sum(1 for p in picks if p.get(f) is False)
        merged[f] = True if trues * 2 > len(picks) else (False if falses and not trues else None)
    return merged, agree


def consensus(items: list[dict], log, max_batches: int | None = None) -> list[dict]:
    """Re-score the promising SHORTLIST (triage relevance/contribution both
    >= the CONSENSUS_MIN_* bars, best first, capped) with EVERY configured
    provider together and combine their independent votes. If the providers
    don't converge on contribution the item is marked provisional (uncertain).
    Mutates in place; non-shortlist items keep their triage scores."""
    if _n_vote_configured() < 2:
        log("[consensus] <2 voting providers configured; skipping (a single "
            "vote just re-scores the triage)")
        return items
    short = [it for it in items
             if (it.get("relevance") or {}).get("level", 0) >= config.CONSENSUS_MIN_RELEVANCE
             and (it.get("contribution") or {}).get("level", 0) >= config.CONSENSUS_MIN_CONTRIB]
    short.sort(key=lambda it: ((it.get("contribution") or {}).get("level", 0),
                               (it.get("relevance") or {}).get("level", 0)), reverse=True)
    short = short[:config.CONSENSUS_MAX_ITEMS]
    if not short:
        log("[consensus] no shortlist items to refine")
        return items

    b = config.LLM_RANK_BATCH
    refined = converged = batches = 0
    budget = getattr(config, "LLM_CONSENSUS_BUDGET_S", 0)
    t0 = time.monotonic()
    for start in range(0, len(short), b):
        if max_batches is not None and batches >= max_batches:
            log(f"[consensus] batch budget {max_batches} reached; "
                f"{len(short) - start} shortlist items keep triage scores")
            break
        if budget and time.monotonic() - t0 > budget:
            log(f"[consensus] time budget {budget // 60}min reached; "
                f"{len(short) - start} shortlist items keep triage scores")
            break
        if not _run_budget_left():
            log(f"[consensus] run-wide LLM budget spent; "
                f"{len(short) - start} shortlist items keep triage scores")
            break
        batch = short[start:start + b]
        votes = []
        for name, fn in _VOTE_PROVIDERS:           # cheap providers only
            try:
                res = fn(batch, log)
            except Exception as e:                 # noqa: BLE001
                log(f"[consensus] {name} failed on batch {start // b} ({_err(e)})")
                continue
            if res:
                votes.append(res)
                time.sleep(config.LLM_BATCH_PAUSE)
        batches += 1
        for i, it in enumerate(batch):
            picks = [res[i] for res in votes if i in res]
            if not picks:
                continue                           # nobody scored it -> keep triage
            merged, agree = _merge_votes(picks)
            _apply_score(it, merged)               # levels + posterior-based provisional
            if not agree:                          # didn't converge -> uncertain
                it["contribution"]["provisional"] = True
            it["consensus_n"] = len(picks)
            it["consensus_agree"] = bool(agree)
            refined += 1
            converged += int(agree)
    log(f"[consensus] refined {refined}/{len(short)} shortlist items via "
        f"ensemble; {converged} converged, {refined - converged} flagged uncertain")
    return items
