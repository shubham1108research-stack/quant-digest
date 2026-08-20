#!/usr/bin/env python3
"""Score the archive's backlog and add sleeve labels to everything.

Two gaps, one pass:

  1. 3,517 papers are UNSCORED and stranded. main.py only ranks `fresh` items
     -- the output of store.filter_new -- so anything skipped when a run's LLM
     budget expired is never revisited. The log line "next run picks them up"
     was simply false.
  2. Every paper predating the sleeve fields lacks sleeves/desk_fit, including
     the 309 classics and 2,156 NBER papers ingested this week.

Both are fixed by re-running the same rubric, so they are one job. Results are
written with store.update_meta -- store.save is INSERT OR IGNORE and would
silently do nothing.

Papers that gain a summary or a changed topic have their cached embedding
dropped, because tools/embed.py builds its text from title + topic + abstract
and the cache is keyed only by (uid, model, dim) -- it cannot see the text
change underneath it.

  python tools/rescore.py --limit 500      # a batch
  python tools/rescore.py                  # everything outstanding
"""

import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import llm     # noqa: E402
import scoring  # noqa: E402
import store   # noqa: E402

SCORE_FIELDS = ("relevance", "relevance_category", "relevance_posterior",
                "generality", "contribution", "testability", "novelty_type",
                "novelty_posterior", "antecedent_match", "rank_score",
                "isolated_backtest_only", "no_costs_mentioned",
                "extreme_claimed_sharpe", "weak_stat_support",
                "topic", "summary", "sleeves", "desk_fit")


def log(m):
    print(m, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-unscored", action="store_true",
                    help="skip papers that only need sleeve labels")
    args = ap.parse_args()

    con = store.connect()
    todo, need_score, need_sleeve = [], 0, 0
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            d = json.loads(meta)
        except Exception:                                   # noqa: BLE001
            continue
        if scoring.is_junk(title):
            continue
        unscored = d.get("rank_score") is None
        no_sleeve = not d.get("sleeves")
        if not (unscored or no_sleeve):
            continue
        if args.only_unscored and not unscored:
            continue
        # the rubric reads title + abstract; without either there is nothing
        # to judge and a call would only invent one
        text = (d.get("abstract") or "").strip() or (d.get("summary") or "").strip()
        if not text and not (title or "").strip():
            continue
        need_score += unscored
        need_sleeve += no_sleeve and not unscored
        item = dict(d)
        item["uid"] = uid
        item["title"] = title or d.get("title", "")
        item["_had_summary"] = bool((d.get("summary") or "").strip())
        item["_old_topic"] = d.get("topic", "")
        todo.append(item)

    log(f"[rescore] {len(todo)} papers outstanding "
        f"({need_score} unscored, {need_sleeve} scored but no sleeve)")
    # unscored first: they are missing everything, not just a label
    todo.sort(key=lambda it: it.get("rank_score") is not None)
    if args.limit:
        todo = todo[:args.limit]
        log(f"[rescore] limited to {len(todo)}")
    if not todo:
        return
    if not llm.have_key():
        raise SystemExit("no LLM provider key set")

    llm.start_run_budget(0)          # dedicated job: no shared wall-clock cap
    llm.rank(todo, log)

    written = invalidated = 0
    sleeves = collections.Counter()
    fits = collections.Counter()
    for it in todo:
        if not it.get("sleeves") and it.get("rank_score") is None:
            continue                                   # provider skipped it
        patch = {k: it[k] for k in SCORE_FIELDS if k in it}
        if not patch:
            continue
        if store.update_meta(con, it["uid"], patch):
            written += 1
            for s in (it.get("sleeves") or []):
                sleeves[s] += 1
            fits[it.get("desk_fit", 0)] += 1
        # embedding text is title + topic + abstract; a new summary or a changed
        # topic makes the cached vector stale
        if (not it["_had_summary"] and it.get("summary")) or \
                (it.get("topic") and it["topic"] != it["_old_topic"]):
            try:
                con.execute("DELETE FROM embeddings WHERE uid=?", (it["uid"],))
                invalidated += 1
            except Exception:                          # noqa: BLE001
                pass
    con.commit()

    log(f"\n[rescore] wrote {written}/{len(todo)}; "
        f"invalidated {invalidated} stale vectors")
    log("[rescore] sleeve distribution:")
    for k, v in sleeves.most_common():
        log(f"    {k:<16} {v}")
    log(f"[rescore] desk_fit: {dict(sorted(fits.items(), reverse=True))}")

    left = sum(1 for (m,) in con.execute("SELECT meta FROM items")
               if not (json.loads(m).get("sleeves") if m else None))
    log(f"[rescore] papers still without a sleeve: {left}")


if __name__ == "__main__":
    main()
