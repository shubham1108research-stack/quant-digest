"""Optional LLM triage layer (Google Gemini, free tier).

Ranks the deduped items 0-100 for relevance to the configured research
interests and attaches (rank_score, why) to each. Entirely optional: dormant
unless GEMINI_API_KEY (or GOOGLE_API_KEY) is set, and every failure path
degrades to the plain no-LLM feed rather than breaking the run.

Uses the Gemini REST API via `requests` -- no extra dependency, no cost on the
free tier. Swap providers by rewriting `_rank_batch` only.
"""

import json
import os
import re
import time

import requests

import config

_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
             "{model}:generateContent")

_SYSTEM = (
    "You are a quantitative-research analyst curating a weekly digest for a "
    "practitioner. For each item, score 0-100 how relevant and important it is "
    "to these interests, and note briefly why.\n\nINTERESTS:\n"
    + config.RANK_INTERESTS
    + "\n\nScore bands: 80-100 = must-read (novel, rigorous, implementable, or "
    "field-defining); 50-79 = relevant; 20-49 = tangential; 0-19 = off-topic or "
    "noise. Be selective -- most items are NOT must-reads. Judge from the title, "
    "authors, source, and abstract provided.\n\n"
    "Return ONLY a JSON array, one object per item, no prose:\n"
    '[{"i": <index int>, "score": <int 0-100>, "why": "<= 12 words"}]'
)


def _api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


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
            out[i] = (score, str(o.get("why", "")).strip()[:120])
        except Exception:                          # noqa: BLE001
            continue
    return out


def _rank_batch(key: str, batch: list[dict], log) -> dict[int, tuple[int, str]]:
    url = _ENDPOINT.format(model=config.LLM_MODEL)
    body = {
        "system_instruction": {"parts": [{"text": _SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": _prompt(batch)}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }
    for attempt in range(config.LLM_MAX_RETRIES):
        r = requests.post(url, params={"key": key},
                          json=body, timeout=90)
        if r.status_code in (429, 500, 503):
            wait = 10 * (attempt + 1)
            log(f"[llm] {r.status_code}; retry {attempt + 1}/"
                f"{config.LLM_MAX_RETRIES} after {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        cands = r.json().get("candidates") or []
        if not cands:
            return {}
        text = "".join(p.get("text", "")
                       for p in (cands[0].get("content") or {}).get("parts", []))
        return _parse(text)
    r.raise_for_status()                           # exhausted retries
    return {}


def rank(items: list[dict], log) -> list[dict]:
    """Attach rank_score/why to each item in place; return the same list."""
    if not items:
        return items
    key = _api_key()
    if not key:
        log("[llm] GEMINI_API_KEY not set; skipping ranking (plain no-LLM feed)")
        return items

    b = config.LLM_RANK_BATCH
    ranked = 0
    for start in range(0, len(items), b):
        batch = items[start:start + b]
        try:
            scores = _rank_batch(key, batch, log)
        except Exception as e:                     # noqa: BLE001
            log(f"[llm] batch {start // b} failed: {type(e).__name__}: {e}")
            continue
        for i, it in enumerate(batch):
            if i in scores:
                it["rank_score"], it["why"] = scores[i]
                ranked += 1
        time.sleep(config.LLM_BATCH_PAUSE)         # stay under free-tier RPM
    log(f"[llm] ranked {ranked}/{len(items)} items via {config.LLM_MODEL}")
    return items
