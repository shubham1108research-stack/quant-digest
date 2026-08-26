#!/usr/bin/env python3
"""Write hand-produced rubric scores back into state.db.

THE ONE RULE HERE: these verdicts go through llm._apply_score, the same
function the API path uses. Every derived field -- relevance_posterior, the
Bayesian novelty posterior, rank_score -- is then computed by identical code
from identical inputs, so a hand-scored paper and an API-scored one differ
only in who read the abstract. Re-implementing that arithmetic here would be
how the two quietly become different scales that get compared anyway.

VALIDATION IS STRICT AND LOUD. A malformed entry is refused by uid rather than
written partially: a paper carrying three of four rubric axes is worse than an
unscored one, because everything downstream treats the presence of
relevance_category as "this has been judged".

PROVENANCE IS MANDATORY. --scored-by is required, unvalidated against any list,
and stamped on every row. llm.py records that the free tiers were removed
because nothing on a scored item said which model judged it; writing a second
scorer into the archive without that field would rebuild the same hole by hand.

    python tools/ingest_scores.py scored.json --scored-by claude-opus-5 --dry-run
    python tools/ingest_scores.py scored.json --scored-by claude-opus-5
"""

import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import llm     # noqa: E402
import store   # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from rescore import SCORE_FIELDS, SCORED_CHARS_KEY   # noqa: E402  -- one whitelist, not two

LEVELS = ("relevance", "generality", "contribution", "testability", "desk_fit")
REQUIRED = LEVELS + ("relevance_category", "novelty_type", "antecedent_match",
                     "topic", "summary")
FLAGS = ("isolated_backtest_only", "no_costs_mentioned",
         "extreme_claimed_sharpe", "weak_stat_support")
CATEGORIES = ("core_fit", "adjacent", "off_topic")
NOVELTY = ("theory", "method", "empirical", "none")
ANTECEDENT = ("matches_known", "ambiguous", "no_antecedent")


def log(m):
    print(m, flush=True)


def validate(rec):
    """Return a list of problems. Empty means the record is usable."""
    bad = []
    for k in REQUIRED:
        if k not in rec:
            bad.append(f"missing {k}")
    for k in LEVELS:
        v = rec.get(k)
        if not isinstance(v, dict):
            bad.append(f"{k} is not an object")
            continue
        lvl = v.get("level")
        if not isinstance(lvl, int) or not 0 <= lvl <= 3:
            bad.append(f"{k}.level is {lvl!r}, want int 0-3")
        if not str(v.get("why") or "").strip():
            bad.append(f"{k}.why is empty")
    if rec.get("relevance_category") not in CATEGORIES:
        bad.append(f"relevance_category {rec.get('relevance_category')!r}")
    if rec.get("novelty_type") not in NOVELTY:
        bad.append(f"novelty_type {rec.get('novelty_type')!r}")
    if rec.get("antecedent_match") not in ANTECEDENT:
        bad.append(f"antecedent_match {rec.get('antecedent_match')!r}")
    for k in FLAGS:
        if k in rec and rec[k] not in (True, False, None):
            bad.append(f"{k} is {rec[k]!r}, want true/false/null")
    sl = rec.get("sleeves") or []
    if not isinstance(sl, list):
        bad.append("sleeves is not a list")
    else:
        unknown = [s for s in sl if s not in config.SLEEVES]
        if unknown:
            bad.append(f"unknown sleeves {unknown}")
    tg = rec.get("tags") or []
    if not isinstance(tg, list):
        bad.append("tags is not a list")
    else:
        # Dropped rather than fatal: the rubric says an unknown tag is
        # discarded, and a tag is a filter -- a wrong one is worse than none.
        rec["tags"] = [t for t in tg if t in config.TAGS][:config.TAGS_MAX]
    if not str(rec.get("summary") or "").strip():
        bad.append("summary is empty")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--scored-by", required=True,
                    help="who judged these; stamped on every row")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and report, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="overwrite papers that already carry a score")
    args = ap.parse_args()

    recs = []
    for f in args.files:
        d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
        recs.extend(d if isinstance(d, list) else [d])
    log(f"[ingest] {len(recs):,} score records from {len(args.files)} file(s)")

    con = store.connect()
    have = {}
    for uid, meta in con.execute("SELECT uid, meta FROM items"):
        try:
            have[uid] = json.loads(meta)
        except Exception:                                     # noqa: BLE001
            have[uid] = {}

    written = rejected = skipped = 0
    problems = collections.Counter()
    cats = collections.Counter()
    for rec in recs:
        uid = rec.get("uid")
        if not uid or uid not in have:
            rejected += 1
            problems["uid not in the archive"] += 1
            continue
        if have[uid].get("relevance_category") and not args.force:
            skipped += 1
            continue
        bad = validate(rec)
        if bad:
            rejected += 1
            for b in bad:
                problems[b.split()[0] + " " + b.split()[1] if len(b.split()) > 1 else b] += 1
            log(f"[ingest] REJECT {uid}: {'; '.join(bad[:4])}")
            continue
        # Same code path as the API scorer, deliberately. _apply_score also
        # computes the posteriors and rank_score from these levels.
        it = {"uid": uid, "title": have[uid].get("title") or ""}
        rec["_scored_by"] = args.scored_by
        try:
            llm._apply_score(it, rec)
        except Exception as e:                                # noqa: BLE001
            rejected += 1
            problems[f"_apply_score raised {type(e).__name__}"] += 1
            log(f"[ingest] REJECT {uid}: _apply_score: {e}")
            continue
        cats[it.get("relevance_category")] += 1
        if args.dry_run:
            written += 1
            continue
        patch = {k: it[k] for k in SCORE_FIELDS if k in it}
        # Same stamp rescore.py writes, so a paper scored here is not treated
        # as outstanding by the next rescore run and silently done twice.
        patch[SCORED_CHARS_KEY] = config.ABSTRACT_CHARS
        if store.update_meta(con, uid, patch):
            written += 1
    if not args.dry_run:
        con.commit()

    log(f"\n[ingest] {'would write' if args.dry_run else 'wrote'} {written:,}; "
        f"{skipped:,} already scored (use --force to replace); {rejected:,} rejected")
    if cats:
        log("[ingest] relevance_category: "
            + ", ".join(f"{k}={v:,}" for k, v in cats.most_common()))
    if problems:
        log("[ingest] problems:")
        for p, n in problems.most_common(12):
            log(f"    {n:>5}  {p}")
    # A run that rejected more than it wrote is a format mismatch, not a batch
    # of bad papers, and should not look like a success in CI.
    if rejected > written:
        log("[ingest] FAILING: more records were rejected than written")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
