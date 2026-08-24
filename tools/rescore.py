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

import config  # noqa: E402
import llm     # noqa: E402
import scoring  # noqa: E402
import store   # noqa: E402

# Stamped alongside the scores so a re-score is RESUMABLE. Without it --force
# re-does every paper on every run, and since a full pass needs three runs the
# work would never converge -- run 2 would redo run 1, run 3 would redo run 2.
SCORED_CHARS_KEY = "scored_chars"

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
    ap.add_argument("--force", action="store_true",
                    help="re-score papers that already have scores (used after "
                         "an input change, e.g. the abstract cap)")
    ap.add_argument("--only-unscored", action="store_true",
                    help="skip papers that only need sleeve labels")
    ap.add_argument("--clear-stamps", action="store_true",
                    help="drop every scored_chars stamp first, so --force sees "
                         "the whole archive as outstanding again")
    args = ap.parse_args()

    con = store.connect()

    if args.clear_stamps:
        # The stamp records "already re-scored at the current abstract cap".
        # It is only trustworthy if it was written by a run that actually
        # scored the paper -- and until the _scored_now fix it was written for
        # every paper a run merely LOOKED at, so a run whose providers died
        # two-thirds through marked the whole archive done. There is no way to
        # tell the honest stamps from the false ones after the fact, so this
        # clears them all and lets one more --force pass rebuild the truth.
        # That pass is now resumable: each run stamps only what it scored, so
        # three short runs finish what one long one cannot.
        cleared = 0
        for uid, meta in con.execute(
                "SELECT uid, meta FROM items").fetchall():
            try:
                d = json.loads(meta or "{}")
            except Exception:                              # noqa: BLE001
                continue
            if SCORED_CHARS_KEY not in d:
                continue
            d.pop(SCORED_CHARS_KEY)
            con.execute("UPDATE items SET meta=? WHERE uid=?",
                        (json.dumps(d, default=str), uid))
            cleared += 1
        con.commit()
        log(f"[rescore] cleared {cleared} scored_chars stamps")
    todo, need_score, need_sleeve = [], 0, 0
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            d = json.loads(meta)
        except Exception:                                   # noqa: BLE001
            continue
        if scoring.is_junk(title):
            continue
        if d.get("retired"):            # never worth another LLM call
            continue
        unscored = d.get("rank_score") is None
        no_sleeve = not d.get("sleeves")
        # --force re-scores everything. Normally this tool only fills gaps,
        # which is right when the inputs are unchanged -- but the whole archive
        # was scored while llm._prompt truncated abstracts to 500 characters,
        # so every judgement was made on the motivation paragraph, before the
        # paper said what it did. Nothing is "outstanding" by the usual test,
        # and without this the fix could never reach the papers it fixes.
        if not (unscored or no_sleeve) and not args.force:
            continue
        # already re-scored at the current abstract cap: nothing would change,
        # so skip it even under --force
        if (args.force and not unscored and not no_sleeve
                and d.get(SCORED_CHARS_KEY) == config.ABSTRACT_CHARS):
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
        f"({need_score} unscored, {need_sleeve} scored but no sleeve)"
        + (f"; --force: re-scoring papers that already have scores, "
           f"abstract cap now {config.ABSTRACT_CHARS} chars" if args.force else ""))
    # unscored first: they are missing everything, not just a label
    todo.sort(key=lambda it: it.get("rank_score") is not None)
    if args.limit:
        todo = todo[:args.limit]
        log(f"[rescore] limited to {len(todo)}")
    if not todo:
        return
    if not llm.have_key():
        raise SystemExit("no LLM provider key set")

    # A dedicated backfill must not inherit the digest's time budgets. There
    # are TWO: the run-wide deadline and a per-pass cap (LLM_RANK_BUDGET_S,
    # 35min). Disabling only the first still stopped this run at 240 of 600.
    llm.start_run_budget(0)
    config.LLM_RANK_BUDGET_S = 0
    config.LLM_CONSENSUS_BUDGET_S = 0
    written = invalidated = 0
    sleeves = collections.Counter()
    fits = collections.Counter()

    _persisted = set()

    def persist(batch, _seen=_persisted):
        """Write one completed batch and COMMIT it.

        This runs per batch, not once at the end, because the end may never
        arrive: a backfill of the whole archive takes hours, and a run killed
        at its workflow timeout used to lose every score it had paid for --
        all of it still sitting in memory. Committing per batch turns a
        timeout from a total loss into a partial one, and the next run simply
        picks up the papers still outstanding."""
        nonlocal written, invalidated
        for it in batch:
            if it["uid"] in _seen:
                continue
            if not it.get("_scored_now"):
                continue                               # provider skipped it
            _seen.add(it["uid"])
            patch = {k: it[k] for k in SCORE_FIELDS if k in it}
            if not patch:
                continue
            patch[SCORED_CHARS_KEY] = config.ABSTRACT_CHARS
            if store.update_meta(con, it["uid"], patch):
                written += 1
                for sl in (it.get("sleeves") or []):
                    sleeves[sl] += 1
                fits[it.get("desk_fit", 0)] += 1
            # embedding text is title + topic + abstract; a new summary or a
            # changed topic makes the cached vector stale
            if (not it["_had_summary"] and it.get("summary")) or \
                    (it.get("topic") and it["topic"] != it["_old_topic"]):
                try:
                    con.execute("DELETE FROM embeddings WHERE uid=?", (it["uid"],))
                    invalidated += 1
                except Exception:                      # noqa: BLE001
                    pass
        con.commit()
        if written and written % 200 < len(batch):
            log(f"[rescore] checkpoint: {written} written so far")

    llm.rank(todo, log, on_batch=persist)

    # anything rank() scored but never handed to a checkpoint is caught here;
    # persist() records what it wrote so nothing is counted or written twice
    for it in todo:
        if it["uid"] in _persisted:
            continue
        # `_scored_now` is set by llm._apply_score, so it means "this run
        # produced this score". The old test -- rank_score is not None -- was
        # true for every paper that had EVER been scored, so when the provider
        # chain died two-thirds through, the 2,732 papers it never reached were
        # re-stamped scored_chars=1500 while still carrying their 500-char
        # scores. The resume filter then skipped them forever.
        if not it.get("_scored_now"):
            continue                                   # provider skipped it
        patch = {k: it[k] for k in SCORE_FIELDS if k in it}
        if patch:
            patch[SCORED_CHARS_KEY] = config.ABSTRACT_CHARS
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
