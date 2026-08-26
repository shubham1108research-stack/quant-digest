#!/usr/bin/env python3
"""Build the semantic index with a LOCAL sentence-transformers model.

A bake-off against tools/embed.py's paid OpenAI embedder, decided by
eval/run.py rather than by argument -- this is exactly the "1.1" question the
retrieval brief raised and the eval was built to settle.

WHY IT MIGHT WIN
Not cost, which is trivial either way: 25,633 papers is about nine cents
through text-embedding-3-small. The reason is that embeddings are the paid
dependency on the path that breaks FIRST. When the OpenAI credits lapse,
embed.py cannot rebuild the index and Ask's mode:"embed" fails outright, so
retrieval stops -- not degrades, stops. A local embedder makes the search
survive an expired card.

BGE models sit around or above text-embedding-3-small on MTEB retrieval, and
bge-small does the whole corpus in about four minutes on a CPU runner. Whether
that holds on THIS corpus with THESE questions is the thing to measure.

WHAT IS HELD CONSTANT
The output is byte-for-byte the same shape as embed.py's -- unit-normalised,
int8 at 127, row-major, with the same manifest -- so eval/run.py cannot tell
the two apart except by the vectors. Same corpus selection, same text
(_text_src), same quantisation. Only the model differs, which is the point.

Writes to a DIRECTORY YOU NAME, never over docs/. A bake-off that clobbers the
live index is not an experiment, it is a deployment.

    python tools/embed_local.py --out docs_bge --model BAAI/bge-small-en-v1.5
    python tools/embed_local.py --out docs_bge --limit 500      # smoke test
"""

import argparse
import hashlib
import json
import pathlib
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import scoring   # noqa: E402
import store     # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import embed as embed_openai   # noqa: E402  -- reuse _text_src and SHARD

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


def log(m):
    print(m, flush=True)


def _quantise(vec) -> bytes:
    """Identical to embed._quantise, deliberately.

    Not imported, because that one takes a list and this one is handed a numpy
    row; but the arithmetic must match exactly or the comparison measures the
    quantiser rather than the model.
    """
    norm = float((vec * vec).sum()) ** 0.5 or 1.0
    out = bytearray(len(vec))
    for i, v in enumerate(vec):
        q = int(round((float(v) / norm) * 127.0))
        out[i] = struct.pack("b", max(-127, min(127, q)))[0]
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True,
                    help="directory to write vec.bin/vec.json into")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args()

    from sentence_transformers import SentenceTransformer   # noqa: PLC0415

    con = store.connect()
    rows = con.execute(
        # ASCENDING first_seen, exactly as embed.py orders it: row assignment
        # has to be append-only or the abs/N.json shards point at the wrong
        # papers, and the eval joins on row order.
        "SELECT uid, title, meta FROM items ORDER BY first_seen ASC, uid"
    ).fetchall()
    papers = []
    for uid, title, meta in rows:
        if scoring.is_junk(title):
            continue
        try:
            m = json.loads(meta)
        except Exception:                              # noqa: BLE001
            m = {}
        if m.get("retired"):
            continue
        text, _src = embed_openai._text_src(m, title)
        if text.strip():
            papers.append((uid, text))
    if args.limit:
        papers = papers[:args.limit]
    log(f"[local] {len(papers):,} papers to embed with {args.model}")

    t0 = time.time()
    model = SentenceTransformer(args.model)
    dim = model.get_sentence_embedding_dimension()
    log(f"[local] model loaded in {time.time()-t0:.0f}s, {dim} dimensions")

    t0 = time.time()
    vecs = model.encode([t for _, t in papers], batch_size=args.batch,
                        show_progress_bar=False, convert_to_numpy=True,
                        normalize_embeddings=False)
    took = time.time() - t0
    log(f"[local] encoded in {took/60:.1f} min "
        f"({len(papers)/max(took,1):.0f} papers/s)")

    uids, blob = [], bytearray()
    for (uid, _t), v in zip(papers, vecs):
        uids.append(uid)
        blob += _quantise(v)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "vec.bin").write_bytes(bytes(blob))
    build = hashlib.sha1("\n".join(uids).encode("utf-8")).hexdigest()[:12]
    (out / "vec.json").write_text(json.dumps(
        {"model": args.model, "dim": dim, "n": len(uids),
         "shard": embed_openai.SHARD, "build": build, "uids": uids}),
        encoding="utf-8")
    log(f"[local] wrote {out}/vec.bin ({len(blob)/1e6:.2f} MB, "
        f"{len(uids):,} rows, build {build})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
