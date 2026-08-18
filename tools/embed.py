#!/usr/bin/env python3
"""Semantic index over the whole archive: embed every paper once, ship a compact
int8 matrix the portal can search entirely in the browser.

Provider is Gemini (gemini-embedding-2) because it is free-tier and supports
Matryoshka truncation -- we ask for 256 dimensions instead of the native 3072.
That matters a lot here: 8.4k papers x 3072 dims x 4 bytes would be ~103 MB,
hopeless for a static page, while 256-dim int8 is ~2 MB. Cosine over unit
vectors is just a dot product and int8 rounding is monotonic, so ranking
quality survives the compression.

Vectors are cached in state.db keyed by (uid, model, dim), so a paper is
embedded exactly ONCE ever -- a re-run only pays for genuinely new items, and
changing model/dim naturally invalidates rather than silently mixing spaces.

Outputs:
  docs/vec.bin   int8 matrix, row-major, n x DIM (no header -- pure payload)
  docs/vec.json  {model, dim, n, uids:[...]} -- row i of vec.bin is uids[i]

Usage:  python tools/embed.py            (incremental; embeds only what's new)
        python tools/embed.py --rebuild  (re-embed everything from scratch)
"""

import json
import os
import pathlib
import struct
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import scoring  # noqa: E402  (repo root on path above)
import store    # noqa: E402

MODEL = "gemini-embedding-2"
DIM = 256                    # Matryoshka truncation (native is 3072)
BATCH = 64                   # contents per batchEmbedContents call
MAX_CHARS = 1600             # per paper; abstracts past this add little signal
PAUSE = 1.0                  # seconds between batches -- stay under free-tier RPM
_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:batchEmbedContents")

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
    """What we actually embed: the title carries the subject, the abstract the
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
    """Unit-normalise then map to int8, so the client can rank with plain
    integer dot products. (Gemini already normalises truncated dims; doing it
    again is cheap and makes the file correct regardless of provider.)"""
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    out = bytearray(len(vec))
    for i, v in enumerate(vec):
        q = int(round((v / norm) * 127.0))
        out[i] = struct.pack("b", max(-127, min(127, q)))[0]
    return bytes(out)


def _embed_batch(texts: list[str], key: str) -> list[list[float]]:
    body = {"requests": [
        {"model": f"models/{MODEL}",
         "content": {"parts": [{"text": t}]},
         "embedContentConfig": {"outputDimensionality": DIM}}
        for t in texts
    ]}
    last = ""
    for attempt in range(6):
        r = requests.post(
            _URL,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body, timeout=120,
        )
        if r.status_code == 429:
            last = r.text[:400].replace("\n", " ")
            if "quota" in last.lower() and "exceeded" in last.lower() \
                    and "per day" in last.lower():
                raise RuntimeError(
                    f"Gemini DAILY quota exhausted -- resume tomorrow (progress "
                    f"is cached in state.db, so nothing is lost). API said: {last}")
            wait = min(90, 10 * (attempt + 1))
            log(f"  rate-limited (429): {last[:180]}")
            log(f"  retrying in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code in (500, 502, 503):
            last = r.text[:200].replace("\n", " ")
            time.sleep(min(30, 5 * (attempt + 1)))
            continue
        if not r.ok:
            raise RuntimeError(f"embeddings HTTP {r.status_code}: "
                               f"{r.text[:400]}")
        out = [e["values"] for e in r.json()["embeddings"]]
        if out and len(out[0]) != DIM:
            raise RuntimeError(
                f"asked for {DIM} dims but got {len(out[0])} -- the "
                f"outputDimensionality field was ignored; check the request shape")
        return out
    raise RuntimeError(f"embeddings API kept failing after retries. Last: {last}")


def main() -> None:
    rebuild = "--rebuild" in sys.argv
    key = os.environ.get("GEMINI_API_KEY")
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
    log(f"[embed] {len(cached)} cached, {len(todo)} to embed ({MODEL} @ {DIM}d)")

    if todo and not key:
        log("[embed] GEMINI_API_KEY not set -- writing index from cache only")
        todo = []

    done = 0
    try:
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            vecs = _embed_batch([t for _, t in chunk], key)
            con.executemany(
                "INSERT OR REPLACE INTO embeddings (uid, model, dim, vec) "
                "VALUES (?,?,?,?)",
                [(u, MODEL, DIM, _quantise(v)) for (u, _), v in zip(chunk, vecs)],
            )
            con.commit()
            done = min(i + BATCH, len(todo))
            if done % (BATCH * 10) == 0 or done == len(todo):
                log(f"[embed] {done}/{len(todo)}")
            time.sleep(PAUSE)
    except Exception as e:                              # noqa: BLE001
        # a daily-quota stop is normal on a cold build: keep what we embedded,
        # write the partial index, and let the next run continue from the cache
        log(f"[embed] stopped early after {done}: {type(e).__name__}: {e}")

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
    pct = (100.0 * len(uids) / len(papers)) if papers else 0.0
    log(f"[embed] wrote docs/vec.bin ({len(blob) / 1e6:.2f} MB, "
        f"{len(uids)}/{len(papers)} papers = {pct:.0f}% coverage)")


if __name__ == "__main__":
    main()
