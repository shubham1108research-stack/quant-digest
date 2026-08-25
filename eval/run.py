#!/usr/bin/env python3
"""Measure whether retrieval actually finds the paper that answers a question.

Every tuning decision in this project has so far been argued rather than
measured: 3-small@256 against 3-large@512, RRF against the weighted sum, where
SIM_FLOOR belongs, whether the graph hop earns its 0.12. Those are empirical
questions about THIS corpus and THESE questions, and without a number the
loudest argument wins. That is what this exists to stop.

WHAT IT MEASURES
The browser's retrieval funnel, reproduced exactly:

    1. cosine over docs/vec.bin          -> ASK_RECALL candidates
    2. askRank re-order                  -> W_SIM*sim + W_KW*kw + W_QUALITY*q
    3. graph hop over docs/edges.bin     -> GRAPH_EXPAND neighbours, re-ranked

It stops there. The LLM screening stage that follows is not measured: it costs
real money per run, and a paper the funnel never surfaced cannot be rescued by
it, so recall at this boundary is the ceiling on everything downstream.

WHY THE CONSTANTS ARE PARSED OUT OF portal.py
The ranking lives in JavaScript inside portal.py's page template. A Python copy
is a second definition, and this codebase has now been bitten three times by
two definitions drifting -- the embedder named in two places, the graph built
from a retired model, "it sits behind Access". So the weights and widths are
READ from portal.py at run time and the run dies if they cannot be found: a
renamed constant fails loudly here instead of quietly measuring a system that
no longer exists. The FORMULAE are still duplicated; test_run.py pins those to
hand-computed values, so a change on either side shows up as a test failure
rather than as a number that moved for unclear reasons.

TIERS, and why some questions are SUPPOSED to fail
Every question carries a tier, because one average hides the thing worth
knowing:

  abstract  the answer is in the abstract. Should pass today. A regression
            here means something broke.
  vocab     the answer is in the abstract, but the question uses a
            practitioner's words for it rather than the paper's. Tests whether
            the embedding generalises past surface overlap.
  fulltext  the answer is ONLY in the body of the paper. These are expected to
            FAIL today -- retrieval reads abstracts and nothing else. That
            failure is the measurement of the gap the full-text index is meant
            to close, and the number it will have to beat.

A tier that scores 100% on the day it is written measures nothing.

USAGE
    python eval/run.py                  # measure and print
    python eval/run.py --write-baseline # record eval/baseline.json
    python eval/run.py --check          # fail if worse than the baseline
    python eval/run.py --explain 3      # show the full ranking for question 3
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import struct
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
DOCS = ROOT / "docs"
KS = (5, 10, 20, 50)
# How far a metric may fall before --check calls it a regression. Recall over
# 30 questions moves in steps of about 3.3 points, so a tighter bound than one
# question's worth of movement would fail on noise instead of on changes.
TOLERANCE = 0.034


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- constants
def js_consts(*names):
    """Read the ranking constants out of portal.py -- their only definition."""
    src = (ROOT / "portal.py").read_text(encoding="utf-8")
    out = {}
    for n in names:
        m = re.search(r"(?:const\s+)?\b" + n + r"\s*=\s*(-?[0-9.]+)", src)
        if not m:
            raise SystemExit(
                "[eval] " + n + " is no longer in portal.py. The eval reads "
                "the ranking constants from there deliberately, so this is a "
                "real failure and not a lookup problem: either the constant "
                "was renamed (fix the list here) or the ranking changed shape "
                "(fix the port below).")
        out[n] = float(m.group(1))
    return out


C = js_consts("W_SIM", "W_KW", "W_QUALITY", "ASK_RECALL", "ASK_SCAN",
              "GRAPH_SEED", "GRAPH_EXPAND", "GRAPH_W")

STOP = set((
    "the a an of and or to in on for with is are be as by at from that this what "
    "which how does do did why when we our their its it also than then these those "
    "between across over under more most less least there here into out about after "
    "before during any some all can could would should may might will shall must "
    "have has had been being").split())


def q_terms(q):
    words = re.sub(r"[^a-z0-9 ]", " ", (q or "").lower()).split()
    seen, out = set(), []
    for w in words:
        if len(w) > 2 and w not in STOP and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def kw_hit(terms, text):
    if not terms or not text:
        return 0.0
    t = " " + str(text).lower() + " "
    return sum(1 for w in terms if w in t) / len(terms)


def strength(x):
    g = x.get("generality") or 0
    t = x.get("testability") or 0
    nov = x.get("novelty_posterior") or 0
    return max(0, min(100, round((g + t) / 6 * 50 + nov * 50)))


def ask_quality(x):
    scored = (x.get("generality") is not None or x.get("testability") is not None
              or x.get("novelty_posterior") is not None)
    q = strength(x) / 100 if scored else 0.45
    return max(0.0, min(1.2, q * (x.get("reputation") or 1)))


def ask_rank(x, terms, sim127):
    sim = max(0.0, min(1.0, sim127 / 127.0))
    kw = 0.6 * kw_hit(terms, x.get("title")) + 0.4 * kw_hit(terms, x.get("summary"))
    return C["W_SIM"] * sim + C["W_KW"] * kw + C["W_QUALITY"] * ask_quality(x)


# ---------------------------------------------------------------- the index
class Index:
    def __init__(self):
        meta = json.loads((DOCS / "vec.json").read_text(encoding="utf-8"))
        self.model, self.dim = meta["model"], int(meta["dim"])
        self.uids = meta["uids"]
        raw = np.frombuffer((DOCS / "vec.bin").read_bytes(), dtype=np.int8)
        rows = len(raw) // self.dim if self.dim else 0
        if rows != len(self.uids):
            raise SystemExit(
                "[eval] vec.bin holds %d rows but vec.json names %d uids. The "
                "manifest and the buffer disagree -- the failure loadIndex "
                "clamps around in the browser. Measuring that would measure "
                "the wrong thing, so rebuild the index first."
                % (rows, len(self.uids)))
        self.vec = raw[:rows * self.dim].reshape(rows, self.dim).astype(np.float64)
        arch = json.loads((DOCS / "archive.json").read_text(encoding="utf-8"))
        self.by_uid = {x["uid"]: x for x in arch}
        self.edges = self._load_edges(rows)
        log("[eval] index %s @ %dd, %s rows, %s archive items, %s"
            % (self.model, self.dim, format(rows, ","),
               format(len(self.by_uid), ","),
               "graph loaded" if self.edges else "NO GRAPH"))

    def _load_edges(self, rows):
        p = DOCS / "edges.bin"
        if not p.exists():
            return None
        buf = p.read_bytes()
        if buf[:4] != b"QDG1":
            return None
        n_nodes, n_edges, width = struct.unpack("<IIB3x", buf[4:16])
        fmt = "<HHBB" if width == 2 else "<IIBB"
        size = struct.calcsize(fmt)
        adj = [[] for _ in range(max(rows, n_nodes))]
        off = 16
        for _ in range(n_edges):
            a, b, kind, w = struct.unpack_from(fmt, buf, off)
            off += size
            if a < len(adj):
                adj[a].append((b, kind, w / 255.0))
        return adj

    def search(self, qv, terms):
        """Candidates as the browser builds them: recall, re-rank, graph hop."""
        sims = self.vec @ np.asarray(qv, dtype=np.float64)
        order = np.argsort(-sims, kind="stable")
        seen_title, cands = set(), []
        for i in order:
            it = self.by_uid.get(self.uids[i])
            if not it or it.get("title") in seen_title or it.get("unverified"):
                continue
            seen_title.add(it.get("title"))
            cands.append({"uid": it["uid"], "row": int(i), "it": it,
                          "rank": ask_rank(it, terms, float(sims[i])),
                          "via_graph": False})
            if len(cands) >= int(C["ASK_RECALL"]):
                break
        cands.sort(key=lambda c: -c["rank"])

        if self.edges:
            seeds = [c["row"] for c in cands[:int(C["GRAPH_SEED"])]]
            seed_set, mass = set(seeds), {}
            for i, r in enumerate(seeds):
                decay = 1.0 / (1.0 + i * 0.15)
                for dst, kind, w in (self.edges[r] if r < len(self.edges) else []):
                    if dst in seed_set:
                        continue
                    mass[dst] = mass.get(dst, 0.0) + (
                        1.0 if kind == 1 else max(0.0, w)) * decay
            extra = sorted(mass.items(), key=lambda kv: -kv[1])[:int(C["GRAPH_EXPAND"])]
            known = set(c["row"] for c in cands)
            for row, m in extra:
                if row in known or row >= len(self.uids):
                    continue
                it = self.by_uid.get(self.uids[row])
                if not it:
                    continue
                cands.append({"uid": it["uid"], "row": row, "it": it,
                              "rank": ask_rank(it, terms, 0.0)
                                      + min(0.25, m * C["GRAPH_W"]),
                              "via_graph": True})
            cands.sort(key=lambda c: -c["rank"])
        return cands


# ------------------------------------------------------------ query vectors
def _key(q, model, dim):
    return "%s@%d:%s" % (model, dim, hashlib.sha1(q.encode("utf-8")).hexdigest()[:16])


def query_vectors(questions, model, dim):
    """Cached, so a run costs nothing and CI is deterministic.

    Keyed on model, width AND question text: a reworded question, or an index
    rebuilt with a different embedder, must not silently reuse a vector from
    the old one. That is precisely the mistake the embedding cache itself was
    making until it grew a content hash.
    """
    path = EVAL / "qvec.json"
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    missing = [q for q in questions if _key(q, model, dim) not in cache]
    if missing:
        api = os.environ.get("OPENAI_API_KEY")
        if not api:
            raise SystemExit(
                "[eval] %d question(s) have no cached vector for %s@%d and "
                "OPENAI_API_KEY is not set. Run this where the key exists (the "
                "eval workflow) to refresh eval/qvec.json."
                % (len(missing), model, dim))
        if not model.startswith("text-embedding"):
            # The query MUST be embedded by the model the index was built with,
            # or it lands in a different vector space and every number below is
            # noise dressed as a measurement. Only the OpenAI endpoint is wired
            # up here, so an index built by anything else is a hard stop rather
            # than a POST of a foreign model name to api.openai.com.
            raise SystemExit(
                "[eval] the index was built with %r, and this harness can only "
                "embed questions with OpenAI text-embedding models. Add the "
                "provider here, or rebuild the index -- do NOT measure across "
                "two vector spaces." % model)
        import requests
        log("[eval] embedding %d new question(s) with %s@%d"
            % (len(missing), model, dim))
        for i in range(0, len(missing), 32):
            chunk = missing[i:i + 32]
            body = {"model": model, "input": chunk}
            if model.startswith("text-embedding"):
                body["dimensions"] = dim
            r = requests.post("https://api.openai.com/v1/embeddings", json=body,
                              headers={"authorization": "Bearer " + api}, timeout=90)
            r.raise_for_status()
            for q, d in zip(chunk, r.json()["data"]):
                cache[_key(q, model, dim)] = [round(float(v), 6) for v in d["embedding"]]
        path.write_text(json.dumps(cache), encoding="utf-8")
    return dict((q, cache[_key(q, model, dim)]) for q in questions)


# ------------------------------------------------------------------ metrics
MISSING = 10 ** 9


def measure(idx, golden, explain=None):
    qs = [g["q"] for g in golden]
    qvecs = query_vectors(qs, idx.model, idx.dim)
    per_q = []
    for n, g in enumerate(golden):
        terms = q_terms(g["q"])
        cands = idx.search(qvecs[g["q"]], terms)
        pos = dict((c["uid"], i) for i, c in enumerate(cands))
        expect = [u for u in g["expect"] if u in idx.by_uid]
        dropped = [u for u in g["expect"] if u not in idx.by_uid]
        ranks = sorted(pos.get(u, MISSING) for u in expect)
        per_q.append({"q": g["q"], "tier": g["tier"], "expect": expect,
                      "dropped": dropped, "ranks": ranks})
        if explain is not None and n == explain:
            log("\n[eval] %s\n  tier=%s  terms=%s" % (g["q"], g["tier"], terms))
            for u in expect:
                r = pos.get(u)
                t = (idx.by_uid[u].get("title") or "")[:70]
                log("  expected %s  rank=%s  %s"
                    % (u, "MISSED" if r is None else r, t))
            log("  top 10 returned:")
            for c in cands[:10]:
                log("   %s %.3f  %s" % ("G" if c["via_graph"] else " ",
                                        c["rank"], (c["it"].get("title") or "")[:66]))
    return per_q


def summarise(per_q):
    def block(rows):
        if not rows:
            return None
        out = {"n": len(rows)}
        for k in KS:
            # recall: what SHARE of a question's expected papers made the cut,
            # averaged over questions. hit: whether ANY of them did. They differ
            # only for multi-answer questions and both matter -- hit says the
            # reader gets an answer, recall says how much of the evidence for it
            # actually arrives.
            out["recall@%d" % k] = round(sum(
                sum(1 for r in q["ranks"] if r < k) / max(1, len(q["expect"]))
                for q in rows) / len(rows), 4)
            out["hit@%d" % k] = round(sum(
                1 for q in rows if q["ranks"] and q["ranks"][0] < k) / len(rows), 4)
        out["mrr"] = round(sum(
            (1.0 / (q["ranks"][0] + 1)) if q["ranks"] and q["ranks"][0] < 50 else 0.0
            for q in rows) / len(rows), 4)
        return out

    tiers = sorted(set(q["tier"] for q in per_q))
    return {"overall": block(per_q),
            "tiers": dict((t, block([q for q in per_q if q["tier"] == t]))
                          for t in tiers)}


def render(summary, per_q):
    log("")
    log("  %-10s %5s %8s %8s %8s %8s %8s"
        % ("tier", "n", "hit@5", "hit@10", "hit@20", "rec@20", "MRR"))
    log("  " + "-" * 60)
    for t, b in list(summary["tiers"].items()) + [("OVERALL", summary["overall"])]:
        if not b:
            continue
        log("  %-10s %5d %8.2f %8.2f %8.2f %8.2f %8.3f"
            % (t, b["n"], b["hit@5"], b["hit@10"], b["hit@20"],
               b["recall@20"], b["mrr"]))

    missed = [q for q in per_q if not q["ranks"] or q["ranks"][0] >= 20]
    if missed:
        log("\n  missed at 20 (%d):" % len(missed))
        for q in missed:
            r = "none" if not q["ranks"] or q["ranks"][0] >= MISSING else str(q["ranks"][0])
            log("    [%-8s] rank=%5s  %s" % (q["tier"], r, q["q"][:64]))

    orphan = sorted(set(u for q in per_q for u in q["dropped"]))
    if orphan:
        log("\n  WARNING: %d expected uid(s) are not in the archive at all. The "
            "golden set names papers that no longer exist, so those questions "
            "are unscoreable rather than failing -- fix the golden set:"
            % len(orphan))
        for u in orphan[:10]:
            log("    " + u)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--explain", type=int, default=None,
                    help="print the full ranking for question N (0-based)")
    ap.add_argument("--golden", default=str(EVAL / "golden.json"))
    args = ap.parse_args()

    golden = json.loads(
        pathlib.Path(args.golden).read_text(encoding="utf-8"))["questions"]
    idx = Index()
    per_q = measure(idx, golden, explain=args.explain)
    summary = summarise(per_q)
    summary["index"] = {"model": idx.model, "dim": idx.dim, "rows": len(idx.uids)}
    render(summary, per_q)

    if args.write_baseline:
        (EVAL / "baseline.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        log("\n[eval] wrote eval/baseline.json")
        return

    if args.check:
        p = EVAL / "baseline.json"
        if not p.exists():
            log("\n[eval] no baseline recorded yet -- run --write-baseline once "
                "so later runs have something to regress against.")
            return
        base = json.loads(p.read_text(encoding="utf-8"))
        if base.get("index") != summary["index"]:
            log("\n[eval] NOTE: the index changed since the baseline (%s -> %s). "
                "Comparing across different indexes is the POINT when you are "
                "choosing an embedder and meaningless when you are not, so read "
                "the numbers rather than trusting the exit code."
                % (base.get("index"), summary["index"]))
        bad = []
        for metric in ("recall@20", "hit@10", "mrr"):
            was = base.get("overall", {}).get(metric)
            now = summary["overall"].get(metric)
            if was is None or now is None:
                continue
            if now < was - TOLERANCE:
                bad.append("%s: %.3f -> %.3f" % (metric, was, now))
        if bad:
            log("\n[eval] REGRESSION against eval/baseline.json:")
            for b in bad:
                log("    " + b)
            sys.exit(1)
        log("\n[eval] no regression against the baseline.")


if __name__ == "__main__":
    main()
