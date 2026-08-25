#!/usr/bin/env python3
"""Extract typed artifacts -- Method, Factor, Dataset, Thesis -- per paper.

The rubric already answers "is this paper any good?". It does not answer the
question you actually have when building something: *I am putting together a
macro model with graph neural networks -- what does this paper hand me, what
data will I need, and what is going to bite me?*

That needs the paper decomposed into reusable objects rather than scored:

    Method    a technique, with the inputs it needs and the pitfalls it carries
    Factor    a signal, with its construction and what its numbers assume
    Dataset   what it was built on, and whether you can actually get that
    Thesis    the economic claim, and how hard the evidence for it is

DEPTH GATING IS THE POINT, not a detail. Implementation specifics may only be
claimed from FULL TEXT. From an abstract the model may state the thesis and
name a method; it may not state hyperparameters, sample windows or pitfalls,
because those live in sections an abstract does not contain. A hallucinated
hyperparameter is worse than a missing one -- it reads as knowledge and cannot
be checked without the PDF the reader does not have. `depth` travels with every
record so the portal can show which is which.

    python tools/artifacts.py --dry-run          # show what would be sent
    python tools/artifacts.py --limit 50         # extract 50 papers
    python tools/artifacts.py                    # the whole candidate set
"""

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import llm     # noqa: E402
import store   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
FT_DIR = ROOT / "docs" / "ft"
ARTIFACT_KEY = "artifacts"


def log(m):
    print(m, flush=True)


def _safe(uid: str) -> str:
    """uid -> full-text shard filename. Must match tools/fulltext.py exactly:
    comparing a raw uid (which contains ':') against these names is how a skip
    list once matched 0 of 2,381 papers."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", uid)


def _section_rank(heading: str) -> int:
    """Lower is better. Unlisted sections sort last but are still eligible."""
    h = (heading or "").strip().lower()
    for i, key in enumerate(config.ARTIFACT_SECTION_PRIORITY):
        if key in h:
            return i
    return len(config.ARTIFACT_SECTION_PRIORITY)


def _select_passages(uid: str) -> tuple[str, str]:
    """(text, depth) -- the parts of the paper worth spending context on.

    The median parsed paper is ~54,000 characters. Truncating that at a
    character budget keeps the Introduction and throws away the Methodology and
    Data sections -- exactly the parts this extractor exists to read. So
    sections are ranked by heading and taken in priority order until the budget
    is spent, and each passage keeps its heading so the model can tell a
    robustness caveat from a motivating claim.
    """
    path = FT_DIR / f"{_safe(uid)}.json"
    if not path.exists():
        return "", "abstract"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        return "", "abstract"
    paras = doc.get("p") or []
    if not paras:
        return "", "abstract"

    ordered = sorted(
        enumerate(paras),
        key=lambda pair: (_section_rank(pair[1].get("s")), pair[0]),
    )
    budget, chunks, used = config.ARTIFACT_FULLTEXT_CHARS, [], set()
    for _, para in ordered:
        text = (para.get("t") or "").strip()
        if len(text) < 80:
            continue
        head = (para.get("s") or "").strip()[:60]
        key = text[:120]
        if key in used:                                # GROBID repeats blocks
            continue
        used.add(key)
        piece = f"[{head}] {text}" if head else text
        if len(piece) > budget:
            piece = piece[:budget]
        chunks.append(piece)
        budget -= len(piece)
        if budget <= 200:
            break
    return "\n\n".join(chunks), ("full" if chunks else "abstract")


_SYSTEM = f"""You extract REUSABLE BUILDING BLOCKS from quantitative finance
papers, for a systematic macro/CTA researcher who is about to implement
something and wants to know what this paper gives them.

You are NOT rating the paper. Do not comment on quality, novelty or fit.

Return a JSON array, one object per paper, each: {{"i": <index>, ...}}

FIELDS

"methods": up to {config.ARTIFACT_MAX_METHODS} techniques the paper actually
  uses or introduces. Each:
    "name"     concrete and specific -- "graph attention network over
               sector-linkage adjacency", not "machine learning"
    "family"   one of: {", ".join(config.METHOD_FAMILIES)}
    "what"     one sentence: what it does and why it was chosen here
    "inputs"   what you must have to run it (data, features, panel shape)
    "hyperparams"  FULL TEXT ONLY. Concrete settings the paper reports.
                   "" when working from an abstract.
    "pitfalls"     FULL TEXT ONLY. What the paper itself says goes wrong --
                   instability, leakage, sensitivity, cost. "" from abstract.

"factors": up to {config.ARTIFACT_MAX_FACTORS} tradeable signals. Each:
    "name", "construction" (how it is computed), "universe", "rebalance",
    "reported"  the headline performance AS THE PAPER STATES IT, verbatim-ish
    "costs"     true only if transaction costs are actually modelled

"datasets": up to {config.ARTIFACT_MAX_DATASETS}. Each:
    "name", "provider", "frequency", "coverage" (span/cross-section),
    "access"    one of: {", ".join(config.DATA_ACCESS)}
    "substitute"  a cheaper or public stand-in, if an obvious one exists

"thesis": {{"claim": one sentence, "mechanism": why it should hold,
            "evidence": one of {", ".join(config.EVIDENCE_STRENGTH)}}}

"repro": {{"code": one of {", ".join(config.CODE_AVAILABILITY)},
           "code_url": URL if the paper gives one, else "",
           "data": one of {", ".join(config.DATA_ACCESS)},
           "effort": one of {", ".join(config.BUILD_EFFORT)}}}

RULES

- Every list may be EMPTY. A theory paper has no datasets; a survey has no
  factors. Empty is a correct answer and is always better than a plausible
  invention.
- Papers marked DEPTH=abstract: leave "hyperparams" and "pitfalls" as "".
  You have not seen the sections those live in. Do not infer them from the
  method's name or from what such papers usually do.
- Never invent a code URL. "" unless the text contains one.
- Quote the paper's own numbers. Do not compute, annualise or convert.
"""


def _prompt(batch: list[dict]) -> str:
    parts = []
    for i, it in enumerate(batch):
        body = it["_text"] or (it.get("abstract") or "")[:config.ARTIFACT_ABSTRACT_CHARS]
        parts.append(
            f"### PAPER {i}\n"
            f"DEPTH={it['_depth']}\n"
            f"TITLE: {it.get('title', '')}\n"
            f"AUTHORS: {(it.get('authors') or '')[:180]}\n"
            f"TOPIC: {it.get('topic', '')}\n"
            f"SLEEVES: {', '.join(it.get('sleeves') or []) or '-'}\n"
            f"TEXT:\n{body}\n")
    return "\n".join(parts)


def _pick(value, allowed, default=""):
    v = str(value or "").strip().lower().replace(" ", "_")
    return v if v in allowed else default


def _clean_str(v, n=400) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _validate(raw: dict, depth: str) -> dict:
    """Force the model's output into the schema. Anything outside the allowed
    vocabularies is dropped rather than guessed at -- the same contract the
    scoring path uses, for the same reason: a categorical the portal cannot
    group on is worse than an absent one."""
    out = {"depth": depth}

    methods = []
    for m in (raw.get("methods") or [])[:config.ARTIFACT_MAX_METHODS]:
        if not isinstance(m, dict) or not _clean_str(m.get("name")):
            continue
        methods.append({
            "name": _clean_str(m.get("name"), 140),
            "family": _pick(m.get("family"), config.METHOD_FAMILIES, "other"),
            "what": _clean_str(m.get("what"), 300),
            "inputs": _clean_str(m.get("inputs"), 240),
            # depth gate, enforced in code and not left to the prompt: an
            # abstract cannot support a hyperparameter claim no matter how
            # confidently the model produced one
            "hyperparams": _clean_str(m.get("hyperparams"), 240) if depth == "full" else "",
            "pitfalls": _clean_str(m.get("pitfalls"), 300) if depth == "full" else "",
        })
    out["methods"] = methods

    factors = []
    for f in (raw.get("factors") or [])[:config.ARTIFACT_MAX_FACTORS]:
        if not isinstance(f, dict) or not _clean_str(f.get("name")):
            continue
        factors.append({
            "name": _clean_str(f.get("name"), 140),
            "construction": _clean_str(f.get("construction"), 300),
            "universe": _clean_str(f.get("universe"), 140),
            "rebalance": _clean_str(f.get("rebalance"), 80),
            "reported": _clean_str(f.get("reported"), 200),
            "costs": bool(f.get("costs")),
        })
    out["factors"] = factors

    datasets = []
    for d in (raw.get("datasets") or [])[:config.ARTIFACT_MAX_DATASETS]:
        if not isinstance(d, dict) or not _clean_str(d.get("name")):
            continue
        datasets.append({
            "name": _clean_str(d.get("name"), 120),
            "provider": _clean_str(d.get("provider"), 100),
            "frequency": _clean_str(d.get("frequency"), 60),
            "coverage": _clean_str(d.get("coverage"), 160),
            "access": _pick(d.get("access"), config.DATA_ACCESS, "unclear"),
            "substitute": _clean_str(d.get("substitute"), 160),
        })
    out["datasets"] = datasets

    th = raw.get("thesis") if isinstance(raw.get("thesis"), dict) else {}
    out["thesis"] = {
        "claim": _clean_str(th.get("claim"), 320),
        "mechanism": _clean_str(th.get("mechanism"), 320),
        "evidence": _pick(th.get("evidence"), config.EVIDENCE_STRENGTH, ""),
    }

    rp = raw.get("repro") if isinstance(raw.get("repro"), dict) else {}
    url = _clean_str(rp.get("code_url"), 300)
    out["repro"] = {
        "code": _pick(rp.get("code"), config.CODE_AVAILABILITY, "none"),
        # a URL the model produced that is not a URL is a hallucination with a
        # scheme on the front; drop it rather than render a dead link
        "code_url": url if url.startswith(("http://", "https://")) else "",
        "data": _pick(rp.get("data"), config.DATA_ACCESS, "unclear"),
        "effort": _pick(rp.get("effort"), config.BUILD_EFFORT, ""),
    }
    return out


def select(con, args) -> list[dict]:
    todo = []
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            d = json.loads(meta or "{}")
        except Exception:                              # noqa: BLE001
            continue
        if d.get("retired") or d.get("rank_score") is None:
            continue
        if (d.get("desk_fit") or 0) < args.min_fit:
            continue
        if d.get(ARTIFACT_KEY) and not args.force:
            continue
        text, depth = _select_passages(uid)
        if args.full_only and depth != "full":
            continue
        if not text and not (d.get("abstract") or "").strip():
            continue
        it = dict(d)
        it["uid"] = uid
        it["title"] = title or d.get("title", "")
        it["_text"] = text
        it["_depth"] = depth
        todo.append(it)
    # Full text first: those are the only papers that can yield implementation
    # detail, so they are worth the budget before abstract-only ones.
    todo.sort(key=lambda x: (x["_depth"] != "full", -(x.get("desk_fit") or 0)))
    if args.limit:
        todo = todo[:args.limit]
    return todo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-fit", type=int, default=config.ARTIFACT_MIN_DESK_FIT)
    ap.add_argument("--full-only", action="store_true",
                    help="only papers with parsed full text")
    ap.add_argument("--force", action="store_true",
                    help="re-extract papers that already have artifacts")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the selection and one prompt; call nothing")
    args = ap.parse_args()

    con = store.connect()
    todo = select(con, args)
    full = sum(1 for x in todo if x["_depth"] == "full")
    log(f"[artifacts] {len(todo)} papers to extract "
        f"({full} full text, {len(todo) - full} abstract-only)")
    if not todo:
        return

    if args.dry_run:
        log(f"\n--- system prompt: {len(_SYSTEM)} chars ---")
        sample = todo[:2]
        p = _prompt(sample)
        log(f"--- user prompt for {len(sample)} papers: {len(p)} chars ---\n")
        log(p[:3000])
        return

    written = 0
    _done = set()

    def persist(batch):
        """Checkpoint per batch. A run killed at its timeout must keep what it
        already paid for -- holding results in memory until the end is how a
        five-hour scoring run once wrote nothing at all."""
        nonlocal written
        for it in batch:
            if it["uid"] in _done or not it.get(ARTIFACT_KEY):
                continue
            _done.add(it["uid"])
            if store.update_meta(con, it["uid"], {ARTIFACT_KEY: it[ARTIFACT_KEY]}):
                written += 1
        con.commit()
        log(f"[artifacts] checkpoint: {written} written")

    llm.extract(todo, _SYSTEM, _prompt, _validate, ARTIFACT_KEY,
                log, on_batch=persist, batch_size=config.ARTIFACT_BATCH)

    for it in todo:                                    # anything not checkpointed
        if it["uid"] in _done or not it.get(ARTIFACT_KEY):
            continue
        if store.update_meta(con, it["uid"], {ARTIFACT_KEY: it[ARTIFACT_KEY]}):
            written += 1
    con.commit()

    kinds = {"methods": 0, "factors": 0, "datasets": 0}
    for it in todo:
        a = it.get(ARTIFACT_KEY) or {}
        for k in kinds:
            kinds[k] += len(a.get(k) or [])
    log(f"[artifacts] wrote {written}/{len(todo)}; {kinds}")


if __name__ == "__main__":
    main()
