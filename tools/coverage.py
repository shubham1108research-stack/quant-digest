#!/usr/bin/env python3
"""What data does the archive actually hold, and for how many papers.

WHY THIS EXISTS. Every design decision in the graph plan turns on coverage --
whether citations are dense enough for PageRank, whether enough papers carry a
publication year to compute a cohort percentile, whether the labelled set is
large enough to train on. Those numbers have been guessed at repeatedly from a
stale local copy of state.db, and guessed wrong: a 12,420-row copy reported 924
missing abstracts where the live 25,633-row database had 729.

So this reads the live database and reports one table. Read-only, writes
nothing, and safe to run at any time.

It reports THREE things per field, because coverage alone does not tell you
what to do:

    held        how many rows carry it now
    reachable   how many COULD carry it, given the identifiers we hold --
                a DOI-only route cannot fill a title-hash row, and counting
                those in the denominator makes a ceiling look like a failure
    source      what would fill the gap

    python tools/coverage.py
    python tools/coverage.py --json      # machine-readable, for tracking
"""

import argparse
import collections
import glob
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config   # noqa: E402
import store    # noqa: E402


def log(m):
    print(m, flush=True)


def _rows(con):
    for uid, title, source, url, meta in con.execute(
            "SELECT uid,title,source,url,meta FROM items"):
        try:
            m = json.loads(meta)
        except Exception:                                   # noqa: BLE001
            m = {}
        yield uid, title or "", source or "", url or "", m


def _has_text(v, n=120):
    return len((v or "").strip()) >= n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    con = store.connect()
    live, retired = [], 0
    for uid, title, source, url, m in _rows(con):
        if m.get("retired"):
            retired += 1
            continue
        live.append((uid, title, source, url, m))
    n = len(live)

    # ---- identifiers: these bound what every other route can reach ----------
    ns = collections.Counter(u.split(":", 1)[0] for u, _, _, _, _ in live)
    has_doi = sum(1 for _, _, _, _, m in live if (m.get("doi") or "").strip())
    has_arxiv = sum(1 for u, _, _, _, m in live
                    if u.startswith("arxiv:") or (m.get("arxiv_id") or "").strip())
    s2able = sum(1 for u, _, _, _, m in live
                 if (m.get("doi") or "").strip() or u.startswith("arxiv:")
                 or (m.get("arxiv_id") or "").strip())
    has_oa_authors = sum(1 for _, _, _, _, m in live if m.get("oa_author_ids"))

    # ---- full text on disk -------------------------------------------------
    # docs/ft filenames are the uid with the separators flattened --
    # "arxiv:2605.12094" is stored as "arxiv_2605.12094.json" -- so matching
    # raw uids against basenames silently reports zero coverage.
    def _ftkey(u):
        return u.replace(":", "_").replace("/", "_")
    ft = {os.path.basename(f)[:-5] for f in glob.glob("docs/ft/*.json")}

    # ---- the graph ---------------------------------------------------------
    edges = {}
    try:
        from graph import graph_con                          # noqa: PLC0415
        g = graph_con(con)
        for kind, cnt in g.execute(
                "SELECT kind, count(*) FROM g.edges GROUP BY kind"):
            edges[kind] = cnt
    except Exception as e:                                    # noqa: BLE001
        edges["(unreadable)"] = str(e)[:40]
    try:
        edges["cites-table"] = con.execute(
            "SELECT count(*) FROM cites").fetchone()[0]
    except Exception:                                         # noqa: BLE001
        pass
    try:
        edges["embeddings-cached"] = con.execute(
            "SELECT count(*) FROM embeddings").fetchone()[0]
    except Exception:                                         # noqa: BLE001
        pass
    # Reference coverage is what coupling rests on, so count the papers that
    # have NO stored reference separately from those never asked about -- the
    # two need different fixes and look identical in a total.
    try:
        edges["papers with >=1 stored ref"] = con.execute(
            "SELECT count(DISTINCT src) FROM paper_refs").fetchone()[0]
        edges["distinct works cited"] = con.execute(
            "SELECT count(DISTINCT ref) FROM paper_refs").fetchone()[0]
    except Exception:                                         # noqa: BLE001
        pass

    def held(pred):
        return sum(1 for r in live if pred(r[4]))

    # field -> (held, reachable, filled by)
    FIELDS = [
        ("-- TEXT", None, None, ""),
        ("abstract", held(lambda m: _has_text(m.get("abstract"))), n,
         "repec abstracts / s2 / openalex / crossref"),
        ("abstract_source recorded", held(lambda m: m.get("abstract_source")), n,
         "record on every write"),
        ("tldr (S2, generated)", held(lambda m: m.get("tldr")), s2able, "s2.py enrich"),
        ("summary (LLM)", held(lambda m: _has_text(m.get("summary"), 40)), n, "scoring"),
        ("parsed full text",
         sum(1 for u, _, _, _, _ in live if _ftkey(u) in ft or u in ft), n,
         "fetch_pdfs + fulltext (needs pdf_url)"),

        ("-- SCALARS", None, None, ""),
        ("cites", held(lambda m: isinstance(m.get("cites"), int)), s2able, "s2.py enrich"),
        ("influential_cites", held(lambda m: m.get("influential_cites") is not None),
         s2able, "s2.py enrich"),
        ("reference_count", held(lambda m: m.get("reference_count") is not None),
         s2able, "s2.py enrich"),
        ("author_h (MAX)", held(lambda m: m.get("author_h") is not None), s2able,
         "s2.py enrich"),
        ("reputation", held(lambda m: m.get("reputation") is not None), n, "scoring"),
        ("pub_year", held(lambda m: m.get("pub_year")), s2able, "s2.py enrich"),
        ("journal / venue", held(lambda m: m.get("journal")), n, "s2 venue / collector"),
        ("pdf_url", held(lambda m: m.get("pdf_url")), s2able, "s2.py enrich / repec pdfs"),

        ("-- LABELS", None, None, ""),
        ("relevance_category", held(lambda m: m.get("relevance_category")), n, "LLM / propagation"),
        ("  ...of those, WITH an abstract",
         held(lambda m: m.get("relevance_category") and _has_text(m.get("abstract"))),
         n, "usable as a training seed"),
        ("sleeves", held(lambda m: m.get("sleeves")), n, "LLM"),
        ("sleeves_prop (propagated)", held(lambda m: m.get("sleeves_prop")), n,
         "propagate.py"),
        ("desk_fit", held(lambda m: m.get("desk_fit") is not None), n, "LLM"),
        ("tags", held(lambda m: m.get("tags")), n, "tags.py"),
        ("scored_by recorded", held(lambda m: m.get("scored_by")), n, "new; only fresh scores"),

        ("-- GOLD", None, None, ""),
        ("classic", held(lambda m: m.get("classic")), n, "curated"),
        ("canon_type", held(lambda m: m.get("canon_type")), n, "canon.py"),
        ("nber_topics", held(lambda m: m.get("nber_topics")), n, "nber_topics.py"),
        ("watchlist author", held(lambda m: m.get("watchlist_author")), n,
         "watchlist; history NOT yet fetched"),

        ("-- IDENTIFIERS", None, None, ""),
        ("doi", has_doi, n, "-"),
        ("arxiv id", has_arxiv, n, "-"),
        ("S2-resolvable (doi or arxiv)", s2able, n, "bounds every S2 route"),
        ("oa_author_ids", has_oa_authors, n, "openalex; enables co-authorship edges"),
    ]

    if args.json:
        out = {"live": n, "retired": retired, "namespaces": dict(ns),
               "edges": edges,
               "fields": {k: {"held": h, "reachable": r}
                          for k, h, r, _ in FIELDS if h is not None}}
        print(json.dumps(out, indent=1, default=str))
        return 0

    log(f"\nLIVE PAPERS {n:,}   (retired {retired:,})")
    log("uid namespace: " + ", ".join(f"{k}:{v:,}" for k, v in ns.most_common()))
    log("")
    log(f"{'field':<34}{'held':>9}{'of live':>9}{'reachable':>11}{'of reach':>10}  source")
    log("-" * 104)
    for name, h, reach, src in FIELDS:
        if h is None:
            log(f"\n{name}")
            continue
        pl = 100.0 * h / max(n, 1)
        pr = 100.0 * h / max(reach, 1)
        log(f"{name:<34}{h:>9,}{pl:>8.1f}%{reach:>11,}{pr:>9.1f}%  {src}")

    log("\nGRAPH")
    for k, v in edges.items():
        log(f"   {k:<24} {v:,}" if isinstance(v, int) else f"   {k:<24} {v}")
    log("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
