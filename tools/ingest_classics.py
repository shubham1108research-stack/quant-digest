#!/usr/bin/env python3
"""Ingest the curated classics into the archive as first-class items.

Today canon.py and docs/classics.json are display-only lists. Measured: 312 of
443 curated papers are NOT in the items table, and ZERO of the 96 canon papers
are -- so Ask has never been able to retrieve or cite Fama-French, Kyle or
Koijen. They exist as a tab, not as corpus.

Ingesting them gives each a uid, an embedding, LLM scores and sleeve labels
through the normal pipeline, which makes them retrievable by Ask with or
without a PDF. It is also the prerequisite for attaching GROBID passages later:
passages are keyed by uid, and these papers had none.

Identity matters here, so each paper is looked up in OpenAlex by title to
recover a DOI, a real abstract, the canonical author list and the venue. The
one-line rationale in canon.py is an editorial note, not an abstract, and would
make a poor embedding.

Papers are dated by their PUBLICATION year, so the portal's recency windows
exclude them from Recent/Monthly automatically -- they belong in the archive and
in retrieval, not in this week's reading.

  python tools/ingest_classics.py --dry-run    # report, write nothing
  python tools/ingest_classics.py
"""

import argparse
import json
import pathlib
import re
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import canon   # noqa: E402
import store   # noqa: E402
import oa as oa_auth   # noqa: E402

MAILTO = "upadhyays1108@gmail.com"
UA = {"User-Agent": f"quant-digest/1.0 (mailto:{MAILTO})"}


def log(m):
    print(m, flush=True)


def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "")).lower().strip()


def deinvert(inv):
    """OpenAlex returns abstracts as {word: [positions]}."""
    if not inv:
        return ""
    slots = {}
    for w, ps in inv.items():
        for p in ps:
            slots[p] = w
    return " ".join(slots[i] for i in sorted(slots)) if slots else ""


_throttled = {"n": 0}


def lookup_crossref(title, year):
    """Crossref first for the classics: these are published articles with
    registered DOIs, and Crossref tolerates volume far better than OpenAlex,
    which hard-throttled this workload after ~77 lookups. Returns the same
    shape fields the caller needs, or None."""
    try:
        r = requests.get("https://api.crossref.org/works",
                         params={"query.bibliographic": title[:200], "rows": 5,
                                 "select": "DOI,title,author,issued,"
                                           "container-title,abstract,"
                                           "is-referenced-by-count",
                                 "mailto": MAILTO},
                         headers=UA, timeout=45)
        if not r.ok:
            log(f"   ! Crossref HTTP {r.status_code}")
            return None
        want = norm(title)
        for w in r.json()["message"]["items"]:
            got = norm((w.get("title") or [""])[0])
            if not got:
                continue
            if got != want and not (len(want) > 25
                                    and (want in got or got in want)):
                continue
            yr = ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0]
            if year and yr and abs(int(yr) - int(year)) > 3:
                continue                          # same title, wrong paper
            names = [" ".join(x for x in (a.get("given"), a.get("family")) if x)
                     for a in (w.get("author") or [])]
            abstract = re.sub(r"<[^>]+>", " ", w.get("abstract") or "")
            return {"doi": w.get("DOI", ""),
                    "abstract": re.sub(r"\s+", " ", abstract).strip(),
                    "authors": ", ".join(n for n in names if n)[:300],
                    "journal": (w.get("container-title") or [""])[0],
                    "cites": w.get("is-referenced-by-count")}
    except Exception as e:                        # noqa: BLE001
        log(f"   ! Crossref {type(e).__name__}")
    return None


def lookup(title, year):
    """Resolve a curated title to its OpenAlex record: DOI, abstract, authors,
    venue. Requires a real title match -- a loose hit would attach the wrong
    paper's identity, which is worse than leaving it unresolved.

    Throttling is reported, never swallowed. The first run of this tool
    returned 45/77 in the first quartile and 0/77 in every quartile after:
    OpenAlex had started refusing, and a bare `return None` made that
    indistinguishable from "paper not found".
    """
    if _throttled["n"] >= 3:
        return None                # circuit open: OpenAlex is refusing this IP
    for attempt in range(2):       # bulk loop -- fail fast, never grind
        try:
            r = requests.get("https://api.openalex.org/works", headers=oa_auth.headers(UA),
                             params={"filter": "title.search:" + title[:180],
                                     "select": "id,doi,title,publication_year,"
                                               "authorships,abstract_inverted_index,"
                                               "primary_location,cited_by_count",
                                     "per-page": 5, "mailto": MAILTO},
                              timeout=45)
        except Exception as e:                    # noqa: BLE001
            log(f"   ! network {type(e).__name__}; retrying")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (429, 403, 503):
            _throttled["n"] += 1
            if _throttled["n"] == 3:
                log("   ! OpenAlex throttling repeatedly -- disabling it for "
                    "the rest of this run; Crossref carries on alone")
            time.sleep(3)
            continue
        if not r.ok:
            log(f"   ! OpenAlex HTTP {r.status_code}")
            return None
        break
    else:
        return None
    try:
        want = norm(title)
        for w in r.json().get("results", []):
            got = norm(w.get("title"))
            if not got:
                continue
            # exact, or one contains the other (subtitle differences)
            if got == want or (len(want) > 25 and (want in got or got in want)):
                if year and w.get("publication_year") and \
                        abs(int(w["publication_year"]) - int(year)) > 3:
                    continue                      # same title, wrong paper
                return w
    except Exception:                             # noqa: BLE001
        return None
    return None


def build(title, authors, year, url, source, extra):
    it = {"title": title, "url": url or "", "source": source,
          "section": "1", "authors": authors or "",
          "date": f"{year}-01-01" if year else "", "classic": True}
    it.update(extra)
    return it


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--repair", action="store_true",
                    help="re-look-up already-ingested classics "
                         "that have no DOI (throttled first run)")
    args = ap.parse_args()

    con = store.connect()

    if args.repair:
        # already ingested, but the first run was throttled after ~77 lookups,
        # so most classics carry no DOI and no abstract. Re-look-up only those.
        broken = []
        for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
            try:
                d = json.loads(meta)
            except Exception:                      # noqa: BLE001
                continue
            if d.get("classic") and not d.get("doi"):
                broken.append((uid, title, d))
        if args.limit:
            broken = broken[:args.limit]
        log(f"[repair] {len(broken)} classic items lack a DOI")
        fixed = gotabs = 0
        for i, (uid, title, d) in enumerate(broken, 1):
            year = (d.get("date") or "")[:4]
            patch = {}
            cr = lookup_crossref(title, year or None)
            if cr:
                if cr.get("doi"):
                    patch["doi"] = cr["doi"]
                    if not d.get("url"):
                        patch["url"] = f"https://doi.org/{cr['doi']}"
                if cr.get("abstract"):
                    patch["abstract"] = cr["abstract"][:6000]
                    gotabs += 1
                if cr.get("authors"):
                    patch["authors"] = cr["authors"]
                if cr.get("journal"):
                    patch.setdefault("journal", cr["journal"])
                if cr.get("cites") is not None:
                    patch.setdefault("cites", cr["cites"])
            if not patch.get("abstract"):          # OpenAlex only for the gap
                w = lookup(title, year or None)
                if w:
                    doi = (w.get("doi") or "").replace("https://doi.org/", "")
                    if doi and not patch.get("doi"):
                        patch["doi"] = doi
                    abstract = deinvert(w.get("abstract_inverted_index"))
                    if abstract:
                        patch["abstract"] = abstract[:6000]
                        gotabs += 1
            if not patch:
                time.sleep(0.8)
                continue
            if patch and store.update_meta(con, uid, patch):
                fixed += 1
            if i % 20 == 0:
                con.commit()       # partial progress survives a timeout
                log(f"[repair] {i}/{len(broken)} ({fixed} fixed, "
                    f"openalex_throttles={_throttled['n']})")
            time.sleep(1.2)                        # slower than the first run
        con.commit()
        log(f"\n[repair] fixed {fixed}/{len(broken)}; {gotabs} gained an abstract")
        log(f"[repair] throttle waits: {_throttled['n']}")
        return

    have = {norm(t) for (t,) in con.execute("SELECT title FROM items")}

    cand = []
    for topic, rows in canon.CANON.items():
        for (title, hint, year, typ, why) in rows:
            cand.append(dict(title=title, authors=hint, year=year, url="",
                             source="classic:canon",
                             extra={"canon_type": typ, "canon_topic": topic,
                                    "canon_why": why, "tier": "T1"}))
    cls = json.loads(pathlib.Path("docs/classics.json").read_text(encoding="utf-8"))
    for key in ("overall", "modern"):
        for x in (cls.get(key) or []):
            if not x.get("title"):
                continue
            cand.append(dict(title=x["title"], authors=x.get("authors", ""),
                             year=x.get("year"), url=x.get("url", ""),
                             source="classic:cited",
                             extra={"journal": x.get("journal", ""),
                                    "cites": x.get("cites"),
                                    "summary": x.get("summary", ""),
                                    "tier": "T1"}))

    seen, todo = set(), []
    for c in cand:
        k = norm(c["title"])
        if not k or k in seen:
            continue
        seen.add(k)
        if k in have:
            continue                              # already in the archive
        todo.append(c)
    log(f"[classics] {len(cand)} curated entries, {len(seen)} unique, "
        f"{len(todo)} not yet in the archive")
    if args.limit:
        todo = todo[:args.limit]

    built, resolved, no_abstract = [], 0, 0
    for i, c in enumerate(todo, 1):
        w = lookup(c["title"], c.get("year"))
        extra = dict(c["extra"])
        url, authors = c["url"], c["authors"]
        if w:
            resolved += 1
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            if doi:
                extra["doi"] = doi
                url = url or f"https://doi.org/{doi}"
            abstract = deinvert(w.get("abstract_inverted_index"))
            if abstract:
                extra["abstract"] = abstract[:6000]
            names = [a.get("author", {}).get("display_name", "")
                     for a in (w.get("authorships") or [])]
            if names:
                authors = ", ".join(n for n in names if n)[:300]
            loc = (w.get("primary_location") or {}).get("source") or {}
            if loc.get("display_name"):
                extra.setdefault("journal", loc["display_name"])
            if w.get("cited_by_count") is not None:
                extra.setdefault("cites", w["cited_by_count"])
        if not extra.get("abstract"):
            no_abstract += 1
        built.append(build(c["title"], authors, c.get("year"), url,
                           c["source"], extra))
        if i % 25 == 0:
            log(f"[classics] {i}/{len(todo)} looked up ({resolved} resolved)")
        time.sleep(0.9)                           # OpenAlex politeness

    log(f"\n[classics] resolved {resolved}/{len(built)} in OpenAlex; "
        f"{len(built)-no_abstract} have a real abstract")

    if args.dry_run:
        log("[classics] dry run -- nothing written")
        for b in built[:5]:
            log(f"   {b.get('doi','(no doi)'):<28} {b['title'][:60]}")
        return

    fresh = store.filter_new(con, built)
    store.save(con, fresh)
    log(f"[classics] inserted {len(fresh)} items")
    total = con.execute("SELECT count(*) FROM items").fetchone()[0]
    log(f"[classics] archive now {total} papers")


if __name__ == "__main__":
    main()
