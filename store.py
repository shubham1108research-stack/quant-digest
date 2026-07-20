"""SQLite state: dedup across runs + a permanent metadata archive."""

import hashlib
import json
import re
import sqlite3

DB_PATH = "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    uid        TEXT PRIMARY KEY,     -- doi:… | arxiv:… | t:<title-hash>
    title      TEXT,
    source     TEXT,                 -- e.g. 'nep-fmk', 'arxiv', 'crossref:JF'
    section    TEXT,                 -- 1..5
    area       TEXT,
    url        TEXT,
    meta       TEXT,                 -- full item as JSON, for the archive
    first_seen TEXT DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,          -- e.g. 'backfill_earliest'
    val   TEXT
);
CREATE TABLE IF NOT EXISTS month_progress (
    month      TEXT PRIMARY KEY,     -- 'YYYY-MM' currently being backfilled
    candidates TEXT,                 -- JSON list of candidate items + partial scores
    done       INTEGER DEFAULT 0     -- 1 once every candidate is scored
);
"""


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.([\d.]+)$", re.I)
_ARXIV_URL = re.compile(r"arxiv\.org/abs/([\d.]+)", re.I)
_ARXIV_REPEC = re.compile(r"RePEc:arx:papers:([\d.]+)", re.I)


def make_uid(item: dict) -> str:
    doi = (item.get("doi") or "").lower().strip()
    # arXiv's own DataCite DOI (10.48550/arxiv.<id>, e.g. from OpenAlex) is
    # just another name for the same paper an arxiv_id already identifies --
    # route it into the same "arxiv:" namespace instead of "doi:", or it
    # silently escapes dedup against the paper collected directly from arXiv
    m = _ARXIV_DOI.match(doi)
    if m:
        return "arxiv:" + m.group(1)
    aid = item.get("arxiv_id") or ""
    if not aid:
        # last resort: some collectors (OpenAlex sweeps, mailing-list
        # mirrors) never set arxiv_id/doi at all, just a bare arxiv.org/abs
        # URL or a RePEc redirect that embeds one -- pull the id out of
        # that rather than falling to a title hash
        url = item.get("url") or ""
        m2 = _ARXIV_URL.search(url) or _ARXIV_REPEC.search(url)
        if m2:
            aid = m2.group(1)
    if aid:
        # strip the version suffix (v1, v2, ...) -- different collection
        # paths (bulk API vs RSS fallback vs a RePEc/NEP mirror link) agree
        # on whether it's present, which would otherwise put the same paper
        # in two different uid namespaces and silently defeat dedup
        return "arxiv:" + re.sub(r"v\d+$", "", aid)
    if doi:
        return "doi:" + doi
    h = hashlib.sha1(norm_title(item.get("title", "")).encode()).hexdigest()[:16]
    return "t:" + h


def connect():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)         # multiple CREATE TABLE statements
    return con


def filter_new(con, items: list[dict]) -> list[dict]:
    """Drop items already seen (by uid) and intra-batch duplicates."""
    fresh, batch_uids = [], set()
    for it in items:
        uid = make_uid(it)
        if uid in batch_uids:
            # same paper from a second source: record the extra source
            for f in fresh:
                if f["uid"] == uid:
                    f["sources"] = sorted(set(f.get("sources", [f["source"]])
                                              ) | {it["source"]})
            continue
        row = con.execute("SELECT 1 FROM items WHERE uid=?", (uid,)).fetchone()
        if row:
            continue
        it["uid"] = uid
        it.setdefault("sources", [it["source"]])
        batch_uids.add(uid)
        fresh.append(it)
    return fresh


def kv_get(con, key: str, default=None):
    row = con.execute("SELECT val FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(con, key: str, val: str) -> None:
    con.execute("INSERT INTO kv (key, val) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, str(val)))
    con.commit()


def get_progress(con, month: str) -> dict | None:
    """Return {'candidates': [...], 'done': bool} for an in-progress backfill
    month, or None if that month has no saved progress."""
    row = con.execute(
        "SELECT candidates, done FROM month_progress WHERE month=?", (month,)
    ).fetchone()
    if not row:
        return None
    try:
        cands = json.loads(row[0]) if row[0] else []
    except Exception:                                  # noqa: BLE001
        cands = []
    return {"candidates": cands, "done": bool(row[1])}


def set_progress(con, month: str, candidates: list[dict], done: bool) -> None:
    con.execute(
        "INSERT INTO month_progress (month, candidates, done) VALUES (?,?,?) "
        "ON CONFLICT(month) DO UPDATE SET candidates=excluded.candidates, "
        "done=excluded.done",
        (month, json.dumps(candidates, default=str), 1 if done else 0))
    con.commit()


def clear_progress(con, month: str) -> None:
    con.execute("DELETE FROM month_progress WHERE month=?", (month,))
    con.commit()


def save(con, items: list[dict]) -> None:
    for it in items:
        con.execute(
            "INSERT OR IGNORE INTO items "
            "(uid, title, source, section, area, url, meta) "
            "VALUES (?,?,?,?,?,?,?)",
            (it["uid"], it.get("title"), ",".join(it.get("sources", [])),
             str(it.get("section")), it.get("area"), it.get("url"),
             json.dumps(it, default=str)),
        )
    con.commit()
