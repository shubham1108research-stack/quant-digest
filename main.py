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
import scoring
import sources
import store

NOTES: list[str] = []

# The digest runs every ~3 days (to stay well within free CI minutes). A CI run
# that starts within this many hours of the last completed run is skipped in a
# few seconds -- so it doesn't matter how often the cron(s) fire. A manual
# force run (FORCE_RUN=true) or a local run always executes.
DIGEST_MIN_INTERVAL_HOURS = 20        # daily -- a touch under 24 so cron jitter
                                      # never pushes a run into the next day

# Strip credentials/PII that an exception message (e.g. a request URL) might
# carry before it reaches the run notes -> the archived, possibly-shared report.
_REDACT = re.compile(r"(?i)(key=|mailto=|api[_-]?key[=:]\s*)[^&\s\"']+")


def log(msg: str) -> None:
    msg = _REDACT.sub(r"\1REDACTED", str(msg))
    print(msg)
    NOTES.append(msg)


def collect(existing: set, con=None) -> list[dict]:
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
        # publishers who block crawlers but will post you the same content:
        # SSRN eJournals, Macrosynergy. Subscribed from a dedicated mailbox.
        ("Inbox", lambda: sources.inbox(log, con)),
        # last: dedup keeps richer records above canonical; this adds net-new
        ("OpenAlex-topics", lambda: sources.openalex_topics(log)),
    ]
    # A collector returning zero looks exactly like a quiet week, which is how
    # Macrosynergy sat at 0 posts indefinitely (its feed had started serving a
    # Cloudflare challenge) and nothing said so. Remember last run's counts and
    # complain when a source is empty twice running.
    import json as _json
    prev = {}
    if con is not None:
        try:
            prev = _json.loads(store.kv_get(con, "collector_counts", "{}"))
        except Exception:                            # noqa: BLE001
            prev = {}
    counts = {}

    for name, fn in steps:
        try:
            got = fn()
            # Drop records that are not papers before they cost anything.
            # sources.is_record_sane catches journal containers, datasets and
            # metadata dated years into the future -- a Zenodo deposit of an
            # entire journal ("Journal of GIS based Historical Studies",
            # authored by "JANGIS", dated 2029) reached the archive through the
            # finance topic sweep because OpenAlex had filed it under Finance.
            # The LLM did mark it off_topic, but only after paying for the call,
            # and it stayed in the archive, the embeddings, the graph and the map.
            bad = [g for g in got if g.get("_reject")]
            got = [g for g in got if not g.get("_reject")]
            if bad:
                log(f"[{name}] dropped {len(bad)} non-paper records "
                    f"(e.g. {bad[0]['_reject']})")
            print(f"{name}: {len(got)} items")
            counts[name] = len(got)
            if not got and prev.get(name) == 0:
                log(f"[{name}] EMPTY TWICE RUNNING -- feed may be dead "
                    f"(blocked, moved, or the selector has rotted)")
            items += got
        except Exception as e:                       # noqa: BLE001
            log(f"[{name}] FAILED this week: {type(e).__name__}: {e}")
    if con is not None:
        store.kv_set(con, "collector_counts", _json.dumps(counts))
    return items


def main() -> None:
    con = store.connect()

    # Every-3-days cadence, enforced HERE (not by the cron) to stay within free
    # CI minutes: skip if a run completed within DIGEST_MIN_INTERVAL_HOURS,
    # however often the crons fire. FORCE_RUN=true (the workflow's `force`
    # input) runs on demand; a local run (no GITHUB_ACTIONS) always runs, so
    # testing/catch-up is never blocked.
    now = dt.datetime.now(dt.timezone.utc)
    force = os.environ.get("FORCE_RUN") == "true"
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    if in_ci and not force:
        last = store.kv_get(con, "last_run_ts")
        if last:
            try:
                hrs = (now - dt.datetime.fromisoformat(last)).total_seconds() / 3600
            except Exception:                          # noqa: BLE001
                hrs = 1e9
            if hrs < DIGEST_MIN_INTERVAL_HOURS:
                print(f"[guard] last run {hrs:.1f}h ago (< "
                      f"{DIGEST_MIN_INTERVAL_HOURS}h); skipping (force=true to "
                      f"override)")
                return

    existing = {r[0] for r in con.execute("SELECT uid FROM items")}
    raw = collect(existing, con)
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

    # one wall-clock budget shared by EVERY LLM pass below (triage, consensus,
    # and monthly's re-scores) so they can't compound past the CI job timeout
    import config
    llm.start_run_budget(config.LLM_RUN_BUDGET_S)

    # optional LLM triage -- attaches rank_score/why; no-op without an API key
    # scoring.llm_score, not llm.rank: only the former applies is_junk, whose
    # own docstring says junk is 'never worth spending LLM quota on'. 76 junk
    # rows are archived and 18 carry full rubric scores -- quota spent on
    # tables of contents, which then compete for slots in Recent/For You.
    scoring.llm_score(research, log)   # mutates the shared item dicts in place
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

    # CFTC positioning for the For You briefing. Publishes Friday 15:30 ET for
    # the prior Tuesday, so six of seven daily runs rewrite an identical file;
    # that is cheaper than a second schedule to maintain. Never fatal -- the
    # digest is about papers, and a CFTC outage should not cost us the email.
    try:
        from tools import cot as _cot
        _cot.build()
    except Exception as e:                           # noqa: BLE001
        log(f"[cot] failed: {type(e).__name__}: {e}")

    # rebuild the static portal from the full archive (incl. this run)
    try:
        n = portal.build(con)
        print(f"portal built -> docs/ ({n} items)")
    except Exception as e:                           # noqa: BLE001
        log(f"[portal] build failed: {type(e).__name__}: {e}")

    # stamp completion (collection/scoring/backfill/portal all attempted) so a
    # firing within DIGEST_MIN_INTERVAL_HOURS skips re-doing the LLM-costly
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
