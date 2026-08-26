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
import concurrent.futures
import json
import os
import pathlib
import queue
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import store
from progress import Progress            # noqa: E402
import fetch_pdfs       # noqa: E402  (reuse its resolver + polite downloader)

OUT = pathlib.Path("docs/ft")
TEI = "{http://www.tei-c.org/ns/1.0}"
GROBID = os.environ.get("GROBID_URL", "http://localhost:8070")
WORDS_PER_PASSAGE = 220          # ~300 tokens: big enough to carry an argument
MIN_PASSAGE_WORDS = 25           # below this it is a heading fragment, not content

# Downloads are latency-bound and already paced per host, so several run at once
# without hitting any one host harder -- the per-host delay still applies, it
# just no longer idles the whole run. Parsers are CPU inside the GROBID
# container on a 4-core runner, so four is the ceiling: more would queue inside
# GROBID rather than go faster.
DL_WORKERS = 6
PARSE_WORKERS = 4

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
    # The skip list is derived from the PASSAGE FILES on disk, not only from the
    # fulltext table. Those files are the real output; the table is bookkeeping.
    # Deriving it this way lets the workflow commit docs/ft alone and leave
    # state.db untouched -- state.db is binary, and a concurrent writer turns
    # every commit into an unmergeable rebase conflict that discards the run.
    # A 1,946-paper parse was lost to exactly that.
    # Files are named _safe(uid), so the comparison has to be made in that
    # space -- every uid contains a colon and every filename has had it
    # replaced, so comparing raw uids against stems matched NOTHING and the
    # skip list silently protected nothing. A 600-paper run re-parsed 413
    # papers it already held and added none.
    on_disk = {f.stem for f in OUT.glob("*.json") if f.stem != "index"}

    # RECONCILE THE TABLE AGAINST THE FILES BEFORE TRUSTING EITHER.
    #
    # The table says 'ok' and the passages are the actual output, so a row
    # claiming 'ok' with no file on disk is a lie that skips the paper forever.
    # It happens whenever the two stores diverge, and they did: a run parsed 314
    # papers, wrote their status into state.db, pushed state.db, and pushed a
    # docs/ft that had been built from empty -- so restoring the corpus from
    # ft.tar.gz.prev brought back an archive predating those 314 while their
    # 'ok' rows survived in the database.
    #
    # Left alone, every future run would skip them as done and they would never
    # be parsed again. Clearing the claim costs one re-parse; keeping it costs
    # the paper.
    stale = [r[0] for r in con.execute(
        "SELECT uid FROM fulltext WHERE status='ok'")
        if _safe(r[0]) not in on_disk]
    if stale:
        con.executemany("UPDATE fulltext SET status='missing' WHERE uid=?",
                        [(u,) for u in stale])
        con.commit()
        log(f"[ft] {len(stale):,} rows claimed 'ok' with no passage file on "
            f"disk -- reset for re-parsing")

    done = set(on_disk)
    done |= {_safe(r[0]) for r in con.execute(
        "SELECT uid FROM fulltext WHERE status IN ('ok','no_pdf')")}
    rows = con.execute(
        "SELECT uid, title, url, meta, first_seen FROM items").fetchall()
    scored = []
    for uid, title, url, meta, seen in rows:
        if _safe(uid) in done:
            continue
        try:
            m = json.loads(meta)
        except Exception:                       # noqa: BLE001
            m = {}
        if m.get("retired"):
            continue
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
        # Curated papers are valuable BY CONSTRUCTION, not by score. The
        # classics and NBER working papers were ingested unscored, so a pure
        # quality ranking gave them zero and a run of 400 picked none of them
        # -- it preferred already-scored arXiv papers. Their standing is the
        # reason they were curated; it does not need an LLM to confirm it.
        # classics rank above NBER: fewer of them, and they are the reference
        # layer everything else gets compared against. NBER is close behind and
        # far more numerous, so without the gap it would fill every batch.
        if m.get("classic"):
            curated = 4.5
        elif m.get("source") == "NBER":
            curated = 3.0
        else:
            curated = 0.0
        quality = (rank + nov) * rep + watch + curated
        # availability is weighted heavily: a paper we cannot fetch is not a
        # cheaper parse, it is a wasted one
        scored.append((quality + 1.5 * avail, seen or "", uid, title, url,
                       m.get("doi", "")))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [(u, t, url, d) for _, _, u, t, url, d in scored[:limit]]


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

    # ---------------------------------------------------------------- pipeline
    # This loop used to do three unrelated things per paper, serially:
    #
    #     resolve  ~2s   the 9-rung ladder, PER PAPER
    #     download ~2s   bytes plus a per-host delay (arxiv.org is 3.0s)
    #     GROBID   ~2s   CPU inside the container
    #
    # Measured: 400 queued, 314 parsed, 32 minutes -- 6.1s each, which is 30
    # hours for 18,000 papers and five job timeouts. The three costs bind on
    # three DIFFERENT resources, so each sat idle while the other two worked.
    #
    # Resolve is gone entirely: it is a batched pass now (fetch_pdfs
    # resolve_staged) and its answers are in the `pdfs` table. Asking the
    # per-paper ladder again re-asks questions already answered.
    #
    # Download and parse now overlap. Downloads are latency-bound with per-host
    # pacing, so several hosts proceed at once; GROBID serves concurrent
    # requests. A queue joins them, so a slow download does not stall the
    # parser and a slow parse does not stall the fetchers.
    known = {}
    try:
        for uid_, url_, st_ in con.execute(
                "SELECT uid, url, status FROM pdfs WHERE url IS NOT NULL"):
            if url_ and st_ in ("ok", "resolved"):
                known[uid_] = url_
    except Exception:                           # noqa: BLE001
        pass
    log(f"[ft] {len(known):,} urls already resolved in the pdfs table")

    tally = collections.Counter()
    prog = Progress(len(todo), "ft", every_s=30)
    q_parse: queue.Queue = queue.Queue(maxsize=DL_WORKERS * 4)
    results: list = []
    res_lock = threading.Lock()

    def record(uid, status, passages=0, words=0):
        with res_lock:
            results.append((uid, status, passages, words))
            tally[status] += 1

    def fetch_worker(item):
        uid, title, url, doi = item
        try:
            # The url comes from the table. Falling back to resolve() would
            # quietly reintroduce the per-paper ladder for every row the
            # batched pass could not answer, which is exactly the cost this
            # change exists to remove -- so a missing url is a recorded miss.
            pdf_url = known.get(uid)
            if not pdf_url:
                record(uid, "no_pdf")
                return
            status, path, _ = fetch_pdfs.download(uid, pdf_url)
            if status != "ok" or not path:
                record(uid, "no_pdf")
                return
            q_parse.put((uid, title, pdf_url, path))
        except Exception as e:                  # noqa: BLE001
            if tally["grobid_error"] <= 5:
                log(f"[ft] {uid} download failed: {type(e).__name__}: {str(e)[:200]}")
            record(uid, "grobid_error")

    def parse_worker():
        while True:
            item = q_parse.get()
            try:
                if item is None:
                    return
                uid, title, pdf_url, path = item
                try:
                    passages = tei_passages(grobid_tei(path, args.grobid))
                    if not passages:
                        record(uid, "no_body")
                        continue
                    words = sum(len(t.split()) for _, t in passages)
                    # The resolved PDF url is recorded HERE, in the passage
                    # file, not only in the pdfs table. That table lives in
                    # state.db, which this workflow deliberately does not
                    # commit -- so a url a resolver worked to find was being
                    # thrown away at the end of every run. Anything with
                    # passages provably had a reachable PDF.
                    (OUT / f"{_safe(uid)}.json").write_text(json.dumps(
                        {"uid": uid, "title": title, "n": len(passages),
                         "pdf": pdf_url,
                         "p": [{"s": sec, "t": txt} for sec, txt in passages]}),
                        encoding="utf-8")
                    record(uid, "ok", len(passages), words)
                except Exception as e:          # noqa: BLE001
                    if tally["grobid_error"] <= 5:
                        log(f"[ft] {uid} failed: {type(e).__name__}: {str(e)[:300]}")
                    record(uid, "grobid_error")
                finally:
                    prog.tick()
            finally:
                q_parse.task_done()

    parsers = [threading.Thread(target=parse_worker, daemon=True)
               for _ in range(PARSE_WORKERS)]
    for t in parsers:
        t.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
        list(ex.map(fetch_worker, todo))
    q_parse.join()
    for _ in parsers:
        q_parse.put(None)
    prog.done()

    # Written from the MAIN thread. store.connect() uses sqlite3's default
    # check_same_thread=True, so a worker touching `con` raises and takes the
    # pool down -- a mistake already made once in fetch_pdfs.
    con.executemany(
        "INSERT OR REPLACE INTO fulltext (uid,status,passages,words) "
        "VALUES (?,?,?,?)", results)
    con.commit()

    # manifest: the portal needs to know which papers have full text BEFORE
    # deciding what to fetch, and probing 8k urls to find out is not an option
    # Rebuilt from the FILES rather than the fulltext table, for the same
    # reason the skip list is: the files are what gets committed, so a run whose
    # state.db is discarded still leaves a manifest that matches what shipped.
    have, pdfs = [], {}
    for f in sorted(OUT.glob("*.json")):
        if f.stem == "index":
            continue
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                       # noqa: BLE001
            continue
        have.append(j.get("uid") or f.stem)
        if j.get("pdf"):
            pdfs[j.get("uid") or f.stem] = j["pdf"]
    (OUT / "index.json").write_text(
        json.dumps({"n": len(have), "uids": have, "pdfs": pdfs}), encoding="utf-8")
    log(f"[ft] manifest: {len(have)} papers with full text")

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
