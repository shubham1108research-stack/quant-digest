#!/usr/bin/env python3
"""Ingest specific papers by identifier, on demand.

This is the write end of the portal's "add to the archive" button. Ask searches
the outside literature when the archive does not cover a question; anything
worth keeping is dispatched here as a list of ids, and comes in through the
SAME path as every other paper -- one uid, a real abstract, then the normal
scorer and sleeve labeller on the next rescore.

Identifiers only. A title is not an identity: two papers share one, versions
differ, and a fuzzy title match quietly ingests the wrong paper. Everything
here is doi: or arxiv:, resolved against the authoritative record.

  python tools/ingest_one.py --ids doi:10.1111/jofi.12345,arxiv:2401.01234
  python tools/ingest_one.py --ids arxiv:2401.01234 --dry-run
"""

import argparse
import json
import pathlib
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store   # noqa: E402
import oa as oa_auth   # noqa: E402

MAILTO = "upadhyays1108@gmail.com"
UA = {"User-Agent": f"quant-digest/1.0 (mailto:{MAILTO})"}
ATOM = "{http://www.w3.org/2005/Atom}"


def log(m):
    print(m, flush=True)


def deinvert(inv):
    """OpenAlex returns abstracts as {word: [positions]}."""
    if not inv:
        return ""
    slots = {}
    for w, ps in (inv or {}).items():
        for p in ps:
            slots[p] = w
    return " ".join(slots[k] for k in sorted(slots))


def from_openalex(doi):
    r = requests.get(f"https://api.openalex.org/works/doi:{doi}",
                     headers=oa_auth.headers(UA), params={"mailto": MAILTO}, timeout=30)
    if not r.ok:
        return None
    w = r.json()
    names = [a.get("author", {}).get("display_name", "")
             for a in (w.get("authorships") or [])]
    loc = (w.get("primary_location") or {}).get("source") or {}
    year = w.get("publication_year")
    return {
        "title": w.get("title") or w.get("display_name") or "",
        "authors": ", ".join(n for n in names if n)[:300],
        "url": (w.get("primary_location") or {}).get("landing_page_url")
               or f"https://doi.org/{doi}",
        "date": w.get("publication_date") or (f"{year}-01-01" if year else ""),
        "doi": doi,
        "abstract": deinvert(w.get("abstract_inverted_index"))[:6000],
        "journal": loc.get("display_name") or "",
        "cites": w.get("cited_by_count") or 0,
        "source": f"journal:{loc.get('display_name')}" if loc.get("display_name") else "OpenAlex",
    }


def from_arxiv(aid):
    r = requests.get("http://export.arxiv.org/api/query",
                     params={"id_list": aid, "max_results": 1},
                     headers=UA, timeout=30)
    if not r.ok:
        return None
    root = ET.fromstring(r.text.encode("utf-8"))
    e = root.find(f"{ATOM}entry")
    if e is None:
        return None
    def txt(tag):
        n = e.find(f"{ATOM}{tag}")
        return re.sub(r"\s+", " ", (n.text or "")).strip() if n is not None else ""
    names = [n.text.strip() for n in e.iter(f"{ATOM}name") if n.text]
    return {
        "title": txt("title"),
        "authors": ", ".join(names[:12])[:300],
        "url": f"https://arxiv.org/abs/{aid}",
        "date": (txt("published") or "")[:10],
        "doi": f"10.48550/arxiv.{aid}",
        "abstract": txt("summary")[:6000],
        "journal": "arXiv",
        "cites": 0,
        "source": "arXiv",
    }


def from_crossref(doi):
    """Crossref record for a DOI. Complements OpenAlex rather than replacing it.

    OpenAlex is richer where it has a work -- citation counts, a resolved
    journal -- but it LAGS on fresh preprints, and SSRN is the case that
    matters here: Crossref indexes an SSRN posting the same day and carries its
    abstract, while OpenAlex may have nothing for weeks.

    That gap is not theoretical. ssrn.7313339 ("Is Sector Rotation Causal?",
    posted 2026-08-20) resolved through OpenAlex with no abstract, so its
    vector was built from the title alone -- which the src provenance column
    reported as "title 1" the first time it ran. Crossref had the abstract all
    along.
    """
    r = requests.get(f"https://api.crossref.org/works/{doi}",
                     params={"mailto": MAILTO}, headers=UA, timeout=30)
    if not r.ok:
        return None
    w = (r.json() or {}).get("message") or {}
    title = " ".join(w.get("title") or []).strip()
    if not title:
        return None
    names = [" ".join(x for x in [a.get("given"), a.get("family")] if x)
             for a in (w.get("author") or [])[:12]]
    # created BEFORE issued, and via the collector's own helper rather than a
    # second copy of the logic. issued for a fresh SSRN posting is often just
    # [[2026]], which yields a bare "2026" -- unsortable against real dates and
    # useless as a "%Y-%m" monthly key. created carries the actual posting day.
    date = _cr_date(w.get("created") or w.get("issued") or {})
    return {
        "title": title,
        "authors": ", ".join(n for n in names if n)[:300],
        "url": w.get("URL") or f"https://doi.org/{doi}",
        "date": date,
        "doi": doi,
        # Crossref abstracts are JATS-wrapped; sources._clean unescapes then
        # strips, in that order, because publishers double-encode.
        "abstract": _clean_abstract(w.get("abstract") or "")[:6000],
        "journal": " ".join(w.get("container-title") or []) or "",
        "cites": w.get("is-referenced-by-count") or 0,
        "source": "SSRN (via Crossref)" if str(doi).startswith("10.2139")
                  else "Crossref",
    }


def _cr_date(parts_obj):
    """Crossref date-parts -> zero-padded ISO, via sources._cr_date_parts."""
    try:
        import sources
        return sources._cr_date_parts(parts_obj)
    except Exception:                                # noqa: BLE001
        parts = (parts_obj.get("date-parts") or [[]])[0]
        return "-".join(("%02d" % int(x)) if i else str(int(x))
                        for i, x in enumerate(parts[:3])) if parts else ""


def _clean_abstract(text):
    """Reuse the collector's JATS/entity cleaning rather than a second copy."""
    try:
        import sources
        return sources._clean(text)
    except Exception:                                # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", text or "").strip()


def resolve(ident):
    ident = ident.strip()
    if ident.lower().startswith("doi:"):
        doi = ident[4:].strip().lower()
        rec = from_openalex(doi)
        # A record with no abstract is a title-only vector, which is a much
        # weaker object in retrieval than it looks -- and indistinguishable
        # from a good one once stored. Try the other authority before
        # accepting one.
        if not rec or not (rec.get("abstract") or "").strip():
            alt = from_crossref(doi)
            if alt and (alt.get("abstract") or "").strip():
                if rec:
                    # keep OpenAlex's richer metadata, take Crossref's abstract
                    rec["abstract"] = alt["abstract"]
                    log(f"[ingest] {doi}: abstract from Crossref "
                        f"(OpenAlex had none)")
                    return rec
                log(f"[ingest] {doi}: resolved via Crossref "
                    f"(OpenAlex had no record)")
                return alt
            if not rec:
                return alt
        return rec
    if ident.lower().startswith("arxiv:"):
        return from_arxiv(re.sub(r"v\d+$", "", ident[6:].strip()))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True,
                    help="comma-separated doi: / arxiv: identifiers")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ids = [i for i in (x.strip() for x in args.ids.split(",")) if i]
    if not ids:
        log("[ingest] nothing to do")
        return
    log(f"[ingest] {len(ids)} identifier(s) requested")

    con = store.connect()
    built, failed = [], []
    for i, ident in enumerate(ids, 1):
        try:
            rec = resolve(ident)
        except Exception as e:                      # noqa: BLE001
            rec = None
            log(f"[ingest] {ident}: {type(e).__name__}: {str(e)[:160]}")
        if not rec or not rec.get("title"):
            failed.append(ident)
            log(f"[ingest] {ident}: not resolved")
        else:
            # section 1 and a real publication date, so the portal's recency
            # windows treat it like any other paper rather than as "new today"
            rec.setdefault("section", "1")
            rec["added_on_request"] = True
            built.append(rec)
            log(f"[ingest] {ident} -> {rec['title'][:70]}")
        if i < len(ids):
            time.sleep(0.9)                         # OpenAlex/arXiv politeness

    if args.dry_run:
        log(f"[ingest] dry run -- {len(built)} would be written")
        return

    fresh = store.filter_new(con, built)
    store.save(con, fresh)
    dupes = len(built) - len(fresh)
    log(f"\n[ingest] inserted {len(fresh)}"
        + (f", {dupes} already in the archive" if dupes else "")
        + (f", {len(failed)} unresolved" if failed else ""))
    total = con.execute("SELECT count(*) FROM items").fetchone()[0]
    log(f"[ingest] archive now {total} papers")
    if failed:
        log("[ingest] unresolved: " + ", ".join(failed))


if __name__ == "__main__":
    main()
