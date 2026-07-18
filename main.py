#!/usr/bin/env python3
"""Weekly quant research digest. Collect -> dedup -> (LLM rank) -> email ->
archive + portal. The LLM ranking is optional and dormant unless
ANTHROPIC_API_KEY is set; without it this is the plain aggregation feed."""

import datetime as dt
import os
import pathlib
import sys

import emailer
import firms
import llm
import portal
import prominence
import sources
import store

NOTES: list[str] = []


def log(msg: str) -> None:
    print(msg)
    NOTES.append(msg)


def collect() -> list[dict]:
    sources.MAILTO = os.environ.get("CONTACT_EMAIL") \
        or os.environ.get("GMAIL_ADDRESS")
    items: list[dict] = []
    steps = [
        ("NEP", sources.nep),
        ("NBER", sources.nber),
        ("arXiv", sources.arxiv),
        ("Journals/Crossref", lambda: sources.journals(log)),
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
    raw = collect()
    fresh = store.filter_new(con, raw)
    print(f"collected {len(raw)}, new after dedup {len(fresh)}")

    # author-citation prominence (best-effort; enriches OpenAlex-native items)
    prominence.MAILTO = sources.MAILTO
    try:
        fresh = prominence.annotate(fresh, log)
    except Exception as e:                           # noqa: BLE001
        log(f"[prominence] failed: {type(e).__name__}: {e}")

    # optional LLM triage -- attaches rank_score/why; no-op without an API key
    fresh = llm.rank(fresh, log)

    html_body = emailer.render(fresh, NOTES)

    # archive first -- if SMTP fails we still keep the report and the state
    reports = pathlib.Path("reports")
    reports.mkdir(exist_ok=True)
    (reports / f"{dt.date.today().isoformat()}.html").write_text(
        html_body, encoding="utf-8")

    store.save(con, fresh)          # persists rank_score/why into the archive

    # rebuild the static portal from the full archive (incl. this run)
    try:
        n = portal.build(con)
        print(f"portal built -> docs/ ({n} items)")
    except Exception as e:                           # noqa: BLE001
        log(f"[portal] build failed: {type(e).__name__}: {e}")

    try:
        emailer.send(html_body)
        print("email sent")
    except Exception as e:                           # noqa: BLE001
        print(f"EMAIL SEND FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    return


if __name__ == "__main__":
    main()
