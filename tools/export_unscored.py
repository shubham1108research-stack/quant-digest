#!/usr/bin/env python3
"""Export papers for scoring by hand, and re-export scored ones for calibration.

WHY THIS EXISTS. Scoring has always gone through llm.py to a paid API. Doing it
locally instead -- reading the abstracts and applying the same rubric directly
-- costs nothing per paper, but the scorer has no R2 credentials and therefore
cannot reach state.db. So the database stays where it is and the ABSTRACTS come
out: a CI job with the credentials exports, the scorer works on a file, and
tools/ingest_scores.py writes the verdicts back through the same code path
llm.py uses.

TWO MODES, and the second is the one that makes the first trustworthy.

  --unscored     papers with no relevance_category. The backlog.
  --sample-scored N
                 papers that ALREADY have scores, with those scores WITHHELD.
                 Score them blind, ingest with --dry-run, and compare against
                 what is already there. A different scorer is not just noisier
                 than the last one, it is differently CALIBRATED, and a
                 systematic half-level of generosity would move every paper it
                 touches as a block -- which reads as "the practitioner
                 journals are unusually strong" rather than as an artefact.
                 The comparison is cheap and the alternative is finding out
                 later from a ranking nobody can explain.

The export deliberately carries no existing scores in either mode. A scorer
that can see the previous verdict is not an independent one.

    python tools/export_unscored.py --unscored --limit 200 --out batch.json
    python tools/export_unscored.py --sample-scored 60 --out calib.json
"""

import argparse
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config   # noqa: E402
import scoring  # noqa: E402
import store    # noqa: E402


def log(m):
    print(m, flush=True)


def _rows(con):
    for uid, title, source, url, meta in con.execute(
            "SELECT uid, title, source, url, meta FROM items"):
        try:
            m = json.loads(meta)
        except Exception:                                    # noqa: BLE001
            m = {}
        yield uid, title, source, url, m


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--unscored", action="store_true",
                   help="papers with no relevance_category")
    g.add_argument("--sample-scored", type=int, metavar="N",
                   help="N already-scored papers, scores withheld, for calibration")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=11,
                    help="sample seed; fixed so a calibration set is reproducible")
    # The rubric reads this many characters. Exporting more would let the
    # scorer see what the API scorer never did, which is a calibration
    # difference dressed up as a scoring difference.
    ap.add_argument("--chars", type=int, default=config.ABSTRACT_CHARS)
    args = ap.parse_args()

    con = store.connect()
    out, skipped_junk, skipped_thin = [], 0, 0
    scored_pool = []
    for uid, title, source, url, m in _rows(con):
        if scoring.is_junk(title) or m.get("retired"):
            continue
        abstract = (m.get("abstract") or "").strip()
        has_score = bool(m.get("relevance_category"))
        rec = {
            "uid": uid,
            "title": title or "",
            "abstract": abstract[:args.chars],
            "journal": m.get("journal") or source or "",
            "authors": m.get("authors") or "",
            "date": m.get("date") or "",
            "url": url or "",
        }
        if args.sample_scored:
            if has_score:
                scored_pool.append(rec)
            continue
        if has_score:
            continue
        if scoring.is_junk(title):
            skipped_junk += 1
            continue
        # An abstract-less paper cannot be scored against a rubric that asks
        # for a method, a sample and a contribution. Sending the title alone
        # would produce a confident guess, which is worse than a gap.
        if len(abstract) < 120:
            skipped_thin += 1
            continue
        out.append(rec)

    if args.sample_scored:
        random.Random(args.seed).shuffle(scored_pool)
        out = scored_pool[:args.sample_scored]
        log(f"[export] {len(scored_pool):,} scored papers in the archive; "
            f"sampled {len(out)} for calibration (seed {args.seed})")
    else:
        log(f"[export] {len(out):,} unscored papers with a usable abstract; "
            f"skipped {skipped_thin:,} with almost no abstract, "
            f"{skipped_junk:,} junk titles")
    if args.limit:
        out = out[:args.limit]
        log(f"[export] limited to {len(out):,}")

    p = pathlib.Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    chars = sum(len(r["abstract"]) for r in out)
    log(f"[export] wrote {p} -- {len(out):,} papers, "
        f"{chars/1e3:,.0f}k characters of abstract "
        f"({chars/max(len(out),1):,.0f} per paper)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
