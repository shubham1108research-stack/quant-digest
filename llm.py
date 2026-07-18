"""Optional LLM triage layer with provider fallback.

Ranks the deduped items 0-100 for relevance and attaches (rank_score, summary).
Tries providers in order -- Gemini first, then Groq -- so if one provider's key
expires or its quota is exhausted, ranking automatically fails over to the next.
Entirely optional: dormant unless at least one provider key is set, and every
failure path degrades to the plain no-LLM feed rather than breaking the run.

Keys (any/all, free tiers):
  GEMINI_API_KEY (or GOOGLE_API_KEY)  -- https://aistudio.google.com/apikey
  GROQ_API_KEY                        -- https://console.groq.com/keys
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

_SYSTEM = (
    "You are a quantitative-research analyst curating a digest for a "
    "practitioner. For each item, give TWO 0-100 scores and a one-line "
    "summary.\n\nINTERESTS:\n"
    + config.RANK_INTERESTS
    + "\n\n1) relevance -- how relevant/important to the interests above. Bands: "
    "80-100 = must-read; 50-79 = relevant; 20-49 = tangential; 0-19 = off-topic "
    "or noise.\n"
    "2) innovation -- how novel/original the contribution is: 80-100 = a new "
    "method, idea, or field-defining result; 50-79 = a meaningful advance; "
    "20-49 = incremental; 0-19 = derivative or a survey. Judge novelty on its "
    "own merits, independent of citation count.\n\n"
    "Be selective -- most items are neither must-reads nor highly innovative. "
    "Judge from the title, authors, source, and abstract provided.\n\n"
    "Also write a crisp one-sentence summary (<= 30 words) of what the paper "
    "does and why it matters to a quant -- concrete about the method, finding, "
    "or asset class; not vague praise.\n\n"
    "Return ONLY a JSON array, one object per item, no prose:\n"
    '[{"i": <int>, "relevance": <int 0-100>, "innovation": <int 0-100>, '
    '"summary": "<one sentence>"}]'
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


def _parse(text: str) -> dict[int, tuple[int, int, str]]:
    """-> {index: (relevance, innovation, summary)}. Tolerates the older
    'score' key (treated as relevance) and a missing innovation (falls back to
    relevance) so a stray old-format response still parses."""
    m = re.search(r"\[.*\]", text, re.S)          # tolerate stray prose around it
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:                              # noqa: BLE001
        return {}
    out: dict[int, tuple[int, int, str]] = {}
    for o in arr:
        try:
            i = int(o["i"])
            rel_raw = o.get("relevance", o.get("score"))
            relevance = max(0, min(100, int(rel_raw)))
            inv = o.get("innovation")
            innovation = max(0, min(100, int(inv))) if inv is not None else relevance
            summary = str(o.get("summary") or o.get("why", "")).strip()[:280]
            out[i] = (relevance, innovation, summary)
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


_PROVIDERS = [("gemini", _rank_gemini), ("groq", _rank_groq)]


def have_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                or os.environ.get("GROQ_API_KEY"))


def rank(items: list[dict], log, max_batches: int | None = None) -> list[dict]:
    """Attach rank_score (relevance), innovation, and summary to each item in
    place; return the same list. Scored items get all three keys; unscored items
    get none, so callers can tell them apart (`'innovation' in it`).

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
                it["rank_score"], it["innovation"], it["summary"] = scores[i]
                ranked += 1
        time.sleep(config.LLM_BATCH_PAUSE)         # stay under free-tier RPM
    log(f"[llm] ranked {ranked}/{len(items)} via {', '.join(sorted(used)) or 'none'}")
    return items
