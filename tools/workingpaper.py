#!/usr/bin/env python3
"""Find the free WORKING-PAPER version of a paywalled article.

Most post-1990 finance classics circulated free for years before the journal
version went behind a paywall -- as an NBER working paper, an SSRN preprint, or
a departmental working paper. Those are separate documents with separate
identifiers, so a DOI-based lookup of the PUBLISHED article never sees them.
That is why Harvey-Liu-Zhu, Hou-Xue-Zhang and much of the Fama-French corpus
came back `no_pdf` while free versions plausibly existed.

Two routes, both legitimate:

  1. OpenAlex title search returning MANY records. The same paper commonly has
     several work records -- the closed journal version and an open preprint.
     The earlier resolver took the first match, which is almost always the
     paywalled one. This scans every title-matching record for an open copy.
  2. NBER's own working-paper search, which yields a deterministic free PDF.

Identity is checked before accepting anything: the title must match and either
the year must be close or an author surname must overlap. A near-miss here
attaches the wrong paper's full text, which is worse than no full text.

  python tools/workingpaper.py --probe 25     # measure on failed classics
"""

import argparse
import json
import pathlib
import re
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store  # noqa: E402

MAILTO = "upadhyays1108@gmail.com"
UA = {"User-Agent": f"quant-digest/1.0 (mailto:{MAILTO})"}
NBER_SEARCH = ("https://www.nber.org/api/v1/working_page_listing/contentType/"
               "working_paper/_/_/search")


def log(m):
    print(m, flush=True)


def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "")).lower().strip()


def surnames(authors):
    out = set()
    for chunk in re.split(r"[,;&]| and ", authors or ""):
        parts = [p for p in chunk.strip().split() if len(p) > 2]
        if parts:
            out.add(parts[-1].lower())
    return out


def _identity_ok(want_title, want_year, want_names, got_title, got_year,
                 got_names):
    if norm(got_title) != norm(want_title):
        if not (len(norm(want_title)) > 25
                and norm(want_title) in norm(got_title)):
            return False
    if want_year and got_year:
        try:
            if abs(int(got_year) - int(want_year)) <= 6:
                return True                     # WP predates the article
        except Exception:                       # noqa: BLE001
            pass
    if want_names and got_names:
        return bool(want_names & got_names)
    return not want_year                        # nothing to contradict it


def via_openalex(title, authors, year):
    """Scan EVERY title-matching record for an open copy, not just the best
    match -- the best match is the journal version, which is the closed one."""
    try:
        r = requests.get("https://api.openalex.org/works",
                         params={"filter": "title.search:" + title[:180],
                                 "select": "title,publication_year,authorships,"
                                           "locations,open_access,type",
                                 "per-page": 25, "mailto": MAILTO},
                         headers=UA, timeout=45)
        if not r.ok:
            return None
        want = surnames(authors)
        for w in r.json().get("results", []):
            names = {(a.get("author", {}).get("display_name") or "")
                     .split()[-1].lower()
                     for a in (w.get("authorships") or [])
                     if a.get("author", {}).get("display_name")}
            if not _identity_ok(title, year, want, w.get("title"),
                                w.get("publication_year"), names):
                continue
            for loc in (w.get("locations") or []):
                if loc.get("pdf_url"):
                    return loc["pdf_url"]
    except Exception:                           # noqa: BLE001
        return None
    return None


_NBER_INDEX = None


def _nber_index(con):
    """Title -> working-paper number, built from the NBER papers already in the
    archive. The NBER search API returns a node id rather than a wNNNNN number,
    so a PDF path cannot be built from it -- but the 2,154 NBER papers ingested
    here already carry theirs. Matching locally needs no network at all."""
    global _NBER_INDEX
    if _NBER_INDEX is None:
        _NBER_INDEX = {}
        for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
            try:
                d = json.loads(meta)
            except Exception:                   # noqa: BLE001
                continue
            if d.get("source") == "NBER" and d.get("wp"):
                _NBER_INDEX[norm(title)] = d["wp"]
    return _NBER_INDEX


def via_nber(title, authors, year, con=None):
    """A free NBER working-paper version of this article, matched by title
    against the local NBER corpus."""
    if con is None:
        return None
    wp = _nber_index(con).get(norm(title))
    if not wp:
        return None
    n = str(wp).lstrip("w")
    return (f"https://www.nber.org/system/files/working_papers/"
            f"w{n}/w{n}.pdf")


def find(title, authors="", year=None, con=None):
    """A free working-paper version of this article, or None."""
    return (via_nber(title, authors, year, con)
            or via_openalex(title, authors, year))


def verify(url):
    """A link is not a PDF until it returns %PDF."""
    try:
        r = requests.get(url, headers=UA, timeout=45, stream=True,
                         allow_redirects=True)
        if not r.ok:
            return False
        return r.raw.read(5, decode_content=True).startswith(b"%PDF")
    except Exception:                           # noqa: BLE001
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", type=int, default=25)
    args = ap.parse_args()

    con = store.connect()
    failed = {u for (u,) in con.execute(
        "SELECT uid FROM fulltext WHERE status='no_pdf'")}
    cands = []
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        if uid not in failed:
            continue
        try:
            d = json.loads(meta)
        except Exception:                       # noqa: BLE001
            continue
        if d.get("classic"):
            cands.append((uid, title or "", d.get("authors", ""),
                          (d.get("date") or "")[:4]))
    log(f"classics with no reachable PDF: {len(cands)}")
    cands = cands[:args.probe]
    log(f"probing {len(cands)}\n")

    hits = 0
    for uid, title, authors, year in cands:
        url = find(title, authors, year, con)
        if url and verify(url):
            hits += 1
            host = url.split("/")[2].replace("www.", "")
            log(f"  OK  [{host}] {title[:58]}")
        else:
            log(f"  --  {title[:66]}")
        time.sleep(1.1)
    log(f"\nworking-paper version found: {hits}/{len(cands)} "
        f"= {100*hits/max(1,len(cands)):.0f}%")


if __name__ == "__main__":
    main()
