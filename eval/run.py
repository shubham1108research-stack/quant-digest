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
# Set by --no-graph. Module-level because Index() reads it at construction,
# the same way DOCS is read.
NO_GRAPH = False
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


# ------------------------------------------------------------------ variants
# Alternative ways to order the SAME candidate set. They exist because the
# first eval run showed the re-rank is not a tie-breaker, it is the dominant
# effect: papers the embedding ranked 1st, 2nd and 5th came out 512th, 356th
# and 116th. Which replacement is better is not something to settle by
# argument, so each is implemented here and measured on the same 30 questions.
#
# The suspected cause, and what each variant does about it:
#
#   askRank blends three terms on incompatible RANGES. `kw` spans the full
#   [0,1] -- a question whose words all appear scores exactly 1 -- and quality
#   reaches 1.2. But `sim` is a cosine, and on this corpus cosines live in a
#   narrow band around 0.2-0.7 and essentially never reach 1. So a 0.55 weight
#   on a term that only ever moves through half its range is worth far less
#   than 0.30 on a term that uses all of its own. The weights say similarity
#   dominates; the arithmetic says keyword overlap does.
#
#   current  what portal.py does NOW: rescale sim across the candidate set so
#            it uses [0,1] like the others. Same three terms, same weights --
#            the smallest change that addresses the diagnosed cause. Chosen by
#            the comparison below, not by argument. ("minmax" still works as a
#            name for it, so older invocations keep meaning the same thing.)
#   legacy   what portal.py did BEFORE that, kept so the fix is falsifiable.
#   rrf      Reciprocal Rank Fusion. Uses only RANKS, so no term's numeric
#            range can dominate another's and the weights disappear entirely.
#            Fuses THREE lists -- dense, keyword and the archive's own quality
#            posterior -- because dropping quality would turn a curated digest
#            into a search box.
#   sim_only the embedding alone, as the floor. If nothing beats this, the
#            entire re-ranking stage is costing more than it earns.
RRF_K = 60.0            # conventional; the constant matters little above ~20


def _kw_of(it, terms):
    return 0.6 * kw_hit(terms, it.get("title")) + 0.4 * kw_hit(terms, it.get("summary"))


def _ranks_by(cands, keyfn):
    out = [0] * len(cands)
    for pos, i in enumerate(sorted(range(len(cands)), key=keyfn)):
        out[i] = pos
    return out


def _rescore(cands, terms, variant):
    """Set c['rank'] for every candidate, then sort. Mutates in place."""
    if not cands:
        return
    if variant == "legacy":
        # The pre-2026-08-26 ranking, kept so the fix stays falsifiable: if a
        # later change makes this win again, that is worth knowing rather than
        # discovering by accident.
        for c in cands:
            c["rank"] = (ask_rank(c["it"], terms, c["sim"])
                         + min(0.25, c["mass"] * C["GRAPH_W"]))
    elif variant == "sim_only":
        for c in cands:
            c["rank"] = (max(0.0, c["sim"] / 127.0)
                         + min(0.25, c["mass"] * C["GRAPH_W"]))
    elif variant in ("current", "minmax"):
        vals = [c["sim"] for c in cands if not c["via_graph"]] or [0.0]
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1.0
        for c in cands:
            s = 0.0 if c["via_graph"] else max(0.0, min(1.0, (c["sim"] - lo) / rng))
            c["rank"] = (C["W_SIM"] * s + C["W_KW"] * _kw_of(c["it"], terms)
                         + C["W_QUALITY"] * ask_quality(c["it"])
                         + min(0.25, c["mass"] * C["GRAPH_W"]))
    elif variant.startswith("rrf"):
        # K controls how flat the fusion is. At K=60 the gap between rank 0 and
        # rank 1 is 1/60 - 1/61, which is almost nothing, so the top of the
        # list is decided by near-ties and MRR collapses even while hit@20
        # improves. Smaller K sharpens the head. Which value is right is an
        # empirical question about this corpus, so it is a variant.
        k = float(variant[3:]) if variant[3:] else RRF_K
        rs = _ranks_by(cands, lambda i: -cands[i]["sim"])
        rk = _ranks_by(cands, lambda i: -_kw_of(cands[i]["it"], terms))
        rq = _ranks_by(cands, lambda i: -ask_quality(cands[i]["it"]))
        rm = _ranks_by(cands, lambda i: -cands[i]["mass"])
        for i, c in enumerate(cands):
            # the graph is a fourth list rather than an additive bonus: in rank
            # space an additive constant is not comparable to 1/(k+rank), and
            # bolting one on would silently make the graph either inert or
            # overwhelming depending on K.
            c["rank"] = (1.0 / (k + rs[i]) + 1.0 / (k + rk[i])
                         + 1.0 / (k + rq[i])
                         + (1.0 / (k + rm[i]) if c["mass"] > 0 else 0.0))
    else:
        raise SystemExit("[eval] unknown variant %r" % variant)
    cands.sort(key=lambda c: -c["rank"])


VARIANTS = ("current", "legacy", "rrf", "rrf20", "rrf5", "sim_only")


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
        # archive.json is the PAPERS, not the vectors, and is identical for any
        # embedder -- so a bake-off directory holding only vec.bin/vec.json
        # falls back to the real one rather than needing a copy.
        arch_p = DOCS / "archive.json"
        if not arch_p.exists():
            arch_p = ROOT / "docs" / "archive.json"
        arch = json.loads(arch_p.read_text(encoding="utf-8"))
        self.by_uid = {x["uid"]: x for x in arch}
        # docs/edges.bin is derived FROM the OpenAI vectors. Loading it for a
        # different embedder would let one model's neighbours rescue another
        # model's misses, which measures a hybrid nobody would ship. NO_GRAPH
        # turns it off for both sides so the comparison is the embedding alone.
        self.edges = None if NO_GRAPH else self._load_edges(rows)
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

    def cosine_ranks(self, qv, uids):
        """Where each uid sits by RAW COSINE, before any re-ranking.

        This is the diagnostic that says WHICH STAGE lost a paper, and without
        it the headline numbers are uninterpretable. A paper that is 4,000th by
        cosine was never reachable and no change to the weights will help --
        that is an embedding problem. A paper that is 12th by cosine and 300th
        after askRank was found and then thrown away, which is a weighting
        problem and a much cheaper fix. Treating those two as one number is how
        you end up rebuilding an index that was working.
        """
        sims = self.vec @ np.asarray(qv, dtype=np.float64)
        order = np.argsort(-sims, kind="stable")
        where = {}
        for rank, i in enumerate(order):
            u = self.uids[i]
            if u in uids and u not in where:
                where[u] = rank
                if len(where) == len(uids):
                    break
        return where

    def search(self, qv, terms, variant="current"):
        """Candidates as the browser builds them: recall, re-rank, graph hop.

        The CANDIDATE SET is identical for every variant -- top ASK_RECALL by
        cosine, plus the graph hop -- and only the final ordering changes. That
        is what makes the comparison fair, and it also bounds it: a paper that
        cosine puts 3,967th is not in the set under any variant, so no amount
        of re-ranking reaches it. Those are embedding failures and they need a
        different fix.
        """
        sims = self.vec @ np.asarray(qv, dtype=np.float64)
        order = np.argsort(-sims, kind="stable")
        seen_title, cands = set(), []
        for i in order:
            it = self.by_uid.get(self.uids[i])
            if not it or it.get("title") in seen_title or it.get("unverified"):
                continue
            seen_title.add(it.get("title"))
            cands.append({"uid": it["uid"], "row": int(i), "it": it,
                          "sim": float(sims[i]), "mass": 0.0, "via_graph": False})
            if len(cands) >= int(C["ASK_RECALL"]):
                break

        if self.edges:
            ordered = sorted(cands, key=lambda c: -ask_rank(c["it"], terms, c["sim"]))
            seeds = [c["row"] for c in ordered[:int(C["GRAPH_SEED"])]]
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
                              "sim": 0.0, "mass": m, "via_graph": True})
        _rescore(cands, terms, variant)
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
        # The key is checked on the OpenAI path ONLY, and further down. It used
        # to be checked here, which meant the local-model branch below -- which
        # never touches api.openai.com -- refused to run without a key it had
        # no use for. That silently turned the whole embedding bake-off into a
        # build with no measurement: the bge index was constructed, all 20,999
        # rows of it, and then scored zero questions.
        if "/" in model:
            # A local sentence-transformers model, named org/model. The QUERY
            # must be embedded by the same model as the index or the two land
            # in different vector spaces and every number below is noise
            # wearing a measurement's clothes.
            log("[eval] embedding %d question(s) locally with %s"
                % (len(missing), model))
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
            st = SentenceTransformer(model)
            vecs = st.encode(missing, convert_to_numpy=True,
                             normalize_embeddings=False)
            for q, v in zip(missing, vecs):
                cache[_key(q, model, dim)] = [round(float(x), 6) for x in v]
            path.write_text(json.dumps(cache), encoding="utf-8")
            return dict((q, cache[_key(q, model, dim)]) for q in questions)
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
        api = os.environ.get("OPENAI_API_KEY")
        if not api:
            raise SystemExit(
                "[eval] %d question(s) have no cached vector for %s@%d and "
                "OPENAI_API_KEY is not set. Run this where the key exists (the "
                "eval workflow) to refresh eval/qvec.json."
                % (len(missing), model, dim))
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


def measure(idx, golden, explain=None, variant="current"):
    qs = [g["q"] for g in golden]
    qvecs = query_vectors(qs, idx.model, idx.dim)
    per_q = []
    for n, g in enumerate(golden):
        terms = q_terms(g["q"])
        cands = idx.search(qvecs[g["q"]], terms, variant)
        pos = dict((c["uid"], i) for i, c in enumerate(cands))
        expect = [u for u in g["expect"] if u in idx.by_uid]
        dropped = [u for u in g["expect"] if u not in idx.by_uid]
        ranks = sorted(pos.get(u, MISSING) for u in expect)
        cos = idx.cosine_ranks(qvecs[g["q"]], set(expect))
        per_q.append({"q": g["q"], "tier": g["tier"], "expect": expect,
                      "dropped": dropped, "ranks": ranks,
                      "cos": dict((u, cos.get(u, MISSING)) for u in expect)})
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
        # cos = where the expected paper sits by RAW COSINE over the whole
        # index; final = where it sits after askRank and the graph hop. The two
        # separate an embedding problem from a weighting one, and the fixes are
        # nothing alike: a paper 4,000th by cosine was never reachable and no
        # weight change saves it, while a paper 12th by cosine and 300th after
        # the re-rank was found and then discarded. One number for both is how
        # you end up rebuilding an index that was working fine.
        log("\n  missed at 20 (%d)   cos = by cosine alone, final = after the "
            "re-rank and graph hop:" % len(missed))
        for q in missed:
            r = "none" if not q["ranks"] or q["ranks"][0] >= MISSING else str(q["ranks"][0])
            c = min(q.get("cos", {}).values()) if q.get("cos") else MISSING
            cs = "none" if c >= MISSING else str(c)
            log("    [%-8s] cos=%6s final=%6s  %s" % (q["tier"], cs, r, q["q"][:56]))
        buried = [q for q in missed
                  if q.get("cos") and min(q["cos"].values()) < int(C["ASK_RECALL"])]
        if buried:
            log("\n  %d of those were INSIDE the cosine recall set and were lost "
                "by the re-rank, not by the embedding." % len(buried))

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
    ap.add_argument("--no-graph", action="store_true",
                    help="ignore edges.bin -- required when comparing two "
                         "embedders, since the graph is built from one of them")
    ap.add_argument("--docs", default="",
                    help="measure an index in another directory (a bake-off "
                         "build), leaving docs/ untouched")
    ap.add_argument("--variant", default="current", choices=VARIANTS)
    ap.add_argument("--compare", action="store_true",
                    help="score every ranking variant on the same questions")
    args = ap.parse_args()

    golden = json.loads(
        pathlib.Path(args.golden).read_text(encoding="utf-8"))["questions"]
    if args.no_graph:
        global NO_GRAPH
        NO_GRAPH = True
        log("[eval] graph hop DISABLED -- measuring the embedding alone")
    if args.docs:
        # Index() reads the module-level DOCS. Repointing it is how the
        # self-tests already build synthetic corpora, so this needs no new
        # plumbing -- and it keeps the live index strictly read-only.
        global DOCS
        DOCS = pathlib.Path(args.docs)
        log("[eval] measuring the index in %s" % DOCS)
    idx = Index()

    if args.compare:
        # Same index, same questions, same candidate set -- only the ordering
        # differs, so any gap is attributable to the re-rank and nothing else.
        scored = dict((v, summarise(measure(idx, golden, variant=v)))
                      for v in VARIANTS)
        log("")
        log("  %-10s %8s %8s %8s %8s %8s"
            % ("variant", "hit@5", "hit@10", "hit@20", "rec@20", "MRR"))
        log("  " + "-" * 56)
        for v in VARIANTS:
            o = scored[v]["overall"]
            log("  %-10s %8.2f %8.2f %8.2f %8.2f %8.3f"
                % (v, o["hit@5"], o["hit@10"], o["hit@20"],
                   o["recall@20"], o["mrr"]))
        log("")
        log("  hit@20 by tier:")
        for v in VARIANTS:
            log("  %-10s " % v + "   ".join(
                "%-8s %.2f" % (t, b["hit@20"])
                for t, b in scored[v]["tiers"].items() if b))
        log("\n  The candidate set is identical across variants, so these gaps "
            "are the re-rank alone.")
        return

    per_q = measure(idx, golden, explain=args.explain, variant=args.variant)
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
