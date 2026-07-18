#!/usr/bin/env python3
"""Reduced-scope smoke test: minimal collect -> render -> REAL email send.

Verifies the parts that kept failing (SMTP send: BOM-stripped secrets +
empty-DIGEST_RECIPIENT fallback) and that collect->render still works after
the tiered-journal / topic-sweep changes -- WITHOUT the slow OpenAlex/S2
429-retry storms that make the full run take ~50 min.

Triggered manually via .github/workflows/test.yml. Does NOT touch state.db
or commit anything.
"""

import os
import sys

import emailer
import sources


def main() -> None:
    sources.MAILTO = os.environ.get("CONTACT_EMAIL") \
        or os.environ.get("GMAIL_ADDRESS")

    items: list[dict] = []

    # One fast real collector -- proves collect->render still parses cleanly.
    try:
        got = sources.arxiv()
        print(f"arXiv: {len(got)} items (using first 5)")
        items += got[:5]
    except Exception as e:                           # noqa: BLE001
        print(f"[arXiv] skipped: {type(e).__name__}: {e}")

    # Synthetic items to exercise tier badges (section 3) and the topic
    # sweep section (5) without hitting the throttled OpenAlex endpoints.
    items.append({
        "title": "[TEST] Synthetic Tier-1 journal item",
        "authors": "A. Author, B. Coauthor", "url": "https://example.com/t1",
        "date": "2026-07-18", "section": 3, "tier": "T1",
        "source": "journal:Journal of Finance"})
    items.append({
        "title": "[TEST] Synthetic topic-sweep (net-new) item",
        "authors": "", "url": "https://example.com/topic",
        "date": "2026-07-18", "section": 5, "source": "topic:momentum"})

    for it in items:
        it.setdefault("sources", [it["source"]])

    notes = ["[TEST RUN] reduced-scope smoke test -- NOT the weekly digest; "
             "OpenAlex/Semantic-Scholar collectors were skipped for speed."]

    html = emailer.render(items, notes)
    print(f"rendered {len(html)} bytes across {len(items)} items")

    try:
        emailer.send(html)
        print("EMAIL SENT OK")
    except Exception as e:                           # noqa: BLE001
        print(f"EMAIL SEND FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
