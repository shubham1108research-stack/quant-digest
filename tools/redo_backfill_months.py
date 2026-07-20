#!/usr/bin/env python3
"""One-off: redo the backfill for already-completed months so their Monthly
membership picks up the new Bayesian relevance gate (RELEVANCE_CONFIDENCE)
instead of the old flat relevance-level==3 requirement, and so their scored
candidates get permanently persisted to the archive this time (see
monthly.backfill_step -- previously only the top-N winners survived).

Needs real LLM provider keys (this makes real scoring calls) -- run via
GitHub Actions (`gh workflow run redo-months.yml`), not locally without keys.

Usage: python tools/redo_backfill_months.py 2026-03 2026-04 2026-05 2026-06
"""

import json
import sys

sys.path.insert(0, ".")
import backfill  # noqa: E402
import config    # noqa: E402
import llm       # noqa: E402
import monthly   # noqa: E402
import portal    # noqa: E402
import scoring   # noqa: E402
import store     # noqa: E402


def log(msg):
    print(msg, flush=True)


def redo_month(con, mo, month: str) -> None:
    log(f"=== {month} ===")
    candidates = backfill.fetch_month(month, log)
    if not candidates:
        log(f"[{month}] no articles found; leaving existing entry untouched")
        return
    scoring.attach_s2(candidates, log)
    scoring.llm_score(candidates, log)
    try:
        llm.consensus(candidates, log, max_batches=config.CONSENSUS_MAX_BATCHES)
    except Exception as e:                              # noqa: BLE001
        log(f"[consensus] {month} failed: {type(e).__name__}: {e}")
    to_save = store.filter_new(con, candidates)
    store.save(con, to_save)
    mo[month] = scoring.composite_entries(candidates, config.MONTHLY_TOP_N)
    log(f"[{month}] {len(mo[month])} picks from {len(candidates)} candidates "
        f"({len(to_save)} new items archived)")


def main() -> None:
    months = sys.argv[1:]
    if not months:
        print(__doc__)
        sys.exit(1)
    con = store.connect()
    mo = json.loads(open("docs/monthly.json", encoding="utf-8").read())
    for month in months:
        redo_month(con, mo, month)
        open("docs/monthly.json", "w", encoding="utf-8").write(
            json.dumps(mo, default=str))   # save after each month, not just at the end
    n = portal.build(con)
    log(f"portal rebuilt -> docs/ ({n} items)")


if __name__ == "__main__":
    main()
