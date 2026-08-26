#!/usr/bin/env python3
"""Harvest working-paper PDFs from watched authors' own websites.

WHY THIS EXISTS AFTER I ARGUED IT WAS LOW VALUE. The earlier argument was that
fetch_pdfs.py already runs Unpaywall, which indexes author self-archived
copies, so 53 bespoke parsers would buy only the residual. That still holds for
discovering an arbitrary author. It does not hold here, because the list is
CURATED: 82 names chosen by the desk, 47 of them keeping a genuine publication
page. A working paper posted on a professor's site months before it reaches
SSRN or a journal is not in Unpaywall yet, and that early window is most of the
value.

WHAT A URL IS DECIDES WHAT WE DO WITH IT, so data/author_sites.csv carries a
`kind` instead of pretending every row is a website:

    personal  47  a real publication page. Crawled.
    profile   15  an institutional directory entry. Crawled shallowly; these
                  sometimes link a CV or a personal site, usually nothing.
    firm      11  the person's bio page held no papers at all, so the row now
                  points at that FIRM's research library. Every replacement was
                  verified to answer 200 -- Winton's /research is a 404 and was
                  not invented to fill the column.
    skip       9  nothing to crawl, and the reason is recorded: Google Scholar
                  prohibits scraping, academia.edu is login-walled, and Stephen
                  Ross died in 2017 so his row is a Wikipedia link.

ROBOTS IS READ, NOT PATTERN-MATCHED. Checking the list I flagged two sites as
blanket disallows and both were wrong: bryankellyacademic.org says Allow: / and
only blocks lightbox parameters, and theinvestmentcapm.com blocks NerdyBot plus
two paths. This reuses fetch_pdfs._allowed, which fetches and parses the file.

NOTHING IS INGESTED. Output is candidates in export/, split into PDFs that
match a paper we already hold and PDFs that match nothing. An author's own site
is a strong prior, not a label, and the archive already carries what
unreviewed sweeps produce.

    python tools/author_sites.py --dry-run
    python tools/author_sites.py --kind personal --limit 10
"""

import argparse
import collections
import csv
import io
import json
import pathlib
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import store                                   # noqa: E402
from fetch_pdfs import _allowed, UA            # noqa: E402
from progress import Progress                  # noqa: E402

LIST = pathlib.Path("data/author_sites.csv")
OUT = pathlib.Path("export/author_site_pdfs.json")
PAUSE = 2.0                     # per page; these are personal servers
MAX_PAGES = 3                   # landing page plus up to two publication pages
PUBS = re.compile(r"(publication|research|papers|working.?paper|cv|vita)", re.I)


def log(m):
    print(m, flush=True)


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _fetch(url):
    return requests.get(url, headers={"User-Agent": UA}, timeout=25,
                        allow_redirects=True)


def _links(html, base):
    """(absolute href, anchor text) for every link on a page."""
    out = []
    for m in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         html, re.I | re.S):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        out.append((urljoin(base, href), re.sub(r"\s+", " ", text).strip()))
    return out


def harvest(name, url):
    """{pdf_url: anchor text} found across this author's pages."""
    found, seen = {}, set()
    queue = [url]
    while queue and len(seen) < MAX_PAGES:
        page = queue.pop(0)
        if page in seen:
            continue
        seen.add(page)
        if not _allowed(page):
            log(f"[sites] {name}: robots.txt disallows {page}")
            continue
        try:
            r = _fetch(page)
            if not r.ok or "html" not in (r.headers.get("content-type") or ""):
                continue
        except Exception as e:                            # noqa: BLE001
            log(f"[sites] {name}: {type(e).__name__} on {page}")
            continue
        host = urlsplit(page).netloc
        for href, text in _links(r.text, page):
            if re.search(r"\.pdf(\?|$)", href, re.I):
                # The anchor text is the paper title far more often than not.
                # Where it is "PDF" or "[link]" the filename is the next best
                # guess -- a title is what makes the match possible at all.
                label = text if len(text) > 12 else re.sub(
                    r"[-_]+", " ", pathlib.Path(urlsplit(href).path).stem)
                found.setdefault(href, label)
            elif (len(seen) < MAX_PAGES and PUBS.search(text or "")
                  and urlsplit(href).netloc == host and href not in seen):
                queue.append(href)
        time.sleep(PAUSE)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="personal,profile,firm",
                    help="comma-separated kinds to crawl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not LIST.exists():
        raise SystemExit(f"[sites] {LIST} is missing")
    rows = list(csv.DictReader(io.open(LIST, encoding="utf-8")))
    kinds = {k.strip() for k in args.kind.split(",")}
    todo = [r for r in rows if r["kind"] in kinds]
    skipped = [r for r in rows if r["kind"] == "skip"]
    log(f"[sites] {len(rows)} authors listed; crawling {len(todo)} "
        f"({', '.join(sorted(kinds))}); {len(skipped)} marked skip")
    for r in skipped:
        log(f"[sites]   skip {r['name']:<24} {r['note']}")
    if args.limit:
        todo = todo[:args.limit]
    if args.dry_run:
        log(f"[sites] DRY RUN: would fetch up to {len(todo) * MAX_PAGES} pages")
        return 0

    con = store.connect()
    by_title = {}
    for uid, title, meta in con.execute("SELECT uid,title,meta FROM items"):
        if title:
            by_title.setdefault(_norm(title)[:70], uid)
    log(f"[sites] {len(by_title):,} archive titles to match against")

    prog = Progress(len(todo), "sites", every_s=30)
    hits, fresh = [], []
    for r in todo:
        for pdf, label in harvest(r["name"], r["url"]).items():
            uid = by_title.get(_norm(label)[:70])
            rec = {"author": r["name"], "category": r["category"],
                   "pdf": pdf, "label": label, "uid": uid}
            (hits if uid else fresh).append(rec)
        prog.tick()
    prog.done()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"matched": hits, "unmatched": fresh}, indent=1),
                   encoding="utf-8")
    log(f"\n[sites] {len(hits):,} PDFs matched a paper we already hold "
        f"-- a pdf_url for rows that lack one")
    log(f"[sites] {len(fresh):,} PDFs matched nothing -- candidate NEW papers")
    for a, n in collections.Counter(
            h["author"] for h in hits + fresh).most_common(10):
        log(f"[sites]   {n:>4}  {a}")
    log(f"[sites] written to {OUT} -- candidates only, nothing ingested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
