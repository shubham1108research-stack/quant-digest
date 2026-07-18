#!/usr/bin/env python3
"""Weekly quant research digest. Collect -> dedup -> email -> archive.
No-LLM version: pure aggregation; triage/summaries/firm-pages removed."""

import datetime as dt
import os
import pathlib
import sys

import emailer
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

    html_body = emailer.render(fresh, NOTES)

    # archive first -- if SMTP fails we still keep the report and the state
    reports = pathlib.Path("reports")
    reports.mkdir(exist_ok=True)
    (reports / f"{dt.date.today().isoformat()}.html").write_text(
        html_body, encoding="utf-8")

    store.save(con, fresh)

    try:
        emailer.send(html_body)
        print("email sent")
    except Exception as e:                           # noqa: BLE001
        print(f"EMAIL SEND FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    return


if __name__ == "__main__":
    main()
