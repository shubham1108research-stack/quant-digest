#!/usr/bin/env python3
"""Drip-feed the Semantic Scholar endpoints that batch will not serve.

THE PROBLEM. `s2.py enrich` uses POST /paper/batch, which returns 500 papers a
request and covers the scalars in one run. But two things it cannot do:

  * REFERENCES. Batch returns `referenceCount: 75` and `references: []` --
    measured. Only GET /paper/{id}/references gives the list, one paper per
    request.
  * PAPERS WITH NO DOI. 3,863 rows are title-hash uids the batch endpoint
    cannot address at all. They need GET /paper/search/match, again one
    request each.

At the public rate -- 100 requests per 5 minutes -- either of those is
impossible in a single job and trivial across a day: 100 per 5 min is
**28,800 requests a day**, and the whole archive is 20,999 papers.

SO THIS IS BUILT AS A DRIP, NOT A RUN. Every invocation takes a bounded
request budget, does that much work, records what it learned, and stops. A
scheduled workflow calls it repeatedly. Progress accumulates in state.db,
which is the shared store, so the work survives between runs by construction
rather than by a checkpoint file someone has to remember to write.

RESUMABILITY IS THE WHOLE DESIGN, and it needs negative results as much as
positive ones. A paper S2 has never heard of must be MARKED, or every future
run spends a request rediscovering that. `s2_refs` and `s2_miss` are those
marks:

    s2_refs = n     we fetched its reference list, n entries (0 is a real answer)
    s2_miss = why   S2 could not resolve it; do not ask again

Without those two fields the drip would re-scan the same dead rows forever and
never reach the live ones.

    python tools/s2_harvest.py refs   --max-requests 100 --dry-run
    python tools/s2_harvest.py refs   --max-requests 100
    python tools/s2_harvest.py match  --max-requests 100
"""

import argparse
import collections
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store    # noqa: E402

API = "https://api.semanticscholar.org/graph/v1"
# 100 requests / 5 minutes = one per 3 seconds. Kept as the two numbers rather
# than a magic constant so the arithmetic is checkable when the quota changes.
RATE_REQUESTS, RATE_WINDOW_S = 100, 300
PAUSE = RATE_WINDOW_S / float(RATE_REQUESTS)


def log(m):
    print(m, flush=True)


def _headers():
    h = {"User-Agent": "quant-digest/1.0 (personal research portal; mailto:%s)"
         % (os.environ.get("CONTACT_EMAIL") or "research")}
    key = os.environ.get("S2_API_KEY")
    if key:
        h["x-api-key"] = key
    return h


class Budget:
    """Requests are the scarce resource, so they are counted, not the rows."""

    def __init__(self, n):
        self.left = n
        self.used = 0
        self.throttled = 0

    def spend(self):
        self.left -= 1
        self.used += 1

    def __bool__(self):
        return self.left > 0


def _get(url, budget, tries=3):
    for attempt in range(tries):
        if not budget:
            return None
        req = urllib.request.Request(url, headers=_headers())
        budget.spend()
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"_notfound": True}
            if e.code in (429, 500, 502, 503):
                budget.throttled += 1
                # Back off past the window rather than hammering: a 429 means
                # the shared pool is saturated and speed will not help.
                wait = min(PAUSE * (attempt + 2) * 2, 45)
                time.sleep(wait)
                continue
            return None
        except Exception:                                     # noqa: BLE001
            time.sleep(PAUSE)
    return None


def _rows(con):
    for uid, title, meta in con.execute("SELECT uid,title,meta FROM items"):
        try:
            m = json.loads(meta)
        except Exception:                                     # noqa: BLE001
            continue
        if m.get("retired"):
            continue
        yield uid, title or "", m


def _s2_id(uid, m):
    doi = (m.get("doi") or "").strip()
    if doi:
        return "DOI:" + doi
    if uid.startswith("arxiv:"):
        return "ARXIV:" + uid.split(":", 1)[1].split("v")[0]
    aid = (m.get("arxiv_id") or "").strip()
    if aid:
        return "ARXIV:" + aid.split("v")[0]
    return None


def _have_refs(con):
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT src FROM paper_refs")}
    except Exception:                                         # noqa: BLE001
        return set()


def cmd_refs(args):
    """Reference lists, for papers OpenAlex could not supply them for.

    OpenAlex already covers most of the archive and is free and keyless, so
    this deliberately targets only the remainder -- working papers especially,
    where OpenAlex frequently returns an empty referenced_works.
    """
    con = store.connect()
    con.executescript(
        "CREATE TABLE IF NOT EXISTS paper_refs ("
        " src TEXT NOT NULL, ref TEXT NOT NULL, PRIMARY KEY (src, ref));"
        "CREATE INDEX IF NOT EXISTS paper_refs_ref ON paper_refs(ref);")
    have = _have_refs(con)
    todo = []
    for uid, title, m in _rows(con):
        if uid in have or m.get("s2_refs") is not None or m.get("s2_miss"):
            continue
        sid = _s2_id(uid, m)
        if sid:
            todo.append((uid, sid))
    log(f"[harvest] {len(todo):,} papers have no stored references and have not "
        f"been asked yet")
    if args.dry_run:
        log(f"[harvest] DRY RUN: would spend up to {args.max_requests} requests "
            f"at {PAUSE:.1f}s apart (~{args.max_requests*PAUSE/60:.0f} min)")
        return 0

    budget = Budget(args.max_requests)
    got = collections.Counter()
    rows, marks = [], []
    for uid, sid in todo:
        if not budget:
            break
        d = _get(f"{API}/paper/{urllib.parse.quote(sid)}/references"
                 f"?fields=externalIds&limit=200", budget)
        if d is None:
            got["failed"] += 1
            continue
        if d.get("_notfound"):
            marks.append((uid, {"s2_miss": "notfound"}))
            got["not in s2"] += 1
            time.sleep(PAUSE)
            continue
        refs = []
        for it in (d.get("data") or []):
            ex = ((it.get("citedPaper") or {}).get("externalIds") or {})
            doi = ex.get("DOI")
            if doi:
                # Stored in OpenAlex's url shape so both sources land in one
                # namespace and coupling can group across them.
                refs.append("doi:" + doi.lower())
        rows += [(uid, r) for r in refs]
        marks.append((uid, {"s2_refs": len(refs)}))
        got["fetched"] += 1
        got["refs"] += len(refs)
        time.sleep(PAUSE)

    if rows:
        con.executemany(
            "INSERT OR IGNORE INTO paper_refs (src,ref) VALUES (?,?)", rows)
    for uid, patch in marks:
        store.update_meta(con, uid, patch)
    con.commit()
    log(f"[harvest] spent {budget.used} requests ({budget.throttled} throttled), "
        f"stored {len(rows):,} references")
    for k, v in got.most_common():
        log(f"[harvest]    {k:<12} {v:,}")
    log(f"[harvest] {len(todo) - got['fetched'] - got['not in s2']:,} still "
        f"outstanding for the next run")
    return 0


def cmd_match(args):
    """Resolve title-hash rows to an S2 paper, so batch can reach them later.

    3,863 rows carry neither a DOI nor an arXiv id. /paper/search/match takes a
    title and returns one best guess, which is the only way in -- and it is one
    request per paper, which is exactly what a drip is for.
    """
    con = store.connect()
    todo = []
    for uid, title, m in _rows(con):
        if _s2_id(uid, m) or m.get("s2_miss") or m.get("s2_paper_id"):
            continue
        if len(title.strip()) >= 24:
            todo.append((uid, title.strip()))
    log(f"[harvest] {len(todo):,} rows have no DOI/arXiv and no S2 id yet")
    if args.dry_run:
        log(f"[harvest] DRY RUN: would spend up to {args.max_requests} requests")
        return 0

    budget = Budget(args.max_requests)
    got = collections.Counter()
    for uid, title in todo:
        if not budget:
            break
        q = urllib.parse.quote(title[:200])
        d = _get(f"{API}/paper/search/match?query={q}"
                 f"&fields=title,externalIds,abstract", budget)
        if d is None:
            got["failed"] += 1
            continue
        if d.get("_notfound") or not (d.get("data") or []):
            store.update_meta(con, uid, {"s2_miss": "nomatch"})
            got["no match"] += 1
            time.sleep(PAUSE)
            continue
        it = d["data"][0]
        norm = lambda t: re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()
        # A match endpoint always returns SOMETHING. Without this check it
        # would confidently attach a different paper's DOI to our row, which is
        # worse than leaving the row unresolved.
        if norm(it.get("title"))[:60] != norm(title)[:60]:
            store.update_meta(con, uid, {"s2_miss": "title mismatch"})
            got["rejected (title differs)"] += 1
            time.sleep(PAUSE)
            continue
        patch = {"s2_paper_id": it.get("paperId")}
        doi = (it.get("externalIds") or {}).get("DOI")
        if doi:
            patch["doi"] = doi.lower()
            got["gained a DOI"] += 1
        ab = (it.get("abstract") or "").strip()
        if len(ab) >= 120:
            patch["abstract"] = ab[:6000]
            patch["abstract_source"] = "s2-match"
            con.execute("DELETE FROM embeddings WHERE uid=?", (uid,))
            got["gained an abstract"] += 1
        store.update_meta(con, uid, patch)
        got["matched"] += 1
        time.sleep(PAUSE)
    con.commit()
    log(f"[harvest] spent {budget.used} requests ({budget.throttled} throttled)")
    for k, v in got.most_common():
        log(f"[harvest]    {k:<26} {v:,}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    for name in ("refs", "match"):
        p = sub.add_parser(name)
        p.add_argument("--max-requests", type=int, default=RATE_REQUESTS)
        p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return cmd_refs(args) if args.action == "refs" else cmd_match(args)


if __name__ == "__main__":
    sys.exit(main())
