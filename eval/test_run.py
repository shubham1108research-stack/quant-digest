#!/usr/bin/env python3
"""Offline tests for eval/run.py. No API key, no real index, no network.

The eval reports a number, and a number is believed. So the arithmetic behind
it gets tested against hand-computed values on a synthetic index where the
right answer is known by construction -- otherwise the harness could be
silently wrong in the same direction for months and every decision made from it
would inherit the error.

Two things are pinned here specifically:

  * the RANKING PORT. run.py re-implements askRank from portal.py in Python.
    The constants are read from portal.py so they cannot drift, but the FORMULA
    is duplicated. These tests hold it to hand-computed values, so if either
    side changes shape the test fails instead of the metric quietly moving.

  * the METRICS. recall@k, hit@k and MRR are easy to write in a way that looks
    right and is off by one, or that treats a missing paper as rank 0.

    python eval/test_run.py
"""

import json
import pathlib
import struct
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run                                                    # noqa: E402

FAILED = []


def check(name, got, want, tol=1e-9):
    ok = (abs(got - want) <= tol) if isinstance(want, float) else (got == want)
    print("  %-58s %s" % (name, "ok" if ok else "FAIL  got=%r want=%r" % (got, want)))
    if not ok:
        FAILED.append(name)


# --------------------------------------------------------------- tokenising
def test_terms():
    print("q_terms / kw_hit")
    # stopwords out, short words out, order preserved, duplicates collapsed
    check("stopwords and 2-letter words dropped",
          run.q_terms("What is the of carry in FX"), ["carry"])
    check("duplicates collapse, order kept",
          run.q_terms("carry trade carry"), ["carry", "trade"])
    check("punctuation is a separator",
          run.q_terms("stock-bond correlation?"), ["stock", "bond", "correlation"])
    check("kw_hit is share of terms present", run.kw_hit(["a1", "b2"], "a1 only"), 0.5)
    check("kw_hit empty text", run.kw_hit(["a1"], ""), 0.0)
    check("kw_hit no terms", run.kw_hit([], "anything"), 0.0)
    # substring matching is the browser's behaviour, warts included: "carry"
    # matches "carrying". Pinned so a change to it is a decision, not a drift.
    check("kw_hit matches substrings", run.kw_hit(["carry"], "carrying costs"), 1.0)


# ------------------------------------------------------------------ quality
def test_quality():
    print("strength / ask_quality")
    check("unscored papers sit at 0.45", run.ask_quality({}), 0.45)
    # g=3,t=3,np=1 -> (3+3)/6*50 + 1*50 = 100
    check("strength maxes at 100",
          run.strength({"generality": 3, "testability": 3, "novelty_posterior": 1}), 100)
    check("scored paper uses strength/100",
          run.ask_quality({"generality": 3, "testability": 3, "novelty_posterior": 1}), 1.0)
    check("reputation multiplies but clamps at 1.2",
          run.ask_quality({"generality": 3, "testability": 3,
                           "novelty_posterior": 1, "reputation": 2.0}), 1.2)
    # a zero-scored paper is scored, so it gets 0.0 and NOT the 0.45 default
    check("explicitly zero-scored is not treated as unscored",
          run.ask_quality({"generality": 0, "testability": 0, "novelty_posterior": 0}), 0.0)


# ----------------------------------------------------------------- ask_rank
def test_ask_rank():
    print("ask_rank (hand-computed against portal.py weights)")
    w_sim, w_kw, w_q = run.C["W_SIM"], run.C["W_KW"], run.C["W_QUALITY"]
    item = {"title": "carry", "summary": "", "generality": 3,
            "testability": 3, "novelty_posterior": 1}
    # sim=127 -> 1.0 ; kw = 0.6*1 + 0.4*0 = 0.6 ; quality = 1.0
    check("full sim, title-only keyword, top quality",
          run.ask_rank(item, ["carry"], 127.0), w_sim * 1.0 + w_kw * 0.6 + w_q * 1.0)
    # the /127 not /127^2 bug: sim must contribute its full weight at 127
    check("sim is divided by 127, not 127 squared",
          run.ask_rank({"title": "", "summary": ""}, [], 127.0) - w_q * 0.45, w_sim)
    check("negative similarity clamps to zero",
          run.ask_rank({"title": "", "summary": ""}, [], -50.0), w_q * 0.45)
    check("summary carries 0.4 of the keyword weight",
          run.ask_rank({"title": "", "summary": "carry"}, ["carry"], 0.0),
          w_kw * 0.4 + w_q * 0.45)


# ------------------------------------------------------------------ metrics
def test_metrics():
    print("recall@k / hit@k / MRR")
    M = run.MISSING
    per_q = [
        {"q": "a", "tier": "abstract", "expect": ["u1", "u2"], "ranks": [0, 30], "dropped": []},
        {"q": "b", "tier": "abstract", "expect": ["u3"], "ranks": [M], "dropped": []},
        {"q": "c", "tier": "vocab", "expect": ["u4"], "ranks": [4], "dropped": []},
    ]
    s = run.summarise(per_q)
    o = s["overall"]
    # q_a: 1 of 2 expected inside 20 -> 0.5 ; q_b: 0 ; q_c: 1 -> mean 0.5
    check("recall@20 averages the SHARE found per question", o["recall@20"], 0.5)
    # hit@20: a yes, b no, c yes -> 2/3
    check("hit@20 is any-expected-found", o["hit@20"], round(2 / 3, 4))
    # hit@5 boundary: rank 4 is inside top-5, rank 0 is too
    check("hit@5 includes rank 4 (0-based, k exclusive)", o["hit@5"], round(2 / 3, 4))
    # MRR: 1/(0+1) + 0 + 1/(4+1) = 1.2 over 3 = 0.4
    check("MRR uses 1/(rank+1) and scores a miss as 0", o["mrr"], 0.4)
    check("a missing paper is not treated as rank 0",
          run.summarise([{"q": "x", "tier": "t", "expect": ["u"], "ranks": [M],
                          "dropped": []}])["overall"]["mrr"], 0.0)
    check("tiers are split out", sorted(s["tiers"].keys()), ["abstract", "vocab"])
    check("tier n counts", s["tiers"]["abstract"]["n"], 2)


# ------------------------------------------------- the index and the funnel
def _write_index(d, vecs, uids, items, edges=None):
    """Build a synthetic docs/ that Index can load."""
    dim = len(vecs[0])
    d.mkdir(parents=True, exist_ok=True)
    blob = bytearray()
    for v in vecs:
        n = sum(x * x for x in v) ** 0.5 or 1.0
        for x in v:
            blob += struct.pack("b", max(-127, min(127, int(round(x / n * 127)))))
    (d / "vec.bin").write_bytes(bytes(blob))
    (d / "vec.json").write_text(json.dumps(
        {"model": "test-embed", "dim": dim, "n": len(uids), "shard": 64,
         "uids": uids}), encoding="utf-8")
    (d / "archive.json").write_text(json.dumps(items), encoding="utf-8")
    if edges is not None:
        recs = bytearray()
        for a, b, kind, w in edges:
            recs += struct.pack("<HHBB", a, b, kind, w)
        (d / "edges.bin").write_bytes(
            b"QDG1" + struct.pack("<IIB3x", len(uids), len(edges), 2) + bytes(recs))


def test_index():
    print("Index.search (synthetic corpus, answer known by construction)")
    tmp = pathlib.Path(tempfile.mkdtemp())
    old = run.DOCS
    try:
        # three orthogonal directions; row 0 points exactly at the query
        vecs = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        uids = ["u0", "u1", "u2"]
        items = [{"uid": "u0", "title": "target", "summary": ""},
                 {"uid": "u1", "title": "other", "summary": ""},
                 {"uid": "u2", "title": "third", "summary": ""}]
        _write_index(tmp, vecs, uids, items)
        run.DOCS = tmp
        idx = run.Index()
        cands = idx.search([1.0, 0.0, 0.0], [])
        check("the aligned row ranks first", cands[0]["uid"], "u0")
        check("all rows returned when under ASK_RECALL", len(cands), 3)
        check("no graph file means no graph hop",
              any(c["via_graph"] for c in cands), False)

        # unverified papers are excluded from retrieval entirely
        items2 = [dict(items[0], unverified=True)] + items[1:]
        _write_index(tmp, vecs, uids, items2)
        idx = run.Index()
        check("unverified papers are dropped",
              [c["uid"] for c in idx.search([1.0, 0.0, 0.0], [])], ["u1", "u2"])

        # duplicate titles collapse, keeping the better-scoring row
        items3 = [dict(items[0]), dict(items[1], title="target"), dict(items[2])]
        _write_index(tmp, vecs, uids, items3)
        idx = run.Index()
        got = [c["uid"] for c in idx.search([1.0, 0.0, 0.0], [])]
        check("same-title duplicates collapse to the first seen", got, ["u0", "u2"])

        # GRAPH HOP. This needs a corpus BIGGER than ASK_RECALL to mean
        # anything: with three rows everything is already a candidate, the hop
        # has nothing left to add, and a test asserting it "worked" would pass
        # while exercising none of it. So: 600 rows, and the paper we want is
        # deliberately the single worst match for the query -- unreachable by
        # cosine, reachable only by being a neighbour of the top hit. That is
        # exactly the case the graph exists for.
        n = int(run.C["ASK_RECALL"]) + 100
        far = n - 1
        vecs2 = [[1.0, 0.0, 0.0]]
        for i in range(1, n):
            vecs2.append([0.5, 1.0, 0.0])          # middling, all identical
        vecs2[far] = [-1.0, 0.0, 0.0]              # anti-aligned: ranks last
        uids2 = ["u%d" % i for i in range(n)]
        items2 = [{"uid": u, "title": "t%d" % i, "summary": ""}
                  for i, u in enumerate(uids2)]

        _write_index(tmp, vecs2, uids2, items2)     # no edges yet
        idx = run.Index()
        base = idx.search([1.0, 0.0, 0.0], [])
        check("without a graph, the anti-aligned paper is unreachable",
              far in set(c["row"] for c in base), False)

        _write_index(tmp, vecs2, uids2, items2, edges=[(0, far, 0, 255)])
        idx = run.Index()
        cands = idx.search([1.0, 0.0, 0.0], [])
        pulled = [c for c in cands if c["row"] == far]
        check("the graph reaches a paper cosine ranked dead last",
              len(pulled), 1)
        check("and marks it as arriving via the graph",
              pulled[0]["via_graph"] if pulled else False, True)
        check("no duplicate rows after the hop",
              len(set(c["row"] for c in cands)), len(cands))
        check("candidates stay sorted by rank after the hop",
              all(cands[i]["rank"] >= cands[i + 1]["rank"]
                  for i in range(len(cands) - 1)), True)

        # an edge to a row the manifest does not have must not crash or invent
        _write_index(tmp, vecs2, uids2, items2, edges=[(0, n + 500, 0, 255)])
        idx = run.Index()
        check("an out-of-range edge target is ignored",
              max(c["row"] for c in idx.search([1.0, 0.0, 0.0], [])) < n, True)

        # a manifest that disagrees with the buffer must fail, not clamp
        (tmp / "vec.json").write_text(json.dumps(
            {"model": "test-embed", "dim": 3, "n": 9, "shard": 64,
             "uids": ["u0", "u1", "u2", "u3"]}), encoding="utf-8")
        try:
            run.Index()
            check("manifest/buffer mismatch raises", False, True)
        except SystemExit:
            check("manifest/buffer mismatch raises", True, True)
    finally:
        run.DOCS = old


def test_variants():
    print("ranking variants")
    def mk(sim, mass=0.0, title="", summary="", **kw):
        it = {"uid": "u", "title": title, "summary": summary}
        it.update(kw)
        return {"uid": "u", "row": 0, "it": it, "sim": sim, "mass": mass,
                "via_graph": mass > 0}

    # sim_only must order by similarity and nothing else -- the paper with the
    # better cosine wins even when the other one matches every query word.
    cands = [mk(10.0, title="carry carry"), mk(120.0, title="unrelated")]
    run._rescore(cands, ["carry"], "sim_only")
    check("sim_only ignores keywords", cands[0]["sim"], 120.0)

    # THE BUG THE VARIANTS EXIST FOR. Realistic cosines: 80/127 = 0.63 is a
    # strong match on this corpus, 40/127 = 0.31 a weak one. Under `current`
    # the weak match wins on keyword overlap alone; under minmax it does not.
    good = mk(80.0, title="regime switching in equity returns")
    noisy = mk(40.0, title="carry trade momentum value")
    terms = ["carry", "trade", "momentum", "value"]
    pair = [dict(good), dict(noisy)]
    run._rescore(pair, terms, "legacy")
    check("legacy: keyword overlap beats a much better cosine",
          pair[0]["sim"], 40.0)
    pair = [dict(good), dict(noisy)]
    run._rescore(pair, terms, "minmax")
    check("current: the better cosine wins instead", pair[0]["sim"], 80.0)

    # every candidate having the same similarity must not divide by zero
    flat = [mk(50.0, title="a"), mk(50.0, title="b")]
    run._rescore(flat, ["a"], "current")
    check("current survives a zero-range candidate set", len(flat), 2)

    # rrf: ranks only, so a huge numeric gap in one list cannot swamp the rest
    cands = [mk(127.0, title="zzz"), mk(126.0, title="carry"), mk(1.0, title="carry")]
    run._rescore(cands, ["carry"], "rrf")
    check("rrf produces a total order", len(set(id(c) for c in cands)), 3)
    check("rrf sorts descending",
          all(cands[i]["rank"] >= cands[i + 1]["rank"] for i in range(len(cands) - 1)),
          True)

    try:
        run._rescore([mk(1.0)], [], "nonsense")
        check("an unknown variant is fatal", False, True)
    except SystemExit:
        check("an unknown variant is fatal", True, True)
    check("empty candidate set is a no-op", run._rescore([], [], "rrf"), None)


def test_constants_are_live():
    print("constants come from portal.py, not from a copy")
    src = (run.ROOT / "portal.py").read_text(encoding="utf-8")
    check("W_SIM in run.C matches portal.py text",
          ("W_SIM=%s" % run.C["W_SIM"]) in src.replace(" ", ""), True)
    try:
        run.js_consts("NO_SUCH_CONSTANT_XYZ")
        check("a missing constant is fatal", False, True)
    except SystemExit:
        check("a missing constant is fatal", True, True)


if __name__ == "__main__":
    for fn in (test_terms, test_quality, test_ask_rank, test_metrics,
               test_index, test_variants, test_constants_are_live):
        fn()
    print()
    if FAILED:
        print("FAILED: %d" % len(FAILED))
        for f in FAILED:
            print("  " + f)
        sys.exit(1)
    print("all eval self-tests passed")
