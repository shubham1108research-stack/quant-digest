#!/usr/bin/env python3
"""Weekly quant research digest. Collect -> dedup -> (LLM rank) -> email ->
archive + portal. The LLM ranking is optional and dormant unless
ANTHROPIC_API_KEY is set; without it this is the plain aggregation feed."""

import datetime as dt
import os
import pathlib
import re
import sys

import emailer
import firms
import llm
import monthly
import portal
import prominence
import sources
import store

NOTES: list[str] = []

# A backup `schedule` firing is skipped if a run completed within this many
# hours -- long enough to cover the 23:30->04:30 UTC primary/backup gap, short
# enough that a genuinely missed day (>~1 run cycle) still triggers a backup.
GUARD_HOURS = 18

# Strip credentials/PII that an exception message (e.g. a request URL) might
# carry before it reaches the run notes -> the archived, possibly-shared report.
_REDACT = re.compile(r"(?i)(key=|mailto=|api[_-]?key[=:]\s*)[^&\s\"']+")


def log(msg: str) -> None:
    msg = _REDACT.sub(r"\1REDACTED", str(msg))
    print(msg)
    NOTES.append(msg)


def collect(existing: set) -> list[dict]:
    sources.MAILTO = os.environ.get("CONTACT_EMAIL") \
        or os.environ.get("GMAIL_ADDRESS")
    items: list[dict] = []
    steps = [
        ("NEP", sources.nep),
        ("NBER", lambda: sources.nber(log)),
        ("Watchlist authors", lambda: sources.watchlist(log)),
        ("arXiv", lambda: sources.arxiv(log)),
        ("Journals/Crossref", lambda: sources.journals(log)),
        ("PMR journals", lambda: sources.pmr(log, existing)),
        ("SSRN/Crossref", lambda: sources.ssrn_crossref(log)),
        ("OpenAlex-preprints", lambda: sources.openalex_preprints(log)),
        ("SemanticScholar", lambda: sources.semantic_scholar(log)),
        ("Quantocracy", sources.quantocracy),
        ("Practitioner", lambda: sources.practitioner(log)),
        ("Firms (AQR/Man/RA)", lambda: firms.firms(log)),
        # last: dedup keeps richer records above canonical; this adds net-new
        ("OpenAlex-topics", lambda: sources.openalex_topics(log)),
    ]
    for name, fn in steps:
        try:
            got = fn()
            print(f"{name}: {len(got)} items")
            items += got
        except Exception as e:                       # noqa: BLE001
            log(f"[{name}] FAILED this week: {type(e).__name__}: {e}")
    return items


def main() -> None:
    con = store.connect()

    # The external cron (cron-job.org, workflow_dispatch) is the primary daily
    # trigger; GitHub's own `schedule` crons are BACKUPS that should fire only
    # if the primary missed. Guard on a TIME WINDOW, not the calendar date: the
    # primary runs at 23:30 UTC and the first backup at 04:30 UTC -- only 5h
    # apart but across the UTC midnight, so a date-based guard wrongly treated
    # them as different days and ran the whole LLM-costly pipeline TWICE daily.
    # A manual/dispatch run always executes (guard applies to `schedule` only),
    # so the primary and any catch-up are never blocked.
    now = dt.datetime.now(dt.timezone.utc)
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        last = store.kv_get(con, "last_run_ts")
        if last:
            try:
                hrs = (now - dt.datetime.fromisoformat(last)).total_seconds() / 3600
            except Exception:                          # noqa: BLE001
                hrs = 999
            if hrs < GUARD_HOURS:
                print(f"[guard] a run completed {hrs:.1f}h ago (< {GUARD_HOURS}h); "
                      f"skipping this backup schedule firing")
                return

    existing = {r[0] for r in con.execute("SELECT uid FROM items")}
    raw = collect(existing)
    fresh = store.filter_new(con, raw)
    print(f"collected {len(raw)}, new after dedup {len(fresh)}")

    # flag watched authors' papers that arrived via ANY source (NBER/journals/
    # SSRN/arXiv), not just the OpenAlex watchlist pull -- OpenAlex lags on
    # fresh working papers, so a new Kelly NBER paper is collected here before
    # OpenAlex indexes it. This tags them so they get scoring priority + the
    # Watched surfacing regardless of which feed carried them.
    try:
        sources.watchlist_crossmatch(fresh, log)
    except Exception as e:                             # noqa: BLE001
        log(f"[watchlist] cross-match failed: {type(e).__name__}: {e}")

    # enrich via Semantic Scholar: fills missing abstracts + attaches paper
    # citation count and author h-index (feeds the monthly composite)
    try:
        fresh = sources.enrich_abstracts(fresh, log)
    except Exception as e:                           # noqa: BLE001
        log(f"[enrich] failed: {type(e).__name__}: {e}")

    # author-citation prominence (best-effort; enriches OpenAlex-native items)
    prominence.MAILTO = sources.MAILTO
    try:
        fresh = prominence.annotate(fresh, log)
    except Exception as e:                           # noqa: BLE001
        log(f"[prominence] failed: {type(e).__name__}: {e}")

    # Practitioner & house-research posts (section 4: blogs, AQR/Man/RA,
    # Quantocracy) are NOT research papers -- skip the rubric scoring entirely
    # (saves LLM quota) and surface them in the portal's Practitioners tab by
    # source. Only sections 1/2/3/5 go through triage + consensus.
    research = [it for it in fresh if str(it.get("section")) != "4"]
    n_prac = len(fresh) - len(research)
    if n_prac:
        log(f"[llm] {n_prac} practitioner items skipped (not scored)")

    # watchlist authors' papers go FIRST so the LLM batch budget always scores
    # them before the general backlog -- they must never be dropped unscored
    research.sort(key=lambda it: bool(it.get("watchlist")), reverse=True)

    # optional LLM triage -- attaches rank_score/why; no-op without an API key
    llm.rank(research, log)               # mutates the shared item dicts in place
    # ensemble consensus on the promising shortlist (>=2 providers): multiple
    # models re-score together; disagreement is flagged provisional
    try:
        llm.consensus(research, log)
    except Exception as e:                           # noqa: BLE001
        log(f"[consensus] failed: {type(e).__name__}: {e}")

    # author reputation score + bounded multiplier on every item, so Recent /
    # For You / the email / data.json all apply the SAME author nudge the
    # Monthly composite does (consistent, pool-independent)
    try:
        import scoring
        scoring.annotate_reputation(fresh, log)
    except Exception as e:                           # noqa: BLE001
        log(f"[author] reputation annotate failed: {type(e).__name__}: {e}")

    html_body = emailer.render(fresh, NOTES)

    # archive first -- if SMTP fails we still keep the report and the state
    reports = pathlib.Path("reports")
    reports.mkdir(exist_ok=True)
    (reports / f"{dt.date.today().isoformat()}.html").write_text(
        html_body, encoding="utf-8")

    store.save(con, fresh)          # persists scores into the archive

    # monthly top-20 (present recompute + promote-seminal + one backward month)
    try:
        monthly.run(con, fresh, log)
    except Exception as e:                           # noqa: BLE001
        log(f"[monthly] failed: {type(e).__name__}: {e}")

    # rebuild the static portal from the full archive (incl. this run)
    try:
        n = portal.build(con)
        print(f"portal built -> docs/ ({n} items)")
    except Exception as e:                           # noqa: BLE001
        log(f"[portal] build failed: {type(e).__name__}: {e}")

    # stamp completion (collection/scoring/backfill/portal all attempted) so a
    # backup `schedule` firing within GUARD_HOURS skips re-doing the LLM-costly
    # work, even if the email send below fails
    store.kv_set(con, "last_run_ts", now.isoformat())
    store.kv_set(con, "last_run_date", dt.date.today().isoformat())  # display/back-compat

    try:
        emailer.send(html_body)
        print("email sent")
    except Exception as e:                           # noqa: BLE001
        print(f"EMAIL SEND FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    return


if __name__ == "__main__":
    main()
