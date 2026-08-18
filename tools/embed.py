#!/usr/bin/env python3
"""Semantic index over the whole archive: embed every paper once, ship a compact
int8 matrix the portal can search entirely in the browser.

Why int8 at 256 dims: 5.8k papers x 1536 dims x 4 bytes = 36 MB, far too heavy
for a static page. text-embedding-3-small supports Matryoshka truncation, so we
ask for 256 dims and quantise each unit vector to int8 -> ~1.5 MB. Cosine on
unit vectors is just a dot product, and int8 rounding is monotonic, so ranking
quality is essentially unchanged while the file becomes shippable.

Vectors are cached in state.db keyed by (uid, model), so a paper is embedded
exactly ONCE ever -- a re-run only pays for genuinely new items (fractions of a
cent). The full 5.8k-item cold build costs roughly $0.03.

Outputs:
  docs/vec.bin   int8 matrix, row-major, n x DIM (no header -- pure payload)
  docs/vec.json  {model, dim, n, uids:[...]} -- row i of vec.bin is uids[i]

Usage:  python tools/embed.py            (incremental; embeds only what's new)
        python tools/embed.py --rebuild  (re-embed everything from scratch)
"""

import json
import os
import pathlib
import sqlite3
import struct
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import scoring  # noqa: E402  (repo root on path above)
import store    # noqa: E402

MODEL = "text-embedding-3-small"
DIM = 256                    # Matryoshka truncation -- 6x smaller than native
BATCH = 32                   # inputs per call; small enough for a low-tier TPM cap
MAX_CHARS = 1600             # per paper; abstracts past this add little signal
_URL = "https://api.openai.com/v1/embeddings"

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    uid   TEXT NOT NULL,
    model TEXT NOT NULL,
    dim   INTEGER NOT NULL,
    vec   BLOB NOT NULL,          -- DIM int8 values
    PRIMARY KEY (uid, model, dim)
);
"""


def log(m: str) -> None:
    print(m, flush=True)


def _text(m: dict, title: str) -> str:
    """What we actually embed: title carries the topic, the abstract carries the
    method and findings. Falls back through abstract -> LLM summary -> title."""
    body = (m.get("abstract") or "").strip() or (m.get("summary") or "").strip()
    topic = (m.get("topic") or "").strip()
    parts = [title or m.get("title", "")]
    if topic:
        parts.append(topic)
    if body:
        parts.append(body)
    return " \n".join(p for p in parts if p)[:MAX_CHARS]


def _quantise(vec: list[float]) -> bytes:
    """Unit-normalise then map to int8. Cosine over unit vectors is a dot
    product, so the client can rank with plain integer dot products."""
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    out = bytearray(len(vec))
    for i, v in enumerate(vec):
        q = int(round((v / norm) * 127.0))
        out[i] = struct.pack("b", max(-127, min(127, q)))[0]
    return bytes(out)


def _embed_batch(texts: list[str], key: str) -> list[list[float]]:
    last = ""
    for attempt in range(6):
        r = requests.post(
            _URL,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": MODEL, "input": texts, "dimensions": DIM},
            timeout=90,
        )
        if r.status_code == 429:
            last = r.text[:400].replace("\n", " ")
            # a spent balance also returns 429, but retrying it is pointless --
            # it never clears within a run. Fail loudly instead of burning the
            # whole backoff ladder and reporting a misleading "rate limit".
            if "insufficient_quota" in last or "exceeded your current quota" in last:
                raise RuntimeError(
                    "OpenAI rejected the request for QUOTA/BILLING, not rate "
                    f"limiting -- add credit or use a different key. API said: {last}")
            wait = min(60, 5 * (attempt + 1))
            log(f"  rate-limited (429): {last[:200]}")
            log(f"  retrying in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code in (500, 502, 503):
            last = r.text[:200].replace("\n", " ")
            time.sleep(min(30, 4 * (attempt + 1)))
            continue
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]
    raise RuntimeError(f"embeddings API kept failing after retries. Last response: {last}")


def main() -> None:
    rebuild = "--rebuild" in sys.argv
    key = os.environ.get("OPENAI_API_KEY")
    con = store.connect()
    con.executescript(_CACHE_SCHEMA)
    if rebuild:
        con.execute("DELETE FROM embeddings WHERE model=? AND dim=?", (MODEL, DIM))
        con.commit()

    rows = con.execute(
        "SELECT uid, title, meta FROM items ORDER BY first_seen DESC, uid"
    ).fetchall()
    papers = []
    for uid, title, meta in rows:
        if scoring.is_junk(title):          # same filter the portal export uses
            continue
        try:
            m = json.loads(meta)
        except Exception:                    # noqa: BLE001
            m = {}
        papers.append((uid, _text(m, title)))
    log(f"[embed] {len(papers)} papers in archive")

    cached = {r[0] for r in con.execute(
        "SELECT uid FROM embeddings WHERE model=? AND dim=?", (MODEL, DIM))}
    todo = [(u, t) for u, t in papers if u not in cached and t.strip()]
    log(f"[embed] {len(cached)} cached, {len(todo)} to embed")

    if todo and not key:
        log("[embed] OPENAI_API_KEY not set -- writing index from cache only")
        todo = []

    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        vecs = _embed_batch([t for _, t in chunk], key)
        con.executemany(
            "INSERT OR REPLACE INTO embeddings (uid, model, dim, vec) VALUES (?,?,?,?)",
            [(u, MODEL, DIM, _quantise(v)) for (u, _), v in zip(chunk, vecs)],
        )
        con.commit()
        log(f"[embed] {min(i + BATCH, len(todo))}/{len(todo)}")

    # emit the shipping index, ordered exactly like the .bin rows
    have = dict(con.execute(
        "SELECT uid, vec FROM embeddings WHERE model=? AND dim=?", (MODEL, DIM)))
    uids, blob = [], bytearray()
    for uid, _ in papers:
        v = have.get(uid)
        if v and len(v) == DIM:
            uids.append(uid)
            blob += v

    docs = pathlib.Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "vec.bin").write_bytes(bytes(blob))
    (docs / "vec.json").write_text(json.dumps(
        {"model": MODEL, "dim": DIM, "n": len(uids), "uids": uids}), encoding="utf-8")
    log(f"[embed] wrote docs/vec.bin ({len(blob) / 1e6:.2f} MB, {len(uids)} vectors)")


if __name__ == "__main__":
    main()
