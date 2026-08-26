#!/usr/bin/env python3
"""Bulk-fetch the full-text PDFs we can legitimately get.

Resolution order per paper (first hit wins):
  1. arXiv        -- arxiv.org/pdf/<id>, always available
  2. RePEc->arXiv -- RePEc handles that mirror an arXiv paper
  3. NBER         -- the working-paper PDF path
  4. a URL that already IS a pdf
  5. Unpaywall    -- every oa_location's url_for_pdf, by DOI
  6. OpenAlex     -- locations[].pdf_url, by DOI
  7. Sem. Scholar -- openAccessPdf; recovers ~11% of what Unpaywall gives up on
  8. CORE         -- open-access repository aggregator (needs CORE_API_KEY)
  9. arXiv title  -- exact-title match only; ~3% here, so it runs last

A free copy usually EXISTS for a paywalled economics paper -- as an SSRN, NBER,
CEPR or institutional working paper -- but "exists" and "machine-discoverable"
are different things. SSRN has no public API and blocks crawlers; Google Scholar
prohibits scraping. So automated resolution tops out around a third of the
archive, and the rest need either an institutional subscription or a human with
two minutes. This script does not attempt to bypass any paywall.

Everything is resumable: each attempt's outcome is recorded in state.db, so a
re-run only touches papers that are new or previously failed. Downloads are
rate-limited per host, gated on that host's robots.txt, and verified to be real
PDFs (a paywall interstitial that returns 200 with HTML is recorded as a miss,
not saved as a corrupt file).

  python tools/fetch_pdfs.py                 # resolve + download everything new
  python tools/fetch_pdfs.py --resolve-only  # find URLs, download nothing
  python tools/fetch_pdfs.py --limit 200     # small trial run
  python tools/fetch_pdfs.py --retry-failed  # re-attempt previous failures
  python tools/fetch_pdfs.py --url <link>    # ad-hoc: verify one link, no DB
"""

import argparse
import collections
import hashlib
import json
import os
import pathlib
import queue
import re
import sqlite3
import sys
import threading
import time
import urllib.robotparser as robotparser
from urllib.parse import urlsplit

import requests
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import store
from progress import Progress  # noqa: E402

OUT = pathlib.Path("pdfs")
EMAIL = os.environ.get("CONTACT_EMAIL") or "upadhyays1108@gmail.com"
CORE_KEY = os.environ.get("CORE_API_KEY")   # optional, free registration
_ATOM = "{http://www.w3.org/2005/Atom}"
UA = f"quant-digest/1.0 (research archive; mailto:{EMAIL})"
WORKERS = 6
MAX_MB = 60                      # skip absurd files
HOST_DELAY = {                   # seconds between hits on the same host
    "arxiv.org": 3.0,            # arXiv asks bulk users to go easy
    "export.arxiv.org": 3.1,     # arXiv API: >=3s between queries
    "default": 0.5,
}

# statuses a re-run must never retry, even under --retry-failed: "ok" is done,
# and a host that told us not to fetch will still be telling us that tomorrow
TERMINAL = {"ok", "robots_disallow"}

ARXIV = re.compile(r"arxiv\.org/abs/([^/?#]+)", re.I)
NBER = re.compile(r"nber\.org/papers/w(\d+)", re.I)
IS_PDF = re.compile(r"\.pdf(\?|$)", re.I)
REPEC_ARXIV = re.compile(r"RePEc:arx:papers:([\d.]+)", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdfs (
    uid    TEXT PRIMARY KEY,
    status TEXT,               -- ok | no_source | robots_disallow | http_403 |
                               -- http_429 | http_error | html_not_pdf |
                               -- not_pdf | too_big
    url    TEXT,
    path   TEXT,
    bytes  INTEGER,
    ts     TEXT DEFAULT (datetime('now'))
);
"""

_lock = threading.Lock()
_robots_lock = threading.Lock()
_robots: dict[str, robotparser.RobotFileParser | None] = {}
_last_hit: dict[str, float] = {}
_counts: collections.Counter = collections.Counter()


def log(m):
    print(m, flush=True)


def _host(u):
    m = re.findall(r"https?://([^/]+)", u or "")
    return re.sub(r"^www\.", "", m[0]).lower() if m else ""


def _polite(u):
    """Space out requests per host so a bulk run stays a good citizen.

    The sleep happens OUTSIDE the lock. It used to be inside, and _lock is
    process-global (it also guards _counts) -- so a worker waiting out
    arxiv.org's 3s delay blocked all five others from touching unrelated
    hosts. The per-host throttle collapsed into a global one and WORKERS=6
    behaved as 1. Reserving the slot under the lock keeps the spacing
    guarantee without serialising the whole pool."""
    h = _host(u)
    delay = HOST_DELAY.get(h, HOST_DELAY["default"])
    with _lock:
        now = time.time()
        wait = delay - (now - _last_hit.get(h, 0))
        # claim this host's next slot before releasing, so two workers racing
        # for the same host queue behind each other rather than colliding
        _last_hit[h] = now + max(0.0, wait)
    if wait > 0:
        time.sleep(wait)


def _allowed(url):
    """Check robots.txt for the host we are about to DOWNLOAD from.

    Only the download path is gated -- never the metadata APIs (Unpaywall,
    OpenAlex, Semantic Scholar, CORE, the arXiv API). Those are published for
    exactly this kind of client and their robots.txt is aimed at search
    crawlers; obeying it there would disable the resolver for no benefit.

    Fail OPEN when robots.txt is missing or unreachable -- plenty of university
    and repository servers simply 404 it -- and fail CLOSED only on an explicit
    Disallow. Fetched with requests rather than RobotFileParser.read() so it
    inherits a timeout: a hung robots.txt would otherwise stall a worker.
    """
    p = urlsplit(url or "")
    if p.scheme not in ("http", "https") or not p.netloc:
        return False
    key = p.netloc.lower()
    with _robots_lock:
        seen, rp = key in _robots, _robots.get(key)
    if not seen:
        rp = robotparser.RobotFileParser()
        try:
            r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt",
                             headers={"User-Agent": UA}, timeout=15)
            if r.status_code == 200 and r.text.strip():
                rp.parse(r.text.splitlines())
            else:
                rp = None                       # nothing published -> allowed
        except Exception:                       # noqa: BLE001
            rp = None                           # unreachable -> allowed
        with _robots_lock:
            _robots[key] = rp
    if rp is None:
        return True
    try:
        # RobotFileParser matches the token before the first "/", so our
        # "quant-digest/1.0 (...)" is compared as "quant-digest"
        return rp.can_fetch(UA, url)
    except Exception:                           # noqa: BLE001
        return True


def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _get(u, headers_extra=None, **kw):
    _polite(u)
    h = {"User-Agent": UA, "Accept": "application/pdf,*/*"}
    if headers_extra:
        h.update(headers_extra)
    return requests.get(u, headers=h, timeout=45, **kw)


def _title_to_doi(title):
    """Items collected from mailing-list mirrors carry no DOI (uid is a title
    hash), so ask OpenAlex to identify the paper by title before giving up."""
    if not title or len(title) < 15:
        return None
    try:
        r = _get("https://api.openalex.org/works",
                 params={"filter": "title.search:" + title[:180],
                         "select": "doi,locations", "per-page": 1, "mailto": EMAIL})
        if not r.ok:
            return None
        res = r.json().get("results") or []
        if not res:
            return None
        for loc in (res[0].get("locations") or []):
            if loc.get("pdf_url"):
                return ("PDF", loc["pdf_url"])
        doi = (res[0].get("doi") or "").replace("https://doi.org/", "")
        return ("DOI", doi) if doi else None
    except Exception:                               # noqa: BLE001
        return None


_WP_INDEX = None      # title -> (wp, authors, year); built in main()


def resolve(uid, url, title=None, doi=None):
    """Return a PDF url for this paper, or None if there's no free copy.

    `doi` may come from the item's METADATA, which is not the same as its uid.
    217 classics carry a DOI added by a later repair pass while keeping the
    title-hash uid they were inserted with; reading the DOI from the uid alone
    silently skipped Unpaywall, OpenAlex, Semantic Scholar and CORE for every
    one of them, and recorded the result as "no free copy".
    """
    u = url or ""
    m = ARXIV.search(u)
    if m or uid.startswith("arxiv:"):
        aid = m.group(1) if m else uid.split(":", 1)[1]
        return f"https://arxiv.org/pdf/{aid}"
    # RePEc mirrors an arXiv paper under its own handle -- the arXiv id is
    # right there in the redirect, so don't let it fall through as "no source"
    m = REPEC_ARXIV.search(u)
    if m:
        return f"https://arxiv.org/pdf/{m.group(1)}"
    m = NBER.search(u)
    if m:
        return (f"https://www.nber.org/system/files/working_papers/"
                f"w{m.group(1)}/w{m.group(1)}.pdf")
    if IS_PDF.search(u):
        return u

    doi = (doi or "").strip() or (uid[4:] if uid.startswith("doi:") else None)
    if not doi:                                     # no identifier -> identify it
        got = _title_to_doi(title)
        if not got:
            return None
        kind, val = got
        if kind == "PDF":
            return val
        doi = val

    try:                                            # Unpaywall: the OA authority
        r = _get(f"https://api.unpaywall.org/v2/{doi}", params={"email": EMAIL})
        if r.ok:
            j = r.json()
            best = j.get("best_oa_location") or {}
            if best.get("url_for_pdf"):
                return best["url_for_pdf"]
            for loc in (j.get("oa_locations") or []):
                if loc.get("url_for_pdf"):
                    return loc["url_for_pdf"]
    except Exception:                               # noqa: BLE001
        pass

    try:                                            # OpenAlex catches some extras
        r = _get("https://api.openalex.org/works/doi:" + doi,
                 params={"select": "locations", "mailto": EMAIL})
        if r.ok:
            for loc in (r.json().get("locations") or []):
                if loc.get("pdf_url"):
                    return loc["pdf_url"]
    except Exception:                               # noqa: BLE001
        pass

    # Semantic Scholar indexes repository copies the DOI-based services miss.
    # Measured: recovers ~11% of what Unpaywall gives up on.
    try:
        r = _get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                 params={"fields": "openAccessPdf"})
        if r.ok:
            oa = (r.json() or {}).get("openAccessPdf") or {}
            if oa.get("url"):
                return oa["url"]
    except Exception:                               # noqa: BLE001
        pass

    # CORE aggregates ~300M items from open-access repositories -- the best
    # remaining source for the green-OA copies (working papers on institutional
    # and subject repositories) that no DOI service links directly. Optional:
    # set CORE_API_KEY (free registration) to enable.
    if CORE_KEY:
        try:
            r = _get("https://api.core.ac.uk/v3/search/works",
                     params={"q": f'doi:"{doi}"', "limit": 1},
                     headers_extra={"Authorization": f"Bearer {CORE_KEY}"})
            if r.ok:
                for w in (r.json().get("results") or []):
                    if w.get("downloadUrl"):
                        return w["downloadUrl"]
        except Exception:                           # noqa: BLE001
            pass

    # A free working-paper version, tried before the arXiv guess.
    # tools/workingpaper.py was written for exactly the classics whose journal
    # version is closed but whose NBER working paper is open -- and grep showed
    # it was imported by nobody, so the fix it delivers was never wired into
    # the resolver chain at all. resolve() runs inside a 6-thread pool, so it
    # reads a prebuilt dict rather than a SQLite connection, which is not safe
    # to share across threads.
    if title and _WP_INDEX is not None:
        try:
            import workingpaper                      # noqa: PLC0415
            workingpaper._NBER_INDEX = _WP_INDEX
            wp = workingpaper.via_nber(title, "", None, con=True)
            if wp:
                return wp
        except Exception:                            # noqa: BLE001
            pass

    # last resort: the paper may sit on arXiv under a different identifier.
    # Measured at only ~3% for this corpus (finance/econ rarely posts there),
    # so it runs last and only on an EXACT title match -- a loose match would
    # attach the wrong paper's full text, which is worse than none.
    if title:
        try:
            r = _get("http://export.arxiv.org/api/query",
                     params={"search_query": 'ti:"' + re.sub(r'["\\\\]', " ", title[:150]) + '"',
                             "max_results": 1})
            if r.ok:
                root = ET.fromstring(r.text)
                for e in root.findall(_ATOM + "entry"):
                    got = (e.findtext(_ATOM + "title") or "").strip()
                    if got and _norm_title(got) == _norm_title(title):
                        for lk in e.findall(_ATOM + "link"):
                            if lk.get("title") == "pdf" and lk.get("href"):
                                return lk.get("href")
                    break
        except Exception:                           # noqa: BLE001
            pass
    return None


def download(uid, pdf_url):
    """Fetch and verify. A paywall page that returns 200 with HTML is a miss,
    not a file -- check the magic bytes, never the content-type header alone."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", uid)[:120]
    dest = OUT / f"{safe}.pdf"
    # create the directory HERE, not only in main(): tools/fulltext.py imports
    # this function directly, and relying on the caller to have prepared the
    # directory failed every download in that path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1000:
        return "ok", str(dest), dest.stat().st_size
    if not _allowed(pdf_url):
        return "robots_disallow", None, 0
    try:
        r = _get(pdf_url, stream=True, allow_redirects=True)
    except Exception:                               # noqa: BLE001
        return "http_error", None, 0
    # 403/429 get their own status rather than a flat http_error: they mean the
    # host is GATING us (entitlement / back off), not that the link is dead --
    # a different fix, so it deserves a different line in the tally
    if r.status_code in (403, 429):
        return f"http_{r.status_code}", None, 0
    if not r.ok:
        return "http_error", None, 0
    declared = int(r.headers.get("content-length") or 0)
    if declared > MAX_MB * 1024 * 1024:
        return "too_big", None, declared      # don't stream it just to reject it
    head = r.raw.read(5, decode_content=True) if hasattr(r, "raw") else b""
    if not head.startswith(b"%PDF"):
        # Content-Type lies constantly in both directions, so the magic bytes
        # decide. Separate a landing page / cookie wall from arbitrary junk:
        # HTML here means a human could probably still get the file.
        peek = (head + next(r.iter_content(512), b"")).lstrip()[:300].lower()
        if b"<html" in peek or b"<!doctype" in peek:
            return "html_not_pdf", None, 0
        return "not_pdf", None, 0
    total = len(head)
    tmp = dest.with_suffix(".part")
    with open(tmp, "wb") as f:
        f.write(head)
        for chunk in r.iter_content(65536):
            f.write(chunk)
            total += len(chunk)
            if total > MAX_MB * 1024 * 1024:
                f.close()
                tmp.unlink(missing_ok=True)
                return "too_big", None, total
    tmp.rename(dest)
    return "ok", str(dest), total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resolve-only", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--shuffle", action="store_true",
                    help="sample randomly (for measuring coverage)")
    ap.add_argument("--url", help="fetch one URL and report why it did or did "
                                  "not work; touches neither the DB nor state")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)

    if args.url:
        # ad-hoc probe: the fastest way to answer "is this link actually a PDF,
        # and if not, what is it?" without queueing a run against the archive
        uid = "adhoc_" + hashlib.sha1(args.url.encode()).hexdigest()[:12]
        st, path, n = download(uid, args.url)
        log(f"{'OK  ' if st == 'ok' else 'FAIL'}  {st}  {args.url}")
        if path:
            log(f"      {n // 1024} KB -> {path}")
        sys.exit(0 if st == "ok" else 1)

    con = store.connect()
    con.executescript(_SCHEMA)

    done = {}
    for uid, st in con.execute("SELECT uid, status FROM pdfs"):
        done[uid] = st
    # meta too: 217 classics carry a DOI added by a later repair pass while
    # keeping their title-hash uid. resolve() grew a doi= argument for exactly
    # them, and fulltext.py passes it -- this caller never did, so every one
    # of them skipped Unpaywall/OpenAlex/S2/CORE and fell back to a single
    # OpenAlex title search.
    global _WP_INDEX
    try:
        import workingpaper                          # noqa: PLC0415
        _WP_INDEX = workingpaper._nber_index(con)
        log(f"[pdf] working-paper index: {len(_WP_INDEX)} NBER titles")
    except Exception as e:                           # noqa: BLE001
        log(f"[pdf] working-paper index unavailable: {type(e).__name__}")
    rows = con.execute("SELECT uid, url, title, meta FROM items").fetchall()
    todo = []
    for uid, url, title, meta in rows:
        st = done.get(uid)
        if st in TERMINAL:
            continue
        if st and not args.retry_failed:
            continue
        try:
            doi = (json.loads(meta) or {}).get("doi") or ""
        except Exception:                            # noqa: BLE001
            doi = ""
        todo.append((uid, url, title, doi))
    if args.shuffle:
        import random
        random.seed(3)
        random.shuffle(todo)
    if args.limit:
        todo = todo[:args.limit]
    log(f"[pdf] {len(rows)} papers archived, {sum(1 for s in done.values() if s=='ok')} "
        f"already fetched, {len(todo)} to attempt")

    # Percentage and ETA, and in CI also as ::notice:: annotations -- the only
    # channel readable from outside while a job is still running. Estimating
    # this run's completion previously meant timing 300 papers and
    # extrapolating, which was wrong by design: those 300 were in table order
    # and resolved on the first ladder step, where the archive as a whole does
    # not.
    prog = Progress(len(todo), "pdf")

    q = queue.Queue()
    for t in todo:
        q.put(t)
    results = []

    def worker():
        while True:
            try:
                # FOUR values. todo holds (uid, url, title, doi) and this
                # unpacked three, so every worker raised ValueError the instant
                # it started and the run finished having attempted nothing.
                #
                # Worse if the arity had happened to match: `doi` was never
                # unpacked here, so resolve() read it as a free variable from
                # the enclosing loop -- the LAST paper's doi, reused for every
                # paper in the queue.
                #
                # It never surfaced because nothing ran main(). The only caller
                # is tools/fulltext.py, which imports resolve() and download()
                # directly and never enters this path.
                uid, url, title, doi = q.get_nowait()
            except queue.Empty:
                return
            try:
                pdf = resolve(uid, url, title, doi)
                if not pdf:
                    results.append((uid, "no_source", None, None, 0))
                elif args.resolve_only:
                    results.append((uid, "resolved", pdf, None, 0))
                else:
                    st, path, n = download(uid, pdf)
                    results.append((uid, st, pdf, path, n))
            except Exception:                        # noqa: BLE001
                results.append((uid, "http_error", None, None, 0))
            finally:
                q.task_done()
                # Progress is ticked under the lock: eight workers incrementing
                # a shared counter is exactly the race that makes a percentage
                # untrustworthy, and an untrustworthy percentage is worse than
                # none because it gets planned against.
                with _lock:
                    _counts["done"] += 1
                    prog.tick()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if not args.resolve_only:
        con.executemany(
            "INSERT OR REPLACE INTO pdfs (uid,status,url,path,bytes) VALUES (?,?,?,?,?)",
            [(u, s, url, p, n) for u, s, url, p, n in results])
        con.commit()

    # A run that attempted work and produced nothing is a failure, however
    # tidy its summary reads. The worker crashes printed tracebacks, the
    # process still exited 0, and CI reported success for a job that resolved
    # 0 of 25,628 papers.
    prog.done()
    if todo and not results:
        log(f"[pdf] FAILED: {len(todo):,} papers queued and NOTHING resolved "
            f"-- every worker died. See the tracebacks above.")
        return 1

    tally = collections.Counter(r[1] for r in results)
    mb = sum(r[4] for r in results) / 1e6
    log("\n=== outcome ===")
    for k, v in tally.most_common():
        log(f"  {v:6d}  {k}")
    gated = sum(tally.get(k, 0) for k in
                ("http_403", "http_429", "html_not_pdf", "robots_disallow"))
    if gated:
        log("")
        log(f"  {gated} of these were GATED rather than missing (403/429, a "
            f"landing page, or robots.txt). A free copy may well exist -- that "
            f"is a text-and-data-mining entitlement conversation with those "
            f"hosts, not a scraping problem.")
    total_ok = sum(1 for s in done.values() if s == "ok") + tally.get("ok", 0)
    log(f"\n  {total_ok}/{len(rows)} papers now have a local PDF "
        f"({100.0*total_ok/max(1,len(rows)):.1f}%), {mb:.0f} MB fetched this run")
    log(f"  stored in {OUT.resolve()}")


if __name__ == "__main__":
    main()
