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
rate-limited per host and verified to be real PDFs (a paywall interstitial that
returns 200 with HTML is recorded as a miss, not saved as a corrupt file).

  python tools/fetch_pdfs.py                 # resolve + download everything new
  python tools/fetch_pdfs.py --resolve-only  # find URLs, download nothing
  python tools/fetch_pdfs.py --limit 200     # small trial run
  python tools/fetch_pdfs.py --retry-failed  # re-attempt previous failures
"""

import argparse
import collections
import json
import os
import pathlib
import queue
import re
import sqlite3
import sys
import threading
import time

import requests
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import store  # noqa: E402

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

ARXIV = re.compile(r"arxiv\.org/abs/([^/?#]+)", re.I)
NBER = re.compile(r"nber\.org/papers/w(\d+)", re.I)
IS_PDF = re.compile(r"\.pdf(\?|$)", re.I)
REPEC_ARXIV = re.compile(r"RePEc:arx:papers:([\d.]+)", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdfs (
    uid    TEXT PRIMARY KEY,
    status TEXT,               -- ok | no_source | http_error | not_pdf | too_big
    url    TEXT,
    path   TEXT,
    bytes  INTEGER,
    ts     TEXT DEFAULT (datetime('now'))
);
"""

_lock = threading.Lock()
_last_hit: dict[str, float] = {}
_counts: collections.Counter = collections.Counter()


def log(m):
    print(m, flush=True)


def _host(u):
    m = re.findall(r"https?://([^/]+)", u or "")
    return re.sub(r"^www\.", "", m[0]).lower() if m else ""


def _polite(u):
    """Space out requests per host so a bulk run stays a good citizen."""
    h = _host(u)
    delay = HOST_DELAY.get(h, HOST_DELAY["default"])
    with _lock:
        wait = delay - (time.time() - _last_hit.get(h, 0))
        if wait > 0:
            time.sleep(wait)
        _last_hit[h] = time.time()


def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _get(u, headers_extra=None, **kw):
    _polite(u)
    h = {"User-Agent": UA}
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


def resolve(uid, url, title=None):
    """Return a PDF url for this paper, or None if there's no free copy."""
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

    doi = uid[4:] if uid.startswith("doi:") else None
    if not doi:                                     # title-hash uid -> identify it
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
    try:
        r = _get(pdf_url, stream=True, allow_redirects=True)
    except Exception as e:                          # noqa: BLE001
        return "http_error", None, 0
    if not r.ok:
        return "http_error", None, 0
    head = r.raw.read(5, decode_content=True) if hasattr(r, "raw") else b""
    if not head.startswith(b"%PDF"):
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
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    con = store.connect()
    con.executescript(_SCHEMA)

    done = {}
    for uid, st in con.execute("SELECT uid, status FROM pdfs"):
        done[uid] = st
    rows = con.execute("SELECT uid, url, title FROM items").fetchall()
    todo = []
    for uid, url, title in rows:
        st = done.get(uid)
        if st == "ok":
            continue
        if st and not args.retry_failed:
            continue
        todo.append((uid, url, title))
    if args.shuffle:
        import random
        random.seed(3)
        random.shuffle(todo)
    if args.limit:
        todo = todo[:args.limit]
    log(f"[pdf] {len(rows)} papers archived, {sum(1 for s in done.values() if s=='ok')} "
        f"already fetched, {len(todo)} to attempt")

    q = queue.Queue()
    for t in todo:
        q.put(t)
    results = []

    def worker():
        while True:
            try:
                uid, url, title = q.get_nowait()
            except queue.Empty:
                return
            try:
                pdf = resolve(uid, url, title)
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
                with _lock:
                    _counts["done"] += 1
                    d = _counts["done"]
                if d % 100 == 0:
                    ok = sum(1 for r in results if r[1] == "ok")
                    log(f"[pdf] {d}/{len(todo)} attempted, {ok} downloaded")

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

    tally = collections.Counter(r[1] for r in results)
    mb = sum(r[4] for r in results) / 1e6
    log("\n=== outcome ===")
    for k, v in tally.most_common():
        log(f"  {v:6d}  {k}")
    total_ok = sum(1 for s in done.values() if s == "ok") + tally.get("ok", 0)
    log(f"\n  {total_ok}/{len(rows)} papers now have a local PDF "
        f"({100.0*total_ok/max(1,len(rows)):.1f}%), {mb:.0f} MB fetched this run")
    log(f"  stored in {OUT.resolve()}")


if __name__ == "__main__":
    main()
