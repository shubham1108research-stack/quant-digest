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
    "practitioner. For each item, score 0-100 how relevant and important it is "
    "to these interests, and note briefly why.\n\nINTERESTS:\n"
    + config.RANK_INTERESTS
    + "\n\nScore bands: 80-100 = must-read (novel, rigorous, implementable, or "
    "field-defining); 50-79 = relevant; 20-49 = tangential; 0-19 = off-topic or "
    "noise. Be selective -- most items are NOT must-reads. Judge from the title, "
    "authors, source, and abstract provided.\n\n"
    "Also write a crisp one-sentence summary (<= 30 words) of what the paper "
    "does and why it matters to a quant -- concrete about the method, finding, "
    "or asset class; not vague praise.\n\n"
    "Return ONLY a JSON array, one object per item, no prose:\n"
    '[{"i": <index int>, "score": <int 0-100>, "summary": "<one sentence>"}]'
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


def _parse(text: str) -> dict[int, tuple[int, str]]:
    m = re.search(r"\[.*\]", text, re.S)          # tolerate stray prose around it
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:                              # noqa: BLE001
        return {}
    out: dict[int, tuple[int, str]] = {}
    for o in arr:
        try:
            i = int(o["i"])
            score = max(0, min(100, int(o["score"])))
            summary = str(o.get("summary") or o.get("why", "")).strip()[:280]
            out[i] = (score, summary)
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
    r = requests.post(_GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                      json={"model": config.GROQ_MODEL, "temperature": 0,
                            "messages": [{"role": "system", "content": _SYSTEM},
                                         {"role": "user",
                                          "content": _prompt(batch)}]},
                      timeout=90)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    return _parse(text)


_PROVIDERS = [("gemini", _rank_gemini), ("groq", _rank_groq)]


def rank(items: list[dict], log) -> list[dict]:
    """Attach rank_score/summary to each item in place; return the same list."""
    if not items:
        return items
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GROQ_API_KEY")):
        log("[llm] no provider key set; skipping ranking (plain no-LLM feed)")
        return items

    b = config.LLM_RANK_BATCH
    ranked, used, dead = 0, set(), set()
    for start in range(0, len(items), b):
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
            continue
        for i, it in enumerate(batch):
            if i in scores:
                it["rank_score"], it["summary"] = scores[i]
                ranked += 1
        time.sleep(config.LLM_BATCH_PAUSE)         # stay under free-tier RPM
    log(f"[llm] ranked {ranked}/{len(items)} via {', '.join(sorted(used)) or 'none'}")
    return items
