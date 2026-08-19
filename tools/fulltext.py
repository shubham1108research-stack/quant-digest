#!/usr/bin/env python3
"""Full-text pipeline: PDF -> GROBID -> structured sections -> passages.

GROBID turns a PDF into TEI XML with real document structure -- section
headings, paragraphs, tables, references -- rather than the flat text a naive
extractor produces. That structure is what makes a passage citable ("Section
4.2, robustness") instead of an anonymous blob.

Output is one JSON file per paper in docs/ft/, holding its passages:
    {"uid":..., "title":..., "n":12, "p":[{"s":"4.2 Robustness","t":"..."}]}

Why no passage embeddings: ~3.4k papers x ~40 passages would be ~136k vectors,
about 139 MB -- unshippable to a browser. Retrieval is hierarchical instead, and
the split falls out naturally: DENSE embeddings already pick the right papers
(docs/vec.bin), then BM25 over that shortlist picks the right passages inside
them. Lexical scoring is the better tool at passage level anyway -- within one
paper the passages are all topically identical, and what distinguishes them is
the presence of specific terms.

Files are written once and never rewritten, so committing them costs the corpus
size once rather than a fresh blob per run.

  python tools/fulltext.py --limit 200          # parse the 200 highest-value papers
  python tools/fulltext.py --grobid http://host:8070
"""

import argparse
import collections
import json
import os
import pathlib
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import store            # noqa: E402
import fetch_pdfs       # noqa: E402  (reuse its resolver + polite downloader)

OUT = pathlib.Path("docs/ft")
TEI = "{http://www.tei-c.org/ns/1.0}"
GROBID = os.environ.get("GROBID_URL", "http://localhost:8070")
WORDS_PER_PASSAGE = 220          # ~300 tokens: big enough to carry an argument
MIN_PASSAGE_WORDS = 25           # below this it is a heading fragment, not content

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fulltext (
    uid      TEXT PRIMARY KEY,
    status   TEXT,            -- ok | no_pdf | grobid_error | no_body
    passages INTEGER,
    words    INTEGER,
    ts       TEXT DEFAULT (datetime('now'))
);
"""


def log(m):
    print(m, flush=True)


def _safe(uid):
    return re.sub(r"[^A-Za-z0-9._-]", "_", uid)[:120]


def grobid_tei(pdf_path, url):
    """PDF -> TEI XML, parsed entirely locally inside the container."""
    with open(pdf_path, "rb") as fh:
        r = requests.post(
            f"{url}/api/processFulltextDocument",
            files={"input": (os.path.basename(pdf_path), fh, "application/pdf")},
            # consolidation is OFF: it makes GROBID call Crossref from inside
            # the container for metadata we already hold, turning a local parse
            # into a network dependency that fails the whole document.
            data={"consolidateHeader": "0", "consolidateCitations": "0",
                  "segmentSentences": "0"},
            timeout=300,
        )
    if not r.ok:
        raise RuntimeError(f"grobid HTTP {r.status_code}: {r.text[:200]}")
    return r.text


def _clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def tei_passages(tei_xml):
    """Walk the TEI body and emit (section, text) passages.

    Paragraphs are accumulated within a section until the word budget is hit,
    so a passage never straddles a section boundary -- that keeps the section
    label honest when the passage is later cited.
    """
    root = ET.fromstring(tei_xml.encode("utf-8"))
    body = root.find(f"{TEI}text/{TEI}body")
    if body is None:
        return []
    out = []
    for div in body.iter(f"{TEI}div"):
        head = _clean("".join(div.find(f"{TEI}head").itertext())) \
            if div.find(f"{TEI}head") is not None else ""
        buf, n = [], 0
        for p in div.findall(f"{TEI}p"):
            txt = _clean("".join(p.itertext()))
            if not txt:
                continue
            buf.append(txt)
            n += len(txt.split())
            if n >= WORDS_PER_PASSAGE:
                out.append((head, " ".join(buf)))
                buf, n = [], 0
        if buf and n >= MIN_PASSAGE_WORDS:
            out.append((head, " ".join(buf)))
    return out


def pick_papers(con, limit):
    """Best papers we can ACTUALLY GET, not simply the best papers.

    Ranking on archive score alone selects for published journal articles --
    which is exactly the paywalled subset -- and the first trial duly spent 46
    of 60 attempts on papers with no free PDF. So an availability prior is part
    of the ranking: arXiv and NBER are near-certain, a bare DOI is a coin flip.
    Papers already parsed are skipped."""
    done = {r[0] for r in con.execute(
        "SELECT uid FROM fulltext WHERE status='ok'")}
    rows = con.execute(
        "SELECT uid, title, url, meta, first_seen FROM items").fetchall()
    scored = []
    for uid, title, url, meta, seen in rows:
        if uid in done:
            continue
        try:
            m = json.loads(meta)
        except Exception:                       # noqa: BLE001
            m = {}
        if str(m.get("section")) == "4":        # practitioner posts: no PDF worth parsing
            continue
        rank = (m.get("rank_score") or 0) / 100.0
        nov = m.get("novelty_posterior") or 0
        rep = m.get("reputation") or 1.0
        watch = 0.25 if m.get("watchlist") else 0.0
        u = url or ""
        if uid.startswith("arxiv:") or "arxiv.org" in u or "RePEc:arx:" in u:
            avail = 1.0
        elif "nber.org/papers" in u:
            avail = 0.9
        elif uid.startswith("doi:"):
            avail = 0.35
        else:
            avail = 0.2
        quality = (rank + nov) * rep + watch
        # availability is weighted heavily: a paper we cannot fetch is not a
        # cheaper parse, it is a wasted one
        scored.append((quality + 1.5 * avail, seen or "", uid, title, url))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [(u, t, url) for _, _, u, t, url in scored[:limit]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--grobid", default=GROBID)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    con = store.connect()
    con.executescript(_SCHEMA)
    con.executescript(fetch_pdfs._SCHEMA)      # the pdfs table, for the fetcher

    try:
        r = requests.get(f"{args.grobid}/api/isalive", timeout=20)
        log(f"[ft] GROBID alive: {r.text.strip()[:20]}")
    except Exception as e:                      # noqa: BLE001
        log(f"[ft] GROBID not reachable at {args.grobid}: {e}")
        return

    todo = pick_papers(con, args.limit)
    log(f"[ft] {len(todo)} papers queued (highest-value first)")

    tally = collections.Counter()
    for i, (uid, title, url) in enumerate(todo, 1):
        try:
            pdf_url = fetch_pdfs.resolve(uid, url, title)
            if not pdf_url:
                tally["no_pdf"] += 1
                con.execute("INSERT OR REPLACE INTO fulltext (uid,status,passages,words)"
                            " VALUES (?,?,?,?)", (uid, "no_pdf", 0, 0))
                continue
            status, path, _ = fetch_pdfs.download(uid, pdf_url)
            if status != "ok" or not path:
                tally["no_pdf"] += 1
                con.execute("INSERT OR REPLACE INTO fulltext (uid,status,passages,words)"
                            " VALUES (?,?,?,?)", (uid, "no_pdf", 0, 0))
                continue
            tei = grobid_tei(path, args.grobid)
            passages = tei_passages(tei)
            if not passages:
                tally["no_body"] += 1
                con.execute("INSERT OR REPLACE INTO fulltext (uid,status,passages,words)"
                            " VALUES (?,?,?,?)", (uid, "no_body", 0, 0))
                continue
            words = sum(len(t.split()) for _, t in passages)
            (OUT / f"{_safe(uid)}.json").write_text(json.dumps(
                {"uid": uid, "title": title, "n": len(passages),
                 "p": [{"s": s, "t": t} for s, t in passages]}), encoding="utf-8")
            con.execute("INSERT OR REPLACE INTO fulltext (uid,status,passages,words)"
                        " VALUES (?,?,?,?)", (uid, "ok", len(passages), words))
            tally["ok"] += 1
        except Exception as e:                  # noqa: BLE001
            tally["grobid_error"] += 1
            if tally["grobid_error"] <= 5:      # first few, so a failure is diagnosable
                log(f"[ft] {uid} failed: {type(e).__name__}: {str(e)[:300]}")
            con.execute("INSERT OR REPLACE INTO fulltext (uid,status,passages,words)"
                        " VALUES (?,?,?,?)", (uid, "grobid_error", 0, 0))
        if i % 10 == 0:
            con.commit()
            log(f"[ft] {i}/{len(todo)} · parsed {tally['ok']}")
    con.commit()

    total = con.execute("SELECT count(*), sum(passages), sum(words) "
                        "FROM fulltext WHERE status='ok'").fetchone()
    log("\n=== outcome ===")
    for k, v in tally.most_common():
        log(f"  {v:5d}  {k}")
    log(f"\n  corpus now: {total[0] or 0} papers, {total[1] or 0} passages, "
        f"{(total[2] or 0)/1000:.0f}k words")
    log(f"  written to {OUT.resolve()}")


if __name__ == "__main__":
    main()
