#!/usr/bin/env python3
"""Semantic index over the whole archive: embed every paper once, ship a compact
int8 matrix the portal can search entirely in the browser.

Provider is Mistral (mistral-embed) -- the free tier that is actually live on
this account (OpenAI has no credits, the Gemini key's service account is
disabled). We request output_dimension=256 but do not trust it: support is
model-dependent, so _probe() measures the real width once and everything keys
off that. Width matters -- 8.4k papers x 1024 dims x 4 bytes is ~34 MB, hopeless
for a static page, while 256-dim int8 is ~2 MB. Cosine over unit vectors is just
a dot product and int8 rounding is monotonic, so ranking survives the squeeze.

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

# Provider is chosen by which key is present, OpenAI first. It is the primary
# now for one concrete reason beyond being paid: Mistral rejects a width
# request, so the index sits at its native 1024 dims and vec.bin is ~9 MB that
# every portal visitor downloads on every visit. OpenAI honours `dimensions`,
# which is what finally makes WANT_DIM below mean something -- roughly a
# quarter the payload for the same recall.
#
# Switching model re-embeds from scratch and that is by design: vectors cache
# on (uid, model, dim), so two vector spaces can never be mixed in one index.
_PROVIDERS = [
    # (env var, model, url, name of the width parameter or None if unsupported)
    ("OPENAI_API_KEY", "text-embedding-3-small",
     "https://api.openai.com/v1/embeddings", "dimensions"),
    ("MISTRAL_API_KEY", "mistral-embed",
     "https://api.mistral.ai/v1/embeddings", "output_dimension"),
]
MODEL = "mistral-embed"      # resolved by _pick_provider() at runtime
_DIM_PARAM = "output_dimension"
WANT_DIM = 256               # requested width; the API may ignore it (see _probe)
DIM = 0                      # actual width, resolved at runtime by _probe()
BATCH = 24                   # inputs per call; keeps a request well inside limits
MAX_CHARS = 1600             # per paper; abstracts past this add little signal
SHARD = 64                   # vector rows per abstract shard (~90 KB each)
PAUSE = 1.0                  # seconds between batches -- stay under free-tier RPM
_URL = "https://api.mistral.ai/v1/embeddings"


def _pick_provider() -> str | None:
    """First provider with a key -> sets MODEL/_URL/_DIM_PARAM. Returns the key."""
    global MODEL, _URL, _DIM_PARAM
    for env, model, url, dim_param in _PROVIDERS:
        key = os.environ.get(env)
        if key:
            MODEL, _URL, _DIM_PARAM = model, url, dim_param
            log(f"[embed] provider {env.split('_')[0].lower()} / {MODEL}")
            return key
    return None

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
    integer dot products. Normalising unconditionally keeps the file correct
    whatever the provider does about normalisation itself."""
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    out = bytearray(len(vec))
    for i, v in enumerate(vec):
        q = int(round((v / norm) * 127.0))
        out[i] = struct.pack("b", max(-127, min(127, q)))[0]
    return bytes(out)


_send_dim = True             # flipped off if the API rejects output_dimension


def _post(texts: list[str], key: str):
    body = {"model": MODEL, "input": texts}
    # the width parameter is named differently per provider, and one of them
    # rejects it outright -- _probe measures the result rather than trusting it
    if _send_dim and _DIM_PARAM:
        body[_DIM_PARAM] = WANT_DIM
    return requests.post(
        _URL,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json=body, timeout=120,
    )


def _probe(key: str) -> int:
    """Resolve the ACTUAL embedding width once, up front. output_dimension is
    model-dependent on Mistral, so rather than assume, ask for the narrow width
    and measure what comes back -- the cache table and the shipped index are
    both keyed on the real number, and the portal reads it from vec.json."""
    global _send_dim
    r = _post(["dimension probe"], key)
    if not r.ok and _send_dim and (_DIM_PARAM in r.text
                                   or r.status_code in (400, 422)):
        log("[embed] output_dimension rejected; falling back to native width")
        _send_dim = False
        r = _post(["dimension probe"], key)
    if not r.ok:
        raise RuntimeError(f"probe failed HTTP {r.status_code}: {r.text[:300]}")
    d = len(r.json()["data"][0]["embedding"])
    log(f"[embed] {MODEL} returns {d}-dim vectors"
        f"{' (truncated as requested)' if _send_dim and d == WANT_DIM else ''}")
    return d


def _embed_batch(texts: list[str], key: str) -> list[list[float]]:
    last = ""
    for attempt in range(6):
        r = _post(texts, key)
        if r.status_code == 429:
            last = r.text[:400].replace("\n", " ")
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
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        out = [d["embedding"] for d in data]
        if out and len(out[0]) != DIM:
            raise RuntimeError(
                f"index is {DIM}-dim but the API returned {len(out[0])} -- "
                f"mixing widths would corrupt retrieval")
        return out
    raise RuntimeError(f"embeddings API kept failing after retries. Last: {last}")


def main() -> None:
    global DIM
    rebuild = "--rebuild" in sys.argv
    key = _pick_provider()
    con = store.connect()
    con.executescript(_CACHE_SCHEMA)

    # resolve the real vector width BEFORE touching the cache -- every cache
    # lookup and the shipped index are keyed on it
    if key:
        DIM = _probe(key)
    else:
        row = con.execute("SELECT dim FROM embeddings WHERE model=? LIMIT 1",
                          (MODEL,)).fetchone()
        DIM = row[0] if row else WANT_DIM

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
        # retired by tools/prune.py. This is where it pays: every retired paper
        # is a vector the browser downloads, a node on the map and a candidate
        # in Ask's recall set, for a paper nothing will ever show.
        if m.get("retired"):
            continue
        papers.append((uid, _text(m, title)))
    log(f"[embed] {len(papers)} papers in archive")

    cached = {r[0] for r in con.execute(
        "SELECT uid FROM embeddings WHERE model=? AND dim=?", (MODEL, DIM))}
    todo = [(u, t) for u, t in papers if u not in cached and t.strip()]
    log(f"[embed] {len(cached)} cached, {len(todo)} to embed ({MODEL} @ {DIM}d)")

    if todo and not key:
        log("[embed] no OPENAI_API_KEY or MISTRAL_API_KEY -- index from cache only")
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
        {"model": MODEL, "dim": DIM, "n": len(uids), "shard": SHARD,
         "uids": uids}), encoding="utf-8")

    # Full abstracts, sharded to match the vector ROW ORDER. Ask needs the real
    # abstract (median ~1.4k chars) for the handful of papers it finally reads,
    # not the 400-char summary that ships in archive.json -- but shipping every
    # abstract would be a ~12 MB download. Sharding by row index means the
    # client can grab just the few blocks its picks land in: no hashing needed
    # on either side, because retrieval already knows each paper's row.
    absdir = docs / "abs"
    absdir.mkdir(exist_ok=True)
    for old in absdir.glob("*.json"):
        old.unlink()
    meta_by_uid = {}
    for uid, title, meta in rows:
        try:
            meta_by_uid[uid] = json.loads(meta)
        except Exception:                            # noqa: BLE001
            meta_by_uid[uid] = {}
    n_abs = 0
    for start in range(0, len(uids), SHARD):
        block = {}
        for off, uid in enumerate(uids[start:start + SHARD]):
            m = meta_by_uid.get(uid) or {}
            text = (m.get("abstract") or "").strip()
            if text:
                n_abs += 1
            block[str(start + off)] = text[:6000]     # cap pathological outliers
        (absdir / f"{start // SHARD}.json").write_text(
            json.dumps(block), encoding="utf-8")
    log(f"[embed] wrote {len(uids) // SHARD + 1} abstract shards "
        f"({n_abs} papers have full text)")
    pct = (100.0 * len(uids) / len(papers)) if papers else 0.0
    log(f"[embed] wrote docs/vec.bin ({len(blob) / 1e6:.2f} MB, "
        f"{len(uids)}/{len(papers)} papers = {pct:.0f}% coverage)")


if __name__ == "__main__":
    main()
