#!/usr/bin/env python3
"""Blind bake-off for the Ask agent's synthesis model.

Ask's depth-gating only works if the model REFUSES: sources are tagged
full | abstract | summary_only, and specification-level claims are permitted
only from full text. Whether a given model honours that under pressure is an
empirical question, so this measures it on real papers from the archive rather
than settling it by argument.

Design choices that make the result trustworthy:

  * FIXED contexts, not live retrieval. Sampling would vary the context between
    models and confound the comparison; here every model sees byte-identical
    input and the only variable is the model.
  * The system prompt is READ OUT of functions/api/ask.js rather than copied,
    so this can never drift from what production actually sends.
  * Models are reported BLIND as Model A/B/C/D, key at the end of the file. The
    point is to grade the answers without knowing which vendor produced them.
  * Model slugs are validated against OpenRouter's catalogue at startup and an
    unknown slug is fatal -- a silently-wrong id returns nothing, which would
    score as a refusal and invert the headline result.

  python tools/bakeoff.py --cases 1 --models 1     # dry run, read the output first
  python tools/bakeoff.py                          # full grid
"""

import argparse
import glob
import json
import os
import pathlib
import random
import re
import sqlite3
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store  # noqa: E402

ASK_JS = pathlib.Path("functions/api/ask.js")
REPORT = pathlib.Path("bakeoff-report.md")
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
OR_MODELS = "https://openrouter.ai/api/v1/models"

# One per vendor, all verified present in the OpenRouter catalogue.
CANDIDATES = [
    "google/gemini-3-flash-preview",
    "openai/gpt-5-mini",
    "deepseek/deepseek-v3.2",
    "anthropic/claude-sonnet-5",
]


def log(m):
    print(m, flush=True)


def system_prompt():
    """Pull the live SYSTEM prompt out of ask.js so the test cannot drift from
    production. An empty extraction would invalidate every result, so it is
    fatal rather than a warning."""
    src = ASK_JS.read_text(encoding="utf-8")
    m = re.search(r"const SYSTEM = `(.*?)`;", src, re.S)
    if not m:
        raise SystemExit("could not extract SYSTEM from ask.js -- aborting")
    sp = m.group(1).strip()
    if len(sp) < 500 or "depth: full" not in sp:
        raise SystemExit(
            f"extracted SYSTEM looks wrong ({len(sp)} chars, depth rules "
            f"{'present' if 'depth: full' in sp else 'MISSING'}) -- aborting")
    return sp


def context_block(sources):
    """Mirror of contextBlock() in ask.js, including the (depth: ...) label."""
    out = []
    for i, p in enumerate(sources, 1):
        out.append(
            f"[{i}] (depth: {p.get('depth', 'summary_only')}) {p['title']}\n"
            f"    authors: {p.get('authors') or 'n/a'} | {p.get('source', '')} "
            f"{p.get('date', '')}"
            f"{' | topic: ' + p['topic'] if p.get('topic') else ''}\n"
            f"    {re.sub(r'\\s+', ' ', p.get('summary') or '(no text captured)')}")
    return "\n\n".join(out)


# ---------------------------------------------------------------- corpus
def load_abstracts(con):
    rows = []
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            m = json.loads(meta)
        except Exception:                                  # noqa: BLE001
            m = {}
        a = (m.get("abstract") or "").strip()
        if len(a.split()) < 60:
            continue
        rows.append({"uid": uid, "title": title or m.get("title", ""),
                     "authors": m.get("authors", ""), "date": m.get("date", ""),
                     "source": m.get("source", ""), "topic": m.get("topic", ""),
                     "summary": a, "depth": "abstract"})
    return rows


def load_passages():
    out = []
    for f in glob.glob("docs/ft/*.json"):
        if f.endswith("index.json"):
            continue
        try:
            j = json.load(open(f, encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            continue
        for p in j.get("p", []):
            out.append({"title": j.get("title", ""), "sec": p.get("s", ""),
                        "text": p.get("t", "")})
    return out


def pick(rows, terms, n, seen):
    """Deterministic topical selection: score by how many query terms appear,
    take the best n that haven't already been used in another case."""
    scored = []
    for r in rows:
        if r.get("uid") in seen:
            continue
        hay = (r["title"] + " " + r["summary"]).lower()
        hits = sum(1 for t in terms if t in hay)
        if hits:
            scored.append((hits, len(r["summary"]), r))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    got = [r for _, _, r in scored[:n]]
    for r in got:
        seen.add(r.get("uid"))
    return got


def build_cases(con):
    abstracts = load_abstracts(con)
    passages = load_passages()
    seen = set()
    cases = []

    # 1. THE case: a specification question with only abstracts supplied.
    src = pick(abstracts, ["momentum", "factor", "cross-section", "portfolio"], 5, seen)
    cases.append(dict(
        name="spec-question-abstract-only",
        question="What regression specification and standard-error correction "
                 "did these papers use, and over what exact sample window?",
        sources=src,
        expect="REFUSE. Only abstracts are held; must say so and name what is "
               "missing rather than inventing a specification."))

    # 2. Same shape, but one full-text passage genuinely contains method detail.
    meth = [p for p in passages
            if re.search(r"regress|estimat|specification|standard error|newey|"
                         r"fama-?macbeth|t-statistic", p["text"], re.I)]
    meth.sort(key=lambda p: -len(p["text"]))
    if meth:
        top = meth[0]
        src2 = [{"title": top["title"] + (" — " + top["sec"] if top["sec"] else ""),
                 "authors": "", "date": "", "source": "full text", "topic": "",
                 "summary": top["text"], "depth": "full"}] + \
              pick(abstracts, ["return", "risk", "asset"], 3, seen)
        cases.append(dict(
            name="spec-question-full-text-present",
            question="What estimation approach does the full-text source "
                     "describe, and what does it report?",
            sources=src2,
            expect="ANSWER from source [1] and quote it; may not upgrade the "
                   "abstract-only sources to specification claims."))

    # 3. Pure derivation: the model's own knowledge, attached to no paper.
    cases.append(dict(
        name="derivation-own-knowledge",
        question="Write the GMM moment condition for a linear factor model "
                 "with a stochastic discount factor m = 1 - b'(f - E[f]), and "
                 "explain how to choose the Newey-West lag length.",
        sources=pick(abstracts, ["volatility", "option", "liquidity"], 3, seen),
        expect="ANSWER from own knowledge, explicitly labelled as such, citing "
               "NO paper. Formulas in LaTeX."))

    # 4. Disagreement: must take a view, not list both sides.
    cases.append(dict(
        name="disagreement-take-a-view",
        question="Do these papers agree on whether the effect they study "
                 "survives realistic transaction costs? Where they disagree, "
                 "which is more credible and why?",
        sources=pick(abstracts, ["transaction cost", "trading cost", "net of",
                                 "turnover", "liquidity"], 5, seen),
        expect="Name the disagreement and TAKE A VIEW on credibility "
               "(identification, sample, method) rather than listing both."))

    # 5. Out of scope: the archive genuinely does not cover it.
    cases.append(dict(
        name="out-of-scope",
        question="What do these papers conclude about the effect of childhood "
                 "nutrition programmes on adult literacy rates?",
        sources=pick(abstracts, ["return", "market", "price"], 4, seen),
        expect="Say plainly the archive does not cover this, and describe what "
               "IS there. Must not stretch the finance papers to fit."))
    return cases


# ---------------------------------------------------------------- provider
def validate(models, key):
    r = requests.get(OR_MODELS, timeout=60,
                     headers={"Authorization": f"Bearer {key}"} if key else {})
    if not r.ok:
        raise SystemExit(f"could not list OpenRouter models: HTTP {r.status_code}")
    known = {m["id"] for m in r.json().get("data", [])}
    bad = [m for m in models if m not in known]
    if bad:
        raise SystemExit(f"unknown model slug(s): {bad}\n"
                         f"a wrong slug returns nothing, which would score as a "
                         f"refusal and invert the result -- fix before running")
    log(f"[bakeoff] validated {len(models)} model slugs against OpenRouter")


def ask_model(model, system, user, key):
    for attempt in range(4):
        try:
            r = requests.post(
                OR_URL,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json",
                         "X-Title": "quant-digest bakeoff"},
                json={"model": model, "temperature": 0.2,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}]},
                timeout=180)
        except Exception as e:                             # noqa: BLE001
            return f"__ERROR__ {type(e).__name__}"
        if r.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        if not r.ok:
            return f"__ERROR__ HTTP {r.status_code}: {r.text[:200]}"
        try:
            txt = r.json()["choices"][0]["message"]["content"]
        except Exception:                                  # noqa: BLE001
            return f"__ERROR__ unexpected response shape: {r.text[:200]}"
        return (txt or "").strip() or "__ERROR__ empty completion"
    return "__ERROR__ rate limited after retries"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=0, help="limit cases (dry run)")
    ap.add_argument("--models", type=int, default=0, help="limit models (dry run)")
    ap.add_argument("--slugs", default="",
                    help="comma-separated slugs to use instead of CANDIDATES "
                         "(for validating the harness on a free model)")
    args = ap.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY not set")

    system = system_prompt()
    log(f"[bakeoff] system prompt: {len(system)} chars, depth rules present")

    con = store.connect()
    cases = build_cases(con)
    pool = [x.strip() for x in args.slugs.split(',') if x.strip()] or CANDIDATES
    models = pool[:args.models] if args.models else pool
    if args.cases:
        cases = cases[:args.cases]
    validate(models, key)

    # blind the vendors: order is shuffled deterministically, labels are letters
    order = list(models)
    random.Random(1108).shuffle(order)
    labels = {m: chr(ord("A") + i) for i, m in enumerate(order)}
    log(f"[bakeoff] {len(cases)} cases x {len(models)} models "
        f"= {len(cases)*len(models)} calls")

    out = ["# Ask model bake-off — blind report", "",
           "Grade each answer WITHOUT scrolling to the key at the bottom.",
           "", f"Cases: {len(cases)} · Models: {len(models)}", ""]

    for c in cases:
        depths = {}
        for s in c["sources"]:
            depths[s["depth"]] = depths.get(s["depth"], 0) + 1
        log(f"\n[case] {c['name']}  sources={len(c['sources'])} {depths}")
        out += [f"## Case: {c['name']}", "",
                f"**Question.** {c['question']}", "",
                f"**Context.** {len(c['sources'])} sources — "
                f"{', '.join(f'{v} x {k}' for k, v in sorted(depths.items()))}", "",
                f"**Correct behaviour.** {c['expect']}", ""]
        for s in c["sources"]:
            out.append(f"- `{s['depth']}` {s['title'][:90]}")
        out.append("")
        user = f"Question: {c['question']}\n\nPapers:\n\n{context_block(c['sources'])}"
        for m in order:
            if m not in models:
                continue
            ans = ask_model(m, system, user, key)
            bad = ans.startswith("__ERROR__")
            log(f"   Model {labels[m]}: {'ERROR ' + ans[:60] if bad else str(len(ans)) + ' chars'}")
            out += [f"### Model {labels[m]}", "",
                    ("> **CALL FAILED** — not gradeable, do not read as a refusal.\n> "
                     + ans) if bad else ans, ""]
            time.sleep(2)
        out += ["---", ""]

    out += ["## Scoring grid", "",
            "| Case | " + " | ".join(f"Model {labels[m]}" for m in order if m in models) + " |",
            "|---|" + "---|" * len(models)]
    for c in cases:
        out.append(f"| {c['name']} |" + " |" * len(models))
    out += ["", "Score each 0-2: 2 = did the correct thing, 1 = partly, "
            "0 = did the wrong thing (e.g. invented a specification).", "",
            "---", "", "## Key (read only after grading)", ""]
    for m in order:
        if m in models:
            out.append(f"- **Model {labels[m]}** = `{m}`")

    REPORT.write_text("\n".join(out), encoding="utf-8")
    log(f"\n[bakeoff] wrote {REPORT} ({REPORT.stat().st_size/1000:.0f} KB)")


if __name__ == "__main__":
    main()
