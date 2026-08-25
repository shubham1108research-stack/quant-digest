#!/usr/bin/env python3
"""Semantic index over the whole archive: embed every paper once, ship a compact
int8 matrix the portal can search entirely in the browser.

Provider is whichever key is present, OpenAI first (see _PROVIDERS): OpenAI
honours a width request, Mistral rejects it, and that is the difference between
a 2.3 MB index and a 9 MB one that every portal visitor downloads. The
requested width is never trusted either way -- _probe() measures what actually
comes back once and everything keys off the real number. Width matters: 9k
papers x 1024 dims x 4 bytes is ~34 MB, hopeless for a static page, while
256-dim int8 is ~2 MB. Cosine over unit vectors is a dot product and int8
rounding is monotonic, so ranking survives the squeeze.

Vectors are cached in state.db keyed by (uid, model, dim, txt) where txt is a
hash of the embedded TEXT. The content hash is what stops a paper keeping a
stale vector: tools/fill_abstracts.py backfills abstracts onto papers first
embedded from a title alone, and under a uid-only key every one of those kept
its title-only vector permanently, with nothing to detect it. Changing model or
dim still invalidates wholesale, so two vector spaces can never mix.

Outputs:
  docs/vec.bin   int8 matrix, row-major, n x DIM (no header -- pure payload)
  docs/vec.json  {model, dim, n, uids:[...]} -- row i of vec.bin is uids[i]

Usage:  python tools/embed.py            (incremental; embeds only what's new)
        python tools/embed.py --rebuild  (re-embed everything from scratch)
"""

import hashlib
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
# The median abstract is ~1,374 characters and title+topic are prepended, so
# 1600 was clipping a large share of them. text-embedding-3-small is $0.02 per
# million tokens: the whole archive at 4,000 chars costs about twenty cents.
# The old limit was saving nothing and losing the end of the method section.
MAX_CHARS = 4000
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
    txt   TEXT NOT NULL DEFAULT '',   -- sha1 of the embedded text
    src   TEXT NOT NULL DEFAULT '',   -- abstract | summary | title
    PRIMARY KEY (uid, model, dim, txt)
);
"""


def log(m: str) -> None:
    print(m, flush=True)


# Below this, a rebuild is treated as a failure rather than a shipment --
# see the guard in main(). Percent of the archive that must have vectors.
MIN_COVERAGE = 60.0


def _migrate_cache(con) -> None:
    """Add the content hash to an EXISTING cache table.

    CREATE TABLE IF NOT EXISTS is a no-op once the table exists, so a schema
    edit alone would never reach a live state.db -- the column would silently
    not be there and the next SELECT would fail. SQLite also cannot ALTER a
    PRIMARY KEY, so the table is rebuilt.

    Existing rows are DROPPED rather than carried over with an empty hash:
    there is no way to recover which text produced a stored vector, so keeping
    them would mean trusting exactly the thing this key exists to stop
    trusting. One re-embed of the archive costs about seven cents.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(embeddings)")}
    if cols and "txt" in cols and "src" not in cols:
        # ADD COLUMN, deliberately NOT the rebuild below. The content-hash
        # migration had to drop every row because there was no way to recover
        # which text produced a stored vector. Provenance is a different case:
        # it is simply unknown for old rows and knowable for new ones, and
        # blank is an honest answer. Rebuilding here would re-embed the whole
        # archive to acquire a label.
        con.execute("ALTER TABLE embeddings ADD COLUMN src TEXT NOT NULL DEFAULT ''")
        con.commit()
        log("[embed] added the src column; existing rows keep a blank one")
        return
    if not cols or "txt" in cols:
        return
    n = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    log(f"[embed] migrating cache to a content-keyed schema; "
        f"discarding {n} vectors whose source text cannot be verified")
    con.executescript("""
        DROP TABLE IF EXISTS embeddings_old;
        ALTER TABLE embeddings RENAME TO embeddings_old;
    """)
    con.executescript(_CACHE_SCHEMA)
    con.execute("DROP TABLE IF EXISTS embeddings_old")
    con.commit()


def _sha(text: str) -> str:
    """Content fingerprint for the cache key, so a paper whose abstract arrives
    later is re-embedded instead of keeping the vector built from its title."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _text(m: dict, title: str) -> str:
    """What we actually embed. See _text_src; this keeps the old call shape."""
    return _text_src(m, title)[0]


def _text_src(m: dict, title: str):
    """(text, src) -- src names the RICHEST field that went into the vector.

    The title carries the subject, the abstract the method and findings; it
    falls back abstract -> LLM summary -> title.

    A vector built from a bare title is a far weaker object than one built from
    a full abstract, and once stored the two are indistinguishable. An index
    where 40% of rows are title-only would score exactly like one where none
    are, right until someone wonders why recall is poor and has no way to look.
    Recording which it was turns coverage quality from an assumption into a
    query.
    """
    abstract = (m.get("abstract") or "").strip()
    summary = (m.get("summary") or "").strip()
    src = "abstract" if abstract else ("summary" if summary else "title")
    body = abstract or summary
    topic = (m.get("topic") or "").strip()
    parts = [title or m.get("title", "")]
    if topic:
        parts.append(topic)
    if body:
        parts.append(body)
    return " \n".join(p for p in parts if p)[:MAX_CHARS], src


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
    _migrate_cache(con)

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
        # ASCENDING, so row assignment is APPEND-ONLY. Descending put the newest
        # paper at row 0 and shifted every other row on every build -- and
        # abs/N.json shards are keyed on row index, so a browser holding a
        # cached shard against a fresh manifest read the wrong paper's abstract
        # straight into the Ask prompt. Silently: no error, no warning, a
        # confident answer about a paper nobody cited. Ascending means only the
        # LAST shard changes when papers are added.
        "SELECT uid, title, meta FROM items ORDER BY first_seen ASC, uid"
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
        papers.append((uid,) + _text_src(m, title))
    log(f"[embed] {len(papers)} papers in archive")

    # Cached on the TEXT, not just the uid. tools/fill_abstracts.py backfills
    # abstracts onto papers that were first embedded from a title alone -- and
    # under a uid-only key every one of those keeps its title-only vector
    # forever, invisibly, because nothing ever notices the input changed.
    cached = {(r[0], r[1]) for r in con.execute(
        "SELECT uid, txt FROM embeddings WHERE model=? AND dim=?", (MODEL, DIM))}
    todo = [(u, t, sr) for u, t, sr in papers
            if (u, _sha(t)) not in cached and t.strip()]
    log(f"[embed] {len(cached)} cached, {len(todo)} to embed ({MODEL} @ {DIM}d)")

    if todo and not key:
        log("[embed] no OPENAI_API_KEY or MISTRAL_API_KEY -- index from cache only")
        todo = []

    done = 0
    try:
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            vecs = _embed_batch([t for _, t, _ in chunk], key)
            con.executemany(
                "INSERT OR REPLACE INTO embeddings "
                "(uid, model, dim, txt, src, vec) VALUES (?,?,?,?,?,?)",
                [(u, MODEL, DIM, _sha(t), sr, _quantise(v))
                 for (u, t, sr), v in zip(chunk, vecs)],
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
    for uid, _, _ in papers:
        v = have.get(uid)
        if v and len(v) == DIM:
            uids.append(uid)
            blob += v

    docs = pathlib.Path("docs")
    docs.mkdir(exist_ok=True)

    # REFUSE TO SHIP AN INDEX WORSE THAN THE ONE ALREADY THERE.
    #
    # Run with no API key and an empty cache, this wrote a 0-byte vec.bin at 0%
    # coverage, deleted every abstract shard, and exited 0 -- so
    # prepare_deploy's required=True saw success and would have deployed a
    # portal whose Ask searches nothing. The browser does not fail loudly
    # either: loadIndex clamps to the rows the buffer holds, which is none.
    #
    # A partial index is legitimate exactly once: a COLD build stopped early
    # by a quota, where writing what we have lets the next run continue. "Cold"
    # means the CACHE was empty when this run started -- NOT that docs/vec.json
    # is missing. docs/ is gitignored and rebuilt from scratch on every CI run,
    # so a previous-file test reads as "cold" every time on the one machine
    # that actually ships, degrading this to "fail only when totally empty".
    # The cache keeps the vectors either way; this governs only what SHIPS.
    cold = not cached
    pct = (100.0 * len(uids) / len(papers)) if papers else 0.0
    if not uids or (not cold and pct < MIN_COVERAGE):
        log(f"[embed] REFUSING to write the index: {len(uids)}/{len(papers)} "
            f"papers ({pct:.0f}% coverage), below the {MIN_COVERAGE:.0f}% "
            f"floor, and {len(cached)} vectors were already cached so this is "
            f"not a cold build. Nothing was overwritten.")
        if not os.environ.get("OPENAI_API_KEY") and not os.environ.get("MISTRAL_API_KEY"):
            log("[embed] no embedding API key is set, which is usually the cause.")
        sys.exit(1)

    # BUILD STAMP. abs/N.json shards are keyed on ROW INDEX, so a browser
    # holding a cached shard against a freshly built manifest reads a different
    # paper's abstract straight into the Ask prompt -- silently, with no error
    # and a confident answer about a paper nobody cited. Ascending row order
    # stops that skew from being CREATED; this lets a client DETECT skew that
    # already reached it. Different jobs, both needed.
    #
    # Hashed from the uid list rather than stamped with a time, so it changes
    # exactly when row assignment changes and not once per run. A timestamp
    # would invalidate every cached shard on every build, which is the cost the
    # append-only ordering exists to avoid.
    build = hashlib.sha1("\n".join(uids).encode("utf-8")).hexdigest()[:12]

    (docs / "vec.bin").write_bytes(bytes(blob))
    (docs / "vec.json").write_text(json.dumps(
        {"model": MODEL, "dim": DIM, "n": len(uids), "shard": SHARD,
         "build": build, "uids": uids}), encoding="utf-8")

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
            json.dumps({"_build": build, "rows": block}), encoding="utf-8")
    log(f"[embed] wrote {len(uids) // SHARD + 1} abstract shards "
        f"({n_abs} papers have full text)")
    log(f"[embed] wrote docs/vec.bin ({len(blob) / 1e6:.2f} MB, "
        f"{len(uids)}/{len(papers)} papers = {pct:.0f}% coverage, build {build})")
    # Coverage QUALITY, not just quantity: a title-only vector and one built
    # from a full abstract count the same in the percentage above and are not
    # remotely the same object.
    by_src = dict(con.execute(
        "SELECT COALESCE(NULLIF(src, ''), 'unrecorded'), COUNT(*) "
        "FROM embeddings WHERE model=? AND dim=? GROUP BY 1 ORDER BY 2 DESC",
        (MODEL, DIM)))
    if by_src:
        log("[embed] vector provenance: "
            + ", ".join(f"{k} {v:,}" for k, v in by_src.items()))


if __name__ == "__main__":
    main()
