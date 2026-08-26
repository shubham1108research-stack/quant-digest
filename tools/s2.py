#!/usr/bin/env python3
"""Semantic Scholar as a bulk node-feature source.

WHY THIS EXISTS, AND WHY IT SUPERSEDES SEVERAL SEPARATE ROUTES
The graph plan needs per-paper scalars that barely exist in the archive:
citations (8.3% populated), author h-index (18.2%), pub_year (35.9%), a free
PDF (1 row). Each had its own route -- CitEc for citations, OpenAlex for
authors, RePEc for PDFs -- and CitEc's is capped at 450 calls a day, which is
sixteen runs for one field.

S2's POST /paper/batch returns ALL of them for up to 500 papers in ONE request.
Measured on three papers in a single call:

    Fama-French 1993   cites 25,511  influential 3,231  refs 32  maxAuthorH 85
    NBER w33004        cites      2  influential     0  refs 75  maxAuthorH 160
    Jegadeesh-Titman   cites 11,251  influential 1,019  refs 34  maxAuthorH 90

25,633 papers is ~52 requests.

THE RATE LIMIT IS THE WHOLE GAME, AND IT IS ENDPOINT-SPECIFIC. Measured
unauthenticated, same machine, same minute:

    GET  /paper/search          429  -- throttled globally across anonymous callers
    GET  /paper/search/match    200, then 429 on repeat even at 3s spacing
    POST /paper/batch           200, no throttling observed

A probe built on /paper/search reports "no abstract" for every row and looks
exactly like S2 not having the data. It has the data. Prefer the batch
endpoint for anything at scale; S2_API_KEY raises the limits if set.

WHAT IT COLLECTS
  abstract            where the publisher deposited one with S2
  tldr                S2's one-line summary -- NOT stored as an abstract, see below
  citationCount       the citation scalar
  influentialCitationCount
                      S2's count of citations that substantively build on the
                      paper. A better authority signal than raw count, and it
                      is closer to what "referenced by important work" means.
  referenceCount      out-degree; the reference list itself is a separate call
  authors[].hIndex    reduced to MAX, not mean -- one eminent co-author is the
                      signal, and averaging it against three students destroys it
  openAccessPdf       a free PDF url, which feeds tools/fulltext.py
  year, venue, externalIds

TLDR IS NOT WRITTEN AS AN ABSTRACT. It is model-generated, and storing it in
the abstract field would put synthetic text into the embedding input with no
way to tell it from a real abstract later -- the same indistinguishability
problem that made scored_by necessary. It goes in its own field.

    python tools/s2.py enrich --dry-run
    python tools/s2.py enrich --limit 2000
    python tools/s2.py specter --dry-run      # 768-dim SPECTER2 vectors
"""

import argparse
import collections
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config   # noqa: E402
import store    # noqa: E402

BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
BATCH_MAX = 500                      # S2's documented cap per request
PAUSE = 1.5                          # between batches, unauthenticated
FIELDS = ("title,abstract,tldr,citationCount,influentialCitationCount,"
          "referenceCount,openAccessPdf,externalIds,publicationVenue,year,"
          # authorId is the disambiguation anchor. Resolving a watched author
          # by NAME is unreliable -- /author/search for "Bryan Kelly" returns a
          # clinician with h-index 4 publishing on post-COVID clinics, not the
          # Yale asset-pricing one. An authorId taken from a paper we KNOW is
          # theirs cannot be the wrong person.
          "authors.authorId,authors.hIndex,authors.name")
UA = "quant-digest/1.0 (personal research portal; mailto:%s)"


def log(m):
    print(m, flush=True)


def _headers():
    h = {"User-Agent": UA % (os.environ.get("CONTACT_EMAIL") or "research"),
         "Content-Type": "application/json"}
    key = os.environ.get("S2_API_KEY")
    if key:
        h["x-api-key"] = key
    return h


def _post(ids, fields, tries=4):
    body = json.dumps({"ids": ids}).encode("utf-8")
    req = urllib.request.Request(BATCH_URL + "?fields=" + fields,
                                 data=body, headers=_headers())
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                wait = 5 * (attempt + 1)
                log(f"[s2]   HTTP {e.code}, waiting {wait}s")
                time.sleep(wait)
                continue
            log(f"[s2]   HTTP {e.code}: {e.read()[:160]!r}")
            return None
        except Exception as e:                                # noqa: BLE001
            log(f"[s2]   {type(e).__name__}: {str(e)[:100]}")
            time.sleep(4)
    return None


def _s2_id(uid, meta):
    """The identifier S2 will accept, or None.

    DOI first because it is unambiguous. arXiv second. A title-hash uid has
    neither and cannot be batched -- those need the match endpoint, which is
    the throttled path, so they are counted and skipped rather than pretended
    about.
    """
    doi = (meta.get("doi") or "").strip()
    if doi:
        return "DOI:" + doi
    if uid.startswith("arxiv:"):
        return "ARXIV:" + uid.split(":", 1)[1].split("v")[0]
    aid = (meta.get("arxiv_id") or "").strip()
    if aid:
        return "ARXIV:" + aid.split("v")[0]
    return None


def _rows(con):
    for uid, title, meta in con.execute("SELECT uid,title,meta FROM items"):
        try:
            m = json.loads(meta)
        except Exception:                                     # noqa: BLE001
            continue
        if m.get("retired"):
            continue
        yield uid, title, m


def enrich(args):
    con = store.connect()
    todo, no_id = [], 0
    for uid, title, m in _rows(con):
        sid = _s2_id(uid, m)
        if not sid:
            no_id += 1
            continue
        # Skip rows that already have everything this would write, unless
        # forced. Citations go stale, so `--force` is the refresh path.
        #
        # s2_author_ids is in this list deliberately. It was added AFTER the
        # first full run, so without it every already-enriched row would be
        # skipped forever and the field would stay empty on exactly the papers
        # most likely to resolve a watched author. A new field has to widen the
        # queue, not inherit the previous one's exclusions.
        if not args.force and m.get("cites") is not None and \
                m.get("author_h") is not None and \
                m.get("s2_author_ids") and \
                len((m.get("abstract") or "").strip()) >= 120:
            continue
        todo.append((uid, sid, m))
    log(f"[s2] {len(todo):,} papers to enrich; {no_id:,} have no DOI or arXiv id "
        f"(title-hash rows -- the batch endpoint cannot take them)")
    if args.limit:
        todo = todo[:args.limit]
        log(f"[s2] limited to {len(todo):,}")
    if not todo:
        return 0

    got = collections.Counter()
    wrote = 0
    invalidate = []
    for i in range(0, len(todo), BATCH_MAX):
        chunk = todo[i:i + BATCH_MAX]
        res = _post([c[1] for c in chunk], FIELDS)
        if res is None:
            log(f"[s2] batch {i // BATCH_MAX + 1} failed; skipping")
            continue
        for (uid, sid, m), p in zip(chunk, res):
            if not p:
                got["not in s2"] += 1
                continue
            patch = {}
            abstract = (p.get("abstract") or "").strip()
            if len(abstract) >= 120 and len((m.get("abstract") or "").strip()) < 120:
                patch["abstract"] = abstract[:6000]
                patch["abstract_source"] = "s2"
                invalidate.append(uid)
                got["abstract"] += 1
            # tldr in its OWN field: it is generated text, and putting it in
            # `abstract` would make synthetic and real indistinguishable.
            tl = ((p.get("tldr") or {}) or {}).get("text")
            if tl and not m.get("tldr"):
                patch["tldr"] = tl[:1000]
                got["tldr"] += 1
            if p.get("citationCount") is not None:
                patch["cites"] = int(p["citationCount"])
                patch["cites_source"] = "s2"
                got["cites"] += 1
            if p.get("influentialCitationCount") is not None:
                patch["influential_cites"] = int(p["influentialCitationCount"])
                got["influential"] += 1
            if p.get("referenceCount") is not None:
                patch["reference_count"] = int(p["referenceCount"])
            aids = [a.get("authorId") for a in (p.get("authors") or [])
                    if a.get("authorId")]
            if aids and not m.get("s2_author_ids"):
                patch["s2_author_ids"] = aids
                got["author_ids"] += 1
            hs = [a.get("hIndex") for a in (p.get("authors") or [])
                  if a.get("hIndex") is not None]
            if hs:
                # MAX, deliberately. See the module docstring.
                patch["author_h"] = max(hs)
                patch["author_h_source"] = "s2"
                got["author_h"] += 1
            pdf = (p.get("openAccessPdf") or {}).get("url")
            if pdf and not m.get("pdf_url"):
                patch["pdf_url"] = pdf
                got["pdf_url"] += 1
            if p.get("year") and not m.get("pub_year"):
                patch["pub_year"] = int(p["year"])
                got["pub_year"] += 1
            ven = (p.get("publicationVenue") or {}).get("name")
            if ven and not m.get("journal"):
                patch["journal"] = ven
                got["venue"] += 1
            if not patch:
                got["nothing new"] += 1
                continue
            if args.dry_run:
                wrote += 1
                continue
            if store.update_meta(con, uid, patch):
                wrote += 1
        log(f"[s2]   {min(i + BATCH_MAX, len(todo)):,}/{len(todo):,}")
        time.sleep(PAUSE)

    if not args.dry_run:
        if invalidate:
            # Same reason repec.py does this: the embedding cache is keyed on a
            # hash of the embedded TEXT, so a row that gains an abstract would
            # otherwise keep the vector built from its title alone.
            con.executemany("DELETE FROM embeddings WHERE uid=?",
                            [(u,) for u in invalidate])
            log(f"[s2] invalidated {len(invalidate):,} cached vectors")
        con.commit()
    log(f"\n[s2] {'would update' if args.dry_run else 'updated'} {wrote:,} papers")
    for k, v in got.most_common():
        log(f"[s2]    {k:<14} {v:,}")
    return 0


def specter(args):
    """SPECTER2 document vectors -- 768-dim, free, computed by S2.

    Not wired into the index here. This reports coverage so the bake-off can
    decide whether a purpose-built scientific-document embedding beats
    text-embedding-3-small and bge-small on eval/golden.json -- the same
    question tools/embed_local.py exists to answer, with a third candidate
    that costs nothing and needs no GPU.
    """
    con = store.connect()
    ids = []
    for uid, title, m in _rows(con):
        sid = _s2_id(uid, m)
        if sid:
            ids.append((uid, sid))
    n = min(args.limit or BATCH_MAX, len(ids), BATCH_MAX)
    log(f"[s2] {len(ids):,} papers have an S2-resolvable id; sampling {n}")
    res = _post([s for _, s in ids[:n]], "title,embedding.specter_v2")
    if not res:
        return 1
    have = [p for p in res if p and (p.get("embedding") or {}).get("vector")]
    dims = {len((p["embedding"]["vector"])) for p in have}
    log(f"[s2] {len(have)}/{n} returned a SPECTER2 vector, dim(s) {dims or '-'}")
    log("[s2] not stored -- run the embedding bake-off to decide if it wins")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    e = sub.add_parser("enrich")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--limit", type=int, default=0)
    e.add_argument("--force", action="store_true",
                   help="refresh rows that already have citations")
    s = sub.add_parser("specter")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    return enrich(args) if args.action == "enrich" else specter(args)


if __name__ == "__main__":
    sys.exit(main())
