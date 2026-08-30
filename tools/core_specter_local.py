#!/usr/bin/env python3
"""Compute SPECTER2 vectors locally for the papers S2 has none for.

THE GAP. S2 served embeddings for 207,547 of 230,804 candidates (89.9%);
23,257 have none, because S2 has no record of those papers at all. 9,550 of
them have an abstract from OpenAlex and 13,667 have only a title, so the text
to embed exists -- what is missing is the model run.

THE VECTORS MUST LAND IN THE SAME SPACE, and this is the part that can fail
silently. SPECTER v1 and SPECTER2 are different models; embedding the gap with
v1 and dropping the results into an array of v2 vectors produces a file where
every cosine ACROSS the two groups is meaningless while every cosine WITHIN
each group is fine. Nothing would error. So this uses specter2_base with the
proximity adapter -- the same configuration Semantic Scholar serves -- and
--validate exists to prove it rather than assert it: re-embed papers we
ALREADY have S2 vectors for and compare. A cosine near 1.0 means the pipeline
matches and the outputs can be merged; anything lower means they cannot.

RAW OUTPUT, NOT NORMALISED. S2's vectors have norms around 22, so these are
the unmodified CLS embeddings. Cosine is scale-invariant and would not care,
but anything doing a dot product would, and a file whose halves are scaled
differently is another silent-failure surface.

INPUT FORMAT IS title + [SEP] + abstract, which is what SPECTER2 was trained
on. Papers with no abstract get title alone -- a weaker vector, not a wrong
one, and the `has_abstract` column records which is which.

DEPENDENCIES ARE NOT IN requirements.txt ON PURPOSE. torch and transformers
are ~2.5 GB and every scheduled workflow installs that file on every run.
This is an opt-in local tool, so it imports them lazily and tells you what to
install if they are absent.

    pip install torch transformers adapters
    python tools/core_specter_local.py --validate      # prove the space matches
    python tools/core_specter_local.py                 # embed the missing
"""

import argparse
import csv
import io
import json
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from progress import Progress                                 # noqa: E402

OUT = pathlib.Path("export")
MASTER_CSV = OUT / "core_master.csv"
MASTER_ND = OUT / "core_master.ndjson"
S2_VEC = OUT / "core_s2_specter.npy"
S2_UIDS = OUT / "core_s2_specter_uids.json"
DEST_VEC = OUT / "core_specter_local.npy"
DEST_UIDS = OUT / "core_specter_local_uids.json"

MODEL = "allenai/specter2_base"
ADAPTER = "allenai/specter2"
MAX_LEN = 512


def log(m):
    print(m, flush=True)


def _load_model():
    """Lazy, with an install message rather than a traceback."""
    try:
        import torch                                          # noqa: PLC0415
        from transformers import AutoTokenizer                # noqa: PLC0415
    except ImportError:
        raise SystemExit(
            "[spec] torch/transformers not installed. This is an opt-in local "
            "tool and its dependencies are deliberately NOT in "
            "requirements.txt -- that file is installed by every scheduled "
            "workflow and these are ~2.5 GB.\n"
            "    pip install torch transformers adapters")
    try:
        from adapters import AutoAdapterModel                 # noqa: PLC0415
    except ImportError:
        raise SystemExit(
            "[spec] the `adapters` package is required for SPECTER2 -- the "
            "proximity adapter is what makes it specter2 rather than plain "
            "SciBERT, and without it the vectors would NOT match the ones S2 "
            "served.\n    pip install adapters")

    log(f"[spec] loading {MODEL} + {ADAPTER} (first run downloads ~440 MB)")
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoAdapterModel.from_pretrained(MODEL)
    model.load_adapter(ADAPTER, source="hf", load_as="proximity",
                       set_active=True)
    model.eval()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)
    log(f"[spec] device: {dev}")
    return torch, tok, model, dev


def _texts(rows, abstracts):
    """title + [SEP] + abstract, SPECTER2's training format."""
    out = []
    for r in rows:
        t = (r.get("title") or "").strip()
        a = (abstracts.get(r["uid"]) or "").strip()
        out.append((t + " [SEP] " + a) if a else t)
    return out


def _embed(rows, abstracts, batch=16):
    torch, tok, model, dev = _load_model()
    texts = _texts(rows, abstracts)
    vecs = np.zeros((len(rows), 768), dtype=np.float32)
    prog = Progress((len(rows) + batch - 1) // batch, "spec", every_s=30)
    with torch.no_grad():
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            enc = tok(chunk, padding=True, truncation=True,
                      max_length=MAX_LEN, return_tensors="pt").to(dev)
            out = model(**enc)
            # CLS token, raw -- matching how S2 serves specter_v2
            v = out.last_hidden_state[:, 0, :].cpu().numpy()
            vecs[i:i + len(chunk)] = v
            prog.tick()
    prog.done()
    return vecs


def _abstracts(uids_wanted):
    """Pull abstracts for just these uids out of the master ndjson."""
    want = set(uids_wanted)
    got = {}
    if not MASTER_ND.exists():
        log(f"[spec] !! {MASTER_ND} missing -- embedding titles only, which "
            f"produces weaker vectors for the 41% that do have an abstract")
        return got
    with io.open(MASTER_ND, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:                                 # noqa: BLE001
                continue
            if r["uid"] in want and (r.get("abstract") or "").strip():
                got[r["uid"]] = r["abstract"]
    return got


def cmd_validate(args):
    """Re-embed papers S2 already covered, and compare.

    This is the whole safety argument for the tool. If the local pipeline
    reproduces S2's vectors, the two sets share a space and can be merged; if
    it does not, merging them would corrupt every cross-group similarity while
    leaving within-group ones intact -- an error that shows up as subtly bad
    search results months later, never as a stack trace.
    """
    if not (S2_VEC.exists() and S2_UIDS.exists()):
        raise SystemExit(f"[spec] {S2_VEC} missing -- nothing to validate against")
    uids = json.load(io.open(S2_UIDS, encoding="utf-8"))
    V = np.load(S2_VEC)
    idx = {u: i for i, u in enumerate(uids)}
    rows = [r for r in csv.DictReader(io.open(MASTER_CSV, encoding="utf-8",
                                              newline=""))
            if r["uid"] in idx and r.get("title")]
    import random
    random.seed(11)
    sample = random.sample(rows, min(args.n, len(rows)))
    log(f"[spec] validating on {len(sample)} papers S2 already embedded")
    abstracts = _abstracts([r["uid"] for r in sample])
    log(f"[spec] {len(abstracts)} of them have an abstract")
    mine = _embed(sample, abstracts, batch=args.batch)

    sims = []
    for r, v in zip(sample, mine):
        ref = V[idx[r["uid"]]]
        d = float(np.dot(v, ref) /
                  (np.linalg.norm(v) * np.linalg.norm(ref) + 1e-9))
        sims.append(d)
    sims = np.array(sims)
    log(f"\n[spec] cosine vs S2's own vectors:")
    log(f"    mean {sims.mean():.4f} · median {np.median(sims):.4f} · "
        f"min {sims.min():.4f} · max {sims.max():.4f}")
    log(f"    >=0.99: {(sims >= 0.99).sum()}/{len(sims)} · "
        f">=0.95: {(sims >= 0.95).sum()}/{len(sims)}")
    if sims.mean() >= 0.95:
        log(f"\n[spec] PASS -- the local pipeline reproduces S2's space. "
            f"Local vectors can be merged with the S2 set.")
        return 0
    log(f"\n[spec] !! FAIL -- mean cosine {sims.mean():.3f}. These vectors are "
        f"NOT in S2's space and must NOT be merged with them: every "
        f"cross-group similarity would be meaningless while within-group ones "
        f"stayed fine, so nothing would look broken. Check the model/adapter "
        f"and the title [SEP] abstract input format before proceeding.")
    return 1


def cmd_embed(args):
    if not MASTER_CSV.exists():
        raise SystemExit(f"[spec] {MASTER_CSV} missing -- run core_master.py")
    have = set()
    if S2_UIDS.exists():
        have = set(json.load(io.open(S2_UIDS, encoding="utf-8")))
    done = set()
    if DEST_UIDS.exists():
        done = set(json.load(io.open(DEST_UIDS, encoding="utf-8")))

    rows = [r for r in csv.DictReader(io.open(MASTER_CSV, encoding="utf-8",
                                              newline=""))
            if r["uid"] not in have and r["uid"] not in done
            and (r.get("title") or "").strip()]
    if args.limit:
        rows = rows[:args.limit]
    log(f"[spec] {len(have):,} already have an S2 vector; {len(done):,} "
        f"computed locally; {len(rows):,} to embed now")
    if not rows:
        log("[spec] nothing to do")
        return 0

    abstracts = _abstracts([r["uid"] for r in rows])
    log(f"[spec] {len(abstracts):,} of them have an abstract "
        f"({100*len(abstracts)/len(rows):.1f}%); the rest embed on title alone")

    t0 = time.time()
    vecs = _embed(rows, abstracts, batch=args.batch)
    log(f"[spec] embedded {len(rows):,} in {(time.time()-t0)/60:.1f} min")

    new_uids = [r["uid"] for r in rows]
    if DEST_VEC.exists() and done:
        old = np.load(DEST_VEC)
        old_uids = json.load(io.open(DEST_UIDS, encoding="utf-8"))
        vecs = np.vstack([old, vecs])
        new_uids = old_uids + new_uids
    np.save(DEST_VEC, vecs)
    DEST_UIDS.write_text(json.dumps(new_uids), encoding="utf-8")
    log(f"[spec] {len(new_uids):,} local vectors -> {DEST_VEC} "
        f"({vecs.nbytes/1e6:.0f} MB)")
    log(f"[spec] NOT merged into {S2_VEC.name} -- provenance stays separate. "
        f"Run --validate first if you intend to combine them.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true",
                    help="re-embed papers S2 covered and compare cosines")
    ap.add_argument("--n", type=int, default=64,
                    help="how many papers to validate on")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if args.validate:
        return cmd_validate(args)
    return cmd_embed(args)


if __name__ == "__main__":
    sys.exit(main())
