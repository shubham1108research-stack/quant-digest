#!/usr/bin/env python3
"""Tag the untagged core candidates by SPECTER similarity, and let the
un-nameable clusters propose the terms the vocabulary is missing.

THE PROBLEM THIS SOLVES. Route A tags a paper by matching its TITLE against the
299-term taxonomy. That leaves 13,412 candidates with no tag at all, and they
are not marginal papers -- the untagged set contains "Returns to Buying Winners
and Selling Losers" (the momentum paper, whose title never says momentum),
"Common risk factors in the returns on stocks and bonds", "The Pricing of
Options and Corporate Liabilities" and "Prospect Theory". A vocabulary of exact
phrases cannot reach a paper that describes its subject in other words, and no
amount of adding terms fixes the general case.

WHY SPECTER RATHER THAN A KEYWORD. SPECTER v2 is trained on citation
proximity, so two papers land near each other when the literature treats them
as related, whatever words they use. Semantic Scholar serves the vectors free
on the batch endpoint, 500 ids per request, computed from THEIR copy of the
title and abstract -- so a candidate we hold only a title for still gets a
vector built from the full record.

METHOD. Build one centroid per taxonomy term from papers that term already
tagged, then assign each untagged paper to its nearest centroid. Validation is
built in and runs first: hold out papers whose tag IS known, hide it, and see
how often the centroid recovers it. A method that cannot recover a known label
has no business assigning an unknown one, and the number is printed before
anything is written.

AND THE PART THAT FEEDS BACK. An untagged paper whose nearest centroid is
still far away is not a labelling failure -- it is a SUBJECT THE VOCABULARY
CANNOT NAME. Those papers cluster among themselves, and the frequent phrases
inside a cluster are a proposed term, grounded in papers we actually hold
rather than in what someone thought to write down.

    python tools/core_specter.py fetch      # embeddings -> export/
    python tools/core_specter.py assign     # validate, then tag the untagged
    python tools/core_specter.py propose    # name the un-nameable clusters
"""

import argparse
import collections
import csv
import io
import json
import os
import pathlib
import re
import sys
import time

import numpy as np
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from progress import Progress                              # noqa: E402

OUT = pathlib.Path("export")
CAND = OUT / "core_candidates.csv"
VEC = OUT / "core_specter.npy"
IDS = OUT / "core_specter_uids.json"
S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
UA = "quant-digest/1.0"
DIM = 768
# Papers per term used to build a centroid. More is not better: a term with 20
# prototypes is described by its core, one with 500 drags in everything the
# term co-occurs with.
PROTO_PER_TERM = 25


def log(m):
    print(m, flush=True)


def _s2id(uid):
    """A core uid -> the id form S2 accepts, or None if it cannot take it."""
    if uid.startswith("doi:"):
        return "DOI:" + uid[4:]
    if uid.startswith("arxiv:"):
        return "ARXIV:" + uid[6:]
    return None                      # oa:, sig:, t: -- S2 has no handle for these


def _rows():
    if not CAND.exists():
        log(f"[specter] {CAND} missing -- build the core list first")
        sys.exit(2)
    return list(csv.DictReader(io.open(CAND, encoding="utf-8", newline="")))


def _targets(rows):
    """(untagged papers, prototype papers) -- both need vectors."""
    untagged = [r for r in rows if not (r.get("tag") or "").strip()
                and _s2id(r["uid"])]
    by_term = collections.defaultdict(list)
    for r in rows:
        t = (r.get("tag") or "").strip()
        if t and _s2id(r["uid"]):
            by_term[t].append(r)
    protos = []
    for t, rs in by_term.items():
        # Highest-scoring papers for the term describe it best; a random draw
        # would define "momentum" with whatever happened to mention it once.
        rs.sort(key=lambda r: -float(r.get("score") or 0))
        protos.extend(rs[:PROTO_PER_TERM])
    return untagged, protos


def cmd_fetch(args):
    rows = _rows()
    untagged, protos = _targets(rows)
    want = {r["uid"]: r for r in untagged + protos}
    log(f"[specter] {len(untagged):,} untagged + {len(protos):,} prototypes "
        f"({len(set(r['tag'] for r in protos))} terms) = {len(want):,} vectors")

    have = {}
    if VEC.exists() and IDS.exists():
        old = json.loads(IDS.read_text(encoding="utf-8"))
        arr = np.load(VEC)
        have = {u: arr[i] for i, u in enumerate(old)}
        log(f"[specter] {len(have):,} already cached")
    todo = [u for u in want if u not in have]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        log("[specter] nothing to fetch")
        return 0

    key = os.environ.get("S2_API_KEY", "").strip()
    hdr = {"User-Agent": UA}
    if key:
        hdr["x-api-key"] = key
    pause = 1.1 if key else 3.2
    log(f"[specter] fetching {len(todo):,} in {(len(todo)+499)//500} requests "
        f"({'key set' if key else 'NO KEY -- slow'})")

    prog = Progress((len(todo) + 499) // 500, "specter", every_s=30)
    got = 0
    for i in range(0, len(todo), 500):
        chunk = todo[i:i + 500]
        ids = [_s2id(u) for u in chunk]
        body = None
        for attempt in range(5):
            try:
                rr = requests.post(S2_BATCH, headers=hdr,
                                   params={"fields": "embedding.specter_v2"},
                                   json={"ids": ids}, timeout=120)
            except Exception as e:                          # noqa: BLE001
                log(f"[specter]   {type(e).__name__}; retrying")
                time.sleep(5 * (attempt + 1))
                continue
            if rr.status_code == 429:
                time.sleep(6 * (attempt + 1))
                continue
            if not rr.ok:
                log(f"[specter]   HTTP {rr.status_code}: {rr.text[:120]}")
                break
            body = rr.json()
            break
        if body:
            for uid, rec in zip(chunk, body):
                v = ((rec or {}).get("embedding") or {}).get("vector")
                if v and len(v) == DIM:
                    have[uid] = np.asarray(v, dtype=np.float32)
                    got += 1
        prog.tick()
        # Checkpoint: this is tens of minutes keyless and losing it is rude.
        if (i // 500) % 10 == 9:
            _save(have)
        time.sleep(pause)
    prog.done()
    _save(have)
    log(f"[specter] {got:,} new vectors; {len(have):,} cached "
        f"({100*len(have)/max(1,len(want)):.0f}% of what was wanted)")
    return 0


def _save(have):
    uids = list(have)
    arr = np.vstack([have[u] for u in uids]) if uids else np.zeros((0, DIM), "f4")
    np.save(VEC, arr)
    IDS.write_text(json.dumps(uids), encoding="utf-8")


def _load():
    if not (VEC.exists() and IDS.exists()):
        log("[specter] no vectors -- run `fetch` first")
        sys.exit(2)
    uids = json.loads(IDS.read_text(encoding="utf-8"))
    arr = np.load(VEC).astype(np.float32)
    arr /= (np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9)   # unit -> dot = cosine
    return {u: i for i, u in enumerate(uids)}, arr


def _centroids(rows, ix, arr, exclude=frozenset()):
    """term -> (unit centroid, family, sleeve, n)."""
    acc = collections.defaultdict(list)
    meta = {}
    for r in rows:
        t = (r.get("tag") or "").strip()
        if not t or r["uid"] in exclude or r["uid"] not in ix:
            continue
        acc[t].append(arr[ix[r["uid"]]])
        meta.setdefault(t, (r.get("family", ""), r.get("sleeve", "")))
    out = {}
    for t, vs in acc.items():
        if len(vs) < 3:                 # a centroid of one or two is a point
            continue
        c = np.mean(np.vstack(vs), axis=0)
        c /= (np.linalg.norm(c) + 1e-9)
        out[t] = (c, meta[t][0], meta[t][1], len(vs))
    return out


def cmd_assign(args):
    rows = _rows()
    ix, arr = _load()

    # ---- validation FIRST, on papers whose tag is known -------------------
    tagged = [r for r in rows if (r.get("tag") or "").strip() and r["uid"] in ix]
    rng = np.random.default_rng(7)
    held = set(rng.choice([r["uid"] for r in tagged],
                          size=min(1500, len(tagged)), replace=False).tolist())
    cents = _centroids(rows, ix, arr, exclude=held)
    terms = list(cents)
    M = np.vstack([cents[t][0] for t in terms])
    hit = tot = 0
    sleeve_hit = 0
    for r in tagged:
        if r["uid"] not in held:
            continue
        sims = M @ arr[ix[r["uid"]]]
        pred = terms[int(np.argmax(sims))]
        tot += 1
        hit += pred == (r.get("tag") or "").strip()
        sleeve_hit += cents[pred][2] == (r.get("sleeve") or "")
    log(f"[specter] VALIDATION on {tot:,} held-out tagged papers "
        f"({len(terms)} centroids)")
    log(f"[specter]   exact term recovered : {hit:,} ({100*hit/max(1,tot):.1f}%)")
    log(f"[specter]   sleeve recovered     : {sleeve_hit:,} "
        f"({100*sleeve_hit/max(1,tot):.1f}%)")
    if 100 * sleeve_hit / max(1, tot) < args.min_sleeve_acc:
        log(f"[specter] sleeve accuracy below --min-sleeve-acc "
            f"{args.min_sleeve_acc}%; NOT writing assignments")
        return 1

    # ---- assign the untagged ---------------------------------------------
    cents = _centroids(rows, ix, arr)
    terms = list(cents)
    M = np.vstack([cents[t][0] for t in terms])
    out, far = [], 0
    for r in rows:
        if (r.get("tag") or "").strip() or r["uid"] not in ix:
            continue
        sims = M @ arr[ix[r["uid"]]]
        j = int(np.argmax(sims))
        s = float(sims[j])
        rec = {"uid": r["uid"], "title": r["title"], "sim": round(s, 4),
               "tag": terms[j], "family": cents[terms[j]][1],
               "sleeve": cents[terms[j]][2]}
        if s < args.min_sim:
            # NOT a labelling failure -- a subject the vocabulary cannot name.
            rec["tag"] = rec["family"] = rec["sleeve"] = ""
            rec["unnameable"] = True
            far += 1
        out.append(rec)
    (OUT / "core_specter_tags.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    named = len(out) - far
    log(f"[specter] {named:,} untagged papers assigned a term "
        f"(cosine >= {args.min_sim})")
    log(f"[specter] {far:,} left unnamed -- these are the vocabulary gaps, "
        f"see `propose`")
    log(f"[specter] by sleeve: {dict(collections.Counter(r['sleeve'] for r in out if r['sleeve']).most_common())}")
    log(f"[specter] written to {OUT}/core_specter_tags.json -- nothing applied")
    return 0


def cmd_propose(args):
    """Name the clusters the vocabulary could not reach."""
    p = OUT / "core_specter_tags.json"
    if not p.exists():
        log("[specter] run `assign` first")
        return 2
    recs = [r for r in json.loads(p.read_text(encoding="utf-8"))
            if r.get("unnameable")]
    if not recs:
        log("[specter] nothing unnameable -- vocabulary covers the corpus")
        return 0
    ix, arr = _load()
    V = np.vstack([arr[ix[r["uid"]]] for r in recs if r["uid"] in ix])
    keep = [r for r in recs if r["uid"] in ix]
    k = args.clusters
    rng = np.random.default_rng(11)
    C = V[rng.choice(len(V), size=min(k, len(V)), replace=False)]
    for _ in range(25):                               # k-means, cosine
        lab = np.argmax(V @ C.T, axis=1)
        for j in range(len(C)):
            m = V[lab == j]
            if len(m):
                C[j] = m.mean(axis=0) / (np.linalg.norm(m.mean(axis=0)) + 1e-9)
    tax = {r["term"].strip().lower()
           for r in csv.DictReader(io.open(OUT / "core_tags.csv",
                                           encoding="utf-8"))}
    STOP = set("the a an of and or in on for to with from by is are as at we "
               "this that its their new evidence using use does do it more "
               "than what how why can be has have not but a".split())
    log(f"[specter] {len(keep):,} unnameable papers -> {k} clusters\n")
    for j in range(len(C)):
        members = [keep[i] for i in range(len(keep)) if lab[i] == j]
        if len(members) < args.min_cluster:
            continue
        c = collections.Counter()
        for m in members:
            w = [x for x in re.sub(r"[^a-z0-9 ]", " ", m["title"].lower()).split()
                 if x not in STOP and len(x) > 2]
            for n in (2, 3):
                for i in range(len(w) - n + 1):
                    g = " ".join(w[i:i + n])
                    if g not in tax:
                        c[g] += 1
        phrases = [g for g, n in c.most_common(4) if n >= 3]
        log(f"  cluster {j:>2}  n={len(members):>5}  proposed: "
            f"{', '.join(phrases) if phrases else '(no repeated phrase)'}")
        for m in sorted(members, key=lambda r: -r["sim"])[:2]:
            log(f"              e.g. {m['title'][:66]}")
    log(f"\n[specter] phrases above are CANDIDATES -- validate with "
        f"core_tags.py before adding")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--limit", type=int, default=0)
    a = sub.add_parser("assign")
    a.add_argument("--min-sim", type=float, default=0.72,
                   help="below this the nearest centroid is not a real match")
    a.add_argument("--min-sleeve-acc", type=float, default=60.0,
                   help="refuse to write assignments below this validation %%")
    pr = sub.add_parser("propose")
    pr.add_argument("--clusters", type=int, default=25)
    pr.add_argument("--min-cluster", type=int, default=20)
    args = ap.parse_args()
    return {"fetch": cmd_fetch, "assign": cmd_assign,
            "propose": cmd_propose}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
