#!/usr/bin/env python3
"""Parse PDFs you supply yourself into the full-text corpus.

Automated resolution reaches ~19 of the 309 classics -- the pre-2010 flagship
journals have no free authorized copy, and every discovery mechanism tried
(landing pages, working-paper versions, search) found the same unauthorized
mirrors wearing different hats. So the remaining route is you: whatever you
obtain through your own library access or subscriptions goes in a folder, and
this parses it exactly like any other paper.

Matching a dropped file to an archive entry:
  1. filename contains a DOI            e.g. 10.1016_0304-405X(93)90023-5.pdf
  2. filename is a uid                  e.g. doi_10.3386_w25398.pdf
  3. otherwise GROBID reads the title off the header and matches by title

Papers matched this way get passages in docs/ft/ keyed by their archive uid, so
Ask treats them like anything else -- quotable at depth:full, with section
labels.

  mkdir -p pdfs/manual && cp ~/Downloads/*.pdf pdfs/manual/
  python tools/ingest_dropped.py
"""

import argparse
import glob
import json
import os
import pathlib
import re
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fulltext  # noqa: E402
import store     # noqa: E402

DROP = pathlib.Path("pdfs/manual")
TEI = "{http://www.tei-c.org/ns/1.0}"
DOI_RE = re.compile(r"(10\.\d{4,9}[/_][-._;()/:A-Za-z0-9]+)")


def log(m):
    print(m, flush=True)


def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "")).lower().strip()


def header_title(pdf_path, grobid):
    """Ask GROBID for just the header -- far faster than a full parse, and all
    we need to identify which archive entry this file is."""
    try:
        with open(pdf_path, "rb") as fh:
            r = requests.post(f"{grobid}/api/processHeaderDocument",
                              files={"input": (os.path.basename(pdf_path), fh,
                                               "application/pdf")},
                              data={"consolidateHeader": "0"}, timeout=120)
        if not r.ok:
            return ""
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text.encode("utf-8"))
        el = root.find(f".//{TEI}titleStmt/{TEI}title")
        return "".join(el.itertext()).strip() if el is not None else ""
    except Exception:                                   # noqa: BLE001
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grobid", default=os.environ.get("GROBID_URL",
                                                       "http://localhost:8070"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = sorted(glob.glob(str(DROP / "*.pdf")))
    if not files:
        log(f"no PDFs in {DROP.resolve()} -- drop files there and re-run")
        return
    log(f"[drop] {len(files)} PDFs in {DROP}")

    con = store.connect()
    con.executescript(fulltext._SCHEMA)
    by_doi, by_uid, by_title = {}, {}, {}
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        by_uid[uid] = (uid, title)
        by_title[norm(title)] = (uid, title)
        try:
            d = json.loads(meta)
        except Exception:                               # noqa: BLE001
            d = {}
        if d.get("doi"):
            by_doi[str(d["doi"]).lower()] = (uid, title)
    done = {r[0] for r in con.execute(
        "SELECT uid FROM fulltext WHERE status='ok'")}

    try:
        requests.get(f"{args.grobid}/api/isalive", timeout=15)
    except Exception:                                   # noqa: BLE001
        log(f"[drop] GROBID not reachable at {args.grobid}")
        return

    matched = parsed = 0
    for f in files:
        stem = os.path.basename(f)[:-4]
        hit = None
        m = DOI_RE.search(stem.replace("_", "/"))
        if m:
            hit = by_doi.get(m.group(1).lower())
        if not hit:
            hit = by_uid.get(stem.replace("_", ":", 1))
        if not hit:
            t = header_title(f, args.grobid)
            if t:
                hit = by_title.get(norm(t))
                if not hit:
                    for k, v in by_title.items():
                        if len(norm(t)) > 25 and norm(t) in k:
                            hit = v
                            break
        if not hit:
            log(f"  ?  no archive match: {os.path.basename(f)[:64]}")
            continue
        uid, title = hit
        matched += 1
        if uid in done:
            log(f"  =  already parsed: {title[:58]}")
            continue
        if args.dry_run:
            log(f"  ->  would parse: {title[:58]}")
            continue
        try:
            passages = fulltext.tei_passages(fulltext.grobid_tei(f, args.grobid))
        except Exception as e:                          # noqa: BLE001
            log(f"  !  {type(e).__name__}: {title[:50]}")
            continue
        if not passages:
            log(f"  !  no body extracted: {title[:52]}")
            continue
        words = sum(len(t.split()) for _, t in passages)
        (fulltext.OUT / f"{fulltext._safe(uid)}.json").write_text(json.dumps(
            {"uid": uid, "title": title, "n": len(passages),
             "p": [{"s": s, "t": t} for s, t in passages]}), encoding="utf-8")
        con.execute("INSERT OR REPLACE INTO fulltext "
                    "(uid,status,passages,words) VALUES (?,?,?,?)",
                    (uid, "ok", len(passages), words))
        parsed += 1
        log(f"  OK  {len(passages):3d} passages  {title[:54]}")
    con.commit()
    log(f"\n[drop] matched {matched}/{len(files)}, parsed {parsed}")
    if parsed:
        log("[drop] re-run tools/embed.py so the new passages ship with the index")


if __name__ == "__main__":
    main()
