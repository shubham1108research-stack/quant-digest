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
from progress import Progress   # noqa: E402

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


# How often work is written down. A 2,400-request run is two hours, and
# committing only at the end means a cancel, a timeout or a runner dying at
# 1h59m throws away every request it spent. Measured the hard way: cancelling a
# drip to free the state-db lock lost the whole run.
#
# 100 papers is about five minutes of pacing -- small enough that almost nothing
# is lost, large enough that the commits are not the bottleneck.
CHECKPOINT = 100


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


def _flush(con, rows, marks):
    """Write down what has been fetched so far, then let the loop continue."""
    if rows:
        con.executemany(
            "INSERT OR IGNORE INTO paper_refs (src,ref) VALUES (?,?)", rows)
    for uid, patch in marks:
        store.update_meta(con, uid, patch)
    if rows or marks:
        con.commit()


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
    # Bounded by the BUDGET, not by the queue: the run stops when requests run
    # out, so a percentage against len(todo) would crawl toward 4% and read as
    # a stall. What is being consumed is requests.
    prog = Progress(min(args.max_requests, len(todo)), "harvest-refs",
                    every_s=120)
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
        prog.tick()
        if len(marks) >= CHECKPOINT:
            _flush(con, rows, marks)
            log(f"[harvest]   checkpoint: {got['fetched']:,} papers, "
                f"{got['refs']:,} references written")
            rows, marks = [], []
        time.sleep(PAUSE)

    _flush(con, rows, marks)
    prog.done()
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
        if got["matched"] % CHECKPOINT == 0:
            con.commit()
            log(f"[harvest]   checkpoint: {got['matched']:,} matched")
        time.sleep(PAUSE)
    con.commit()
    log(f"[harvest] spent {budget.used} requests ({budget.throttled} throttled)")
    for k, v in got.most_common():
        log(f"[harvest]    {k:<26} {v:,}")
    return 0


def cmd_authors(args):
    """The watched roster's FULL publication history.

    THE PLAN CALLS THIS THE LARGEST CLEAN EXPANSION OF THE CORE, and it has
    never run: `watchlist author` sits on 55 rows because
    WATCHLIST_LOOKBACK_DAYS = 60 with MAX_PER_AUTHOR = 8 only ever catches an
    author's NEW work. Their back catalogue was never fetched.

    RESOLUTION IS BY PAPER, NOT BY NAME, and this is the whole care of the
    function. /author/search for "Bryan Kelly" returns a clinician with
    h-index 4 who publishes on post-COVID clinics -- measured -- not the Yale
    asset-pricing one. Building a roster on name search would import an
    oncologist's bibliography as desk-relevant research and nothing downstream
    would notice.

    So an author is resolved through papers we ALREADY HOLD and already
    attribute to them: s2.py stores `s2_author_ids` from the batch call, taken
    off a paper whose DOI we matched. An authorId reached that way cannot be a
    different person. Names are used only to decide WHICH stored id we want,
    never to look one up.

    Papers found this way are NOT ingested here. They are recorded as
    candidates with their S2 ids, because a watched author's every paper is a
    strong prior and not a hard label -- Kelly writes on ML asset pricing and
    also on things this desk does not trade.
    """
    con = store.connect()
    import config                                             # noqa: PLC0415
    seed = {a["name"].lower(): a for a in getattr(config, "WATCHLIST_SEED", [])}
    if not seed:
        log("[harvest] no WATCHLIST_SEED in config")
        return 1

    # name -> {authorId: how many held papers attribute them to it}
    votes = collections.defaultdict(collections.Counter)
    held_s2 = set()
    for uid, title, m in _rows(con):
        ids = m.get("s2_author_ids") or []
        names = [n.strip().lower() for n in (m.get("authors") or "").split(",")]
        for sid in ids:
            held_s2.add(str(sid))
        if not ids or not names:
            continue
        for want in seed:
            last = want.split()[-1]
            # A held paper listing this surname AND carrying S2 author ids is
            # evidence; several such papers agreeing is the resolution.
            if any(last in n for n in names):
                for sid in ids:
                    votes[want][str(sid)] += 1

    resolved = {}
    for want, c in votes.items():
        # EVERY id meeting the threshold, not just the top one. S2 fragments
        # people across profiles: the Yale Bryan T. Kelly resolves to an id
        # carrying 19 papers when he has far more, so picking one id returns a
        # partial bibliography and calls it complete.
        #
        # And NOT the highest h-index either. Measured on this exact name:
        #     5342498      h=78, 313 papers -> an orthopaedic surgeon
        #     2411588366   h=18,  19 papers -> "Text as Data", "Hedging
        #                                       Climate Change News"
        # The h-index heuristic picks the surgeon. Only the papers we hold
        # know which person we mean.
        keep = [(sid, n) for sid, n in c.most_common() if n >= 2]
        if keep:
            resolved[want] = keep
    # VERIFY EVERY CANDIDATE ID BY NAME BEFORE SPENDING A REQUEST ON IT.
    #
    # The vote above credits every author id on a matching paper, so a frequent
    # co-author clears the threshold as easily as the person meant. Measured:
    #
    #   watched "Andrea Frazzini"  ->  2106499    Andrea Frazzini      correct
    #                                  102104637  Clifford S. Asness   co-author
    #                                  31871734   L. Pedersen          co-author
    #   watched "Bryan Kelly"      ->  2772316    S. Malamud           co-author,
    #                                                                  and the
    #                                                                  TOP vote
    #
    # Unfiltered that was 1,578 profiles across 40 authors -- 39 each, almost
    # all of them other people. POST /author/batch returns names for hundreds of
    # ids in one request, so the whole candidate set is checkable for the price
    # of two, against ~1,578 requests spent fetching strangers' bibliographies.
    def _key(n):
        """(first initial, surname) -- S2 abbreviates given names freely.

        "Lasse Heje Pedersen" and "L. Pedersen" are one person; "Bryan Kelly"
        and "Bryan T. Kelly" are one person; "Clifford S. Asness" is not either
        of them.
        """
        parts = [w for w in re.split(r"[^A-Za-z]+", (n or "").lower()) if w]
        if not parts:
            return ("", "")
        return (parts[0][:1], parts[-1])

    cand = sorted({sid for ids in resolved.values() for sid, _ in ids})
    names = {}
    if cand:
        for i in range(0, len(cand), 500):
            chunk = cand[i:i + 500]
            body = json.dumps({"ids": chunk}).encode("utf-8")
            req = urllib.request.Request(
                f"{API}/author/batch?fields=name", data=body,
                headers=dict(_headers(), **{"Content-Type": "application/json"}))
            # RETRIED, because a chunk that fails here does not fail loudly --
            # its ids get no name, the `nm is None` branch skips them, and the
            # authors they belonged to vanish from the run. Measured: one 429
            # took the roster from 39 resolved authors to 30, and the only
            # symptom was a smaller number in a log line.
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        for a in json.load(r):
                            if a and a.get("authorId"):
                                names[str(a["authorId"])] = a.get("name") or ""
                    break
                except Exception as e:                        # noqa: BLE001
                    wait = 8 * (attempt + 1)
                    log(f"[harvest] author/batch chunk {i//500 + 1} "
                        f"{type(e).__name__}, retry in {wait}s")
                    time.sleep(wait)
            else:
                log(f"[harvest] author/batch chunk {i//500 + 1} GAVE UP -- the "
                    f"authors in it are skipped this run, not lost")
            time.sleep(PAUSE)

    dropped = 0
    for want in list(resolved):
        keep = []
        for sid, n in resolved[want]:
            nm = names.get(sid)
            if nm is None:
                continue
            if _key(nm) == _key(seed[want]["name"]):
                keep.append((sid, n))
            else:
                dropped += 1
        if keep:
            resolved[want] = keep
        else:
            del resolved[want]
    log(f"[harvest] verified {len(cand):,} candidate profiles by name: dropped "
        f"{dropped:,} belonging to someone else (co-authors)")

    log(f"[harvest] {len(seed)} watched authors; {len(resolved)} resolved to an "
        f"S2 id through papers we hold (>=2 agreeing, name-verified)")
    if not resolved:
        log("[harvest] none resolved -- run `s2.py enrich` first so papers "
            "carry s2_author_ids")
        return 0
    if args.dry_run:
        for want, ids in sorted(resolved.items())[:12]:
            shown = " ".join(f"{sid}({n})" for sid, n in ids[:3])
            log(f"[harvest]    {seed[want]['name']:<28} {shown}")
        log(f"[harvest] DRY RUN: would spend up to {args.max_requests} requests "
            f"across {sum(len(v) for v in resolved.values())} author profiles")
        return 0

    budget = Budget(args.max_requests)
    got = collections.Counter()
    found = []
    for want, ids in sorted(resolved.items()):
        for sid, _n in ids:
            if not budget:
                break
            d = _get(f"{API}/author/{sid}/papers"
                     f"?fields=title,year,externalIds,citationCount&limit=100",
                     budget)
            if not d:
                # _get returns None for a timeout or an exhausted retry/request
                # budget, and {"_notfound": True} only for a real 404. Merging
                # them reported "profile not found: 175" for a roster S2 knows
                # perfectly well. Two other call sites in this file already
                # keep them apart (see `got["failed"]` and `got["term failed"]`).
                got["request failed"] += 1
            elif d.get("_notfound"):
                got["profile not found"] += 1
                time.sleep(PAUSE)
                continue
            for x in (d.get("data") or []):
                doi = (x.get("externalIds") or {}).get("DOI")
                if not doi:
                    continue
                found.append({"doi": doi.lower(), "title": x.get("title") or "",
                              "year": x.get("year"),
                              "cites": x.get("citationCount"),
                              "author": seed[want]["name"], "s2_author": sid})
            got["profiles done"] += 1
            time.sleep(PAUSE)

    out = pathlib.Path("export/watched_author_papers.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    seen_doi = set()
    for uid, title, m in _rows(con):
        d = (m.get("doi") or "").lower().strip()
        if d:
            seen_doi.add(d)
    fresh = [f for f in found if f["doi"] not in seen_doi]
    out.write_text(json.dumps(fresh, indent=1), encoding="utf-8")
    log(f"[harvest] spent {budget.used} requests ({budget.throttled} throttled)")
    for k, v in got.most_common():
        log(f"[harvest]    {k:<20} {v:,}")
    log(f"[harvest] {len(found):,} papers by watched authors, {len(fresh):,} NOT "
        f"already held -> {out}")
    log("[harvest] candidates only -- an author's every paper is a strong prior, "
        "not a label, so ingestion is a separate decision")
    return 0


def cmd_discover(args):
    """Find finance papers the archive does NOT have.

    Every other action here enriches rows we already hold. This one looks
    outward -- which is what the plan means by a core graph that expands, and
    what the existing collectors cannot do: they read fixed feeds, so the
    archive only ever contains what those feeds happened to carry.

    /paper/search/bulk takes a query with year and field-of-study filters,
    sorts by citation count, and pages through a continuation token. Verified:

        query="time series momentum managed futures"
        year=2024-  fieldsOfStudy=Economics,Business  sort=citationCount:desc
        -> 200, total 2, both with DOIs

    QUERIES COME FROM config.TAGS, the 75-term closed vocabulary already used
    for tagging. That is deliberate: the vocabulary was built to describe what
    this desk cares about, so it is the right thing to search with, and reusing
    it means discovery and tagging cannot drift apart into two different
    notions of the subject.

    NOTHING IS INGESTED. Results are written to export/ as candidates. The
    archive already carries what unreviewed topic sweeps produce -- a Zenodo
    entry titled "LAB #958 NEUTRAL: VIDEO SCOUT" that the LLM then scored -- and
    a discovery route that writes straight into the corpus is how you get more
    of that. Finding is cheap; deciding is the expensive part and stays manual.
    """
    con = store.connect()
    import config                                             # noqa: PLC0415
    held = set()
    for uid, title, m in _rows(con):
        d = (m.get("doi") or "").lower().strip()
        if d:
            held.add(d)
    terms = list(getattr(config, "TAGS", []) or [])
    if args.query:
        terms = [args.query]
    log(f"[harvest] {len(held):,} DOIs held; searching {len(terms)} term(s) "
        f"from the tag vocabulary, year {args.since}-")
    if args.dry_run:
        log(f"[harvest] DRY RUN: would spend up to {args.max_requests} requests")
        log(f"[harvest]   sample terms: {terms[:8]}")
        return 0

    budget = Budget(args.max_requests)
    got = collections.Counter()
    fresh, seen = [], set()
    for term in terms:
        if not budget:
            break
        q = urllib.parse.quote(f'"{term}"' if " " in term else term)
        d = _get(f"{API}/paper/search/bulk?query={q}"
                 f"&fields=title,year,externalIds,citationCount,abstract"
                 f"&year={args.since}-&fieldsOfStudy=Economics,Business"
                 f"&sort=citationCount:desc", budget)
        if not d or d.get("_notfound"):
            got["term failed"] += 1
            time.sleep(PAUSE)
            continue
        got["terms searched"] += 1
        for x in (d.get("data") or [])[:args.per_term]:
            doi = (x.get("externalIds") or {}).get("DOI")
            if not doi:
                got["no doi"] += 1
                continue
            doi = doi.lower()
            if doi in held:
                got["already held"] += 1
                continue
            if doi in seen:
                continue
            seen.add(doi)
            fresh.append({"doi": doi, "title": x.get("title") or "",
                          "year": x.get("year"),
                          "cites": x.get("citationCount"),
                          "has_abstract": bool((x.get("abstract") or "").strip()),
                          "found_by": term})
        time.sleep(PAUSE)

    out = pathlib.Path("export/discovered.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    fresh.sort(key=lambda f: -(f["cites"] or 0))
    out.write_text(json.dumps(fresh, indent=1), encoding="utf-8")
    log(f"[harvest] spent {budget.used} requests ({budget.throttled} throttled)")
    for k, v in got.most_common():
        log(f"[harvest]    {k:<16} {v:,}")
    log(f"[harvest] {len(fresh):,} papers NOT already held -> {out}")
    for f in fresh[:8]:
        # citationCount and year are both nullable in the S2 response. The sort
        # two lines up already defends with `or 0`; this line did not, so the
        # first paper without a citation count would raise TypeError AFTER the
        # JSON was written -- the file survives, the job exits non-zero, and the
        # run looks like it failed when it had actually finished.
        log(f"[harvest]    {f.get('year') or '????'} "
            f"c={f.get('cites') or 0:<5} {(f.get('title') or '')[:52]}")
    log("[harvest] candidates only -- nothing ingested. Review before adding.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    for name in ("refs", "match", "authors", "discover"):
        p = sub.add_parser(name)
        p.add_argument("--max-requests", type=int, default=RATE_REQUESTS)
        p.add_argument("--dry-run", action="store_true")
        if name == "discover":
            p.add_argument("--since", type=int, default=2024,
                           help="earliest publication year")
            p.add_argument("--per-term", type=int, default=10,
                           help="candidates kept per search term")
            p.add_argument("--query", default="",
                           help="one query instead of the tag vocabulary")
    args = ap.parse_args()
    return {"refs": cmd_refs, "match": cmd_match, "authors": cmd_authors,
            "discover": cmd_discover}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
