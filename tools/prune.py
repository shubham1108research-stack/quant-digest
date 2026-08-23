#!/usr/bin/env python3
"""Retire the papers that are in the archive but should never have been.

38% of the archive is dead weight -- 4,265 papers the LLM itself marked
`off_topic`, plus junk titles, records with no text and metadata dated years in
the future. 3,023 of the off-topic ones came in through SSRN, whose Crossref
query used sort=created: that makes Crossref ignore query relevance completely
and return the newest SSRN papers of ANY discipline, so the finance queries were
decoration. That is fixed at the source; this deals with what already arrived.

FLAGGED, NOT DELETED. state.db is the cross-run dedup memory: `filter_new` skips
a uid it has seen, so deleting a row invites the same paper straight back on the
next collection. Retired papers keep their row and gain

    retired: "<why>"        and    retired_on: "<date>"

and every consumer skips them -- the portal export, the embedding index, the
graph, the map, the full-text queue. They stop costing an embedding, a graph
node, a spot on the map and a slot in Ask's retrieval, without losing the
memory that we already looked at them.

Reversible: `--restore` clears the flag on everything, or on one reason.

  python tools/prune.py --dry-run
  python tools/prune.py
  python tools/prune.py --restore off_topic
"""

import argparse
import collections
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import scoring   # noqa: E402
import store     # noqa: E402


def log(m):
    print(m, flush=True)


def reason_for(title, d, horizon):
    """Why this paper should not be in the working archive, or ''."""
    if scoring.is_junk(title or ""):
        return "junk_title"
    date = str(d.get("date") or "")
    if len(date) >= 4 and date[:4].isdigit() and date > horizon:
        return "future_date"
    # off_topic is trusted ONLY where the collection itself was broken or
    # indiscriminate. Sampling the flagged set found the classifier calling
    # real finance off_topic -- "A Theory of Housing Demand Shocks" (NBER),
    # "Social Finance: Cultural Evolution, Transmission Bias and Market
    # Dynamics" (NBER), "New Frontiers in Household Finance" (Review of
    # Financial Studies) -- and all three also carry rank_score 0 and
    # desk_fit 0, so no threshold on the scores rescues them either.
    #
    # What IS reliable is the provenance. SSRN's Crossref query ran with
    # sort=created, which makes Crossref ignore query relevance entirely and
    # return the newest SSRN papers of any discipline; the topic sweeps search
    # free text across a whole OpenAlex field. Those two mechanisms produced
    # the quail cages and the activated sludge. A deliberately chosen feed --
    # NBER, a named journal, arXiv, NEP -- gets the benefit of the doubt, and
    # a classifier mistake there is not grounds for deleting the paper.
    src = str(d.get("source") or "")
    indiscriminate = src.startswith(("SSRN", "topic:", "topic-sweep"))
    if indiscriminate and d.get("relevance_category") == "off_topic":
        return "off_topic_indiscriminate_source"
    if not ((d.get("abstract") or "").strip() or (d.get("summary") or "").strip()):
        return "no_text"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", nargs="?", const="*", default=None,
                    help="clear the flag (optionally for one reason only)")
    args = ap.parse_args()
    con = store.connect()

    if args.restore:
        n = 0
        for uid, meta in con.execute("SELECT uid, meta FROM items").fetchall():
            d = json.loads(meta)
            r = d.get("retired")
            if not r or (args.restore != "*" and r != args.restore):
                continue
            d.pop("retired", None)
            d.pop("retired_on", None)
            con.execute("UPDATE items SET meta=? WHERE uid=?",
                        (json.dumps(d, default=str), uid))
            n += 1
        con.commit()
        log(f"[prune] restored {n} papers")
        return

    horizon = (datetime.date.today() + datetime.timedelta(days=120)).isoformat()
    today = datetime.date.today().isoformat()
    hits, by_reason, by_source = [], collections.Counter(), collections.Counter()
    kept = 0
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            d = json.loads(meta)
        except Exception:                                # noqa: BLE001
            continue
        if d.get("retired"):
            continue
        why = reason_for(title, d, horizon)
        if not why:
            kept += 1
            continue
        # never retire something curated by hand, whatever the classifier says
        if d.get("classic") or d.get("watchlist") or d.get("added_on_request"):
            kept += 1
            continue
        hits.append((uid, d, why))
        by_reason[why] += 1
        by_source[str(d.get("source", ""))[:26]] += 1

    total = kept + len(hits)
    log(f"[prune] {total:,} papers; retiring {len(hits):,} ({100*len(hits)/max(1,total):.0f}%), "
        f"keeping {kept:,}")
    for r, n in by_reason.most_common():
        log(f"    {r:<14} {n:>6}")
    log("  by source:")
    for srcname, n in by_source.most_common(8):
        log(f"    {srcname:<28} {n:>6}")

    if args.dry_run:
        log("[prune] dry run -- nothing written")
        return

    for uid, d, why in hits:
        d["retired"] = why
        d["retired_on"] = today
        con.execute("UPDATE items SET meta=? WHERE uid=?",
                    (json.dumps(d, default=str), uid))
        # their vectors are dead weight in an index the browser downloads
        con.execute("DELETE FROM embeddings WHERE uid=?", (uid,))
    con.commit()
    log(f"[prune] retired {len(hits):,}; their vectors dropped "
        f"-- rerun tools/embed.py and tools/graph.py")


if __name__ == "__main__":
    main()
