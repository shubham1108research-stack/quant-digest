#!/usr/bin/env python3
"""Walk a practitioner blog's RSS archive backwards, page by page.

WHY THIS EXISTS
sources.practitioner() reads page one of each feed and keeps whatever falls
inside the 30-day window. Alpha Architect's feed holds FIVE items -- about
seventeen days at their posting rate -- so the live collector sees only the
last handful, and anything older than the archive's start was never collected
at all. Their /blog/ page, their category pages and their WordPress REST API
all return a Cloudflare challenge; the feed does not, and it paginates.

    /category/.../value-investing/   403      Cloudflare challenge
    /wp-json/wp/v2/posts             403
    /feed/                           200
    /feed/?paged=N                   200      back to ~page 300

So this uses the one door the publisher leaves open, on the terms they leave it
open on: their own syndication feed, one request at a time, with a delay. It
does not touch the blocked paths and does not pretend to be a browser.

?cat= and ?s= are ignored by the feed -- a category-filtered request returns the
identical unfiltered bytes -- so filtering happens HERE, on the per-item
category tags the feed does carry.

    python tools/backfill_practitioner.py --feed alpha --dry-run
    python tools/backfill_practitioner.py --feed alpha --max-pages 40
    python tools/backfill_practitioner.py --feed alpha --category value
"""

import argparse
import pathlib
import sys
import time

import feedparser
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config   # noqa: E402
import sources  # noqa: E402
import store    # noqa: E402

UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}
# One request a second. The feed is served without complaint and there is no
# hurry: this is a backfill that runs once, not a hot path.
PAUSE = 1.0
# A page that yields no NEW links means the feed has started repeating rather
# than paginating, which some WordPress configurations do at the end. Two in a
# row is the stop signal; one could be a genuinely duplicated page.
DRY_PAGES = 2

FEEDS = {
    "alpha": ("Alpha Architect", "https://alphaarchitect.com/feed/", 4),
    "quantpedia": ("Quantpedia", "https://quantpedia.com/feed/", 4),
    "newfound": ("Newfound / Flirting with Models",
                 "https://blog.thinknewfound.com/feed/", 4),
}


def log(m):
    print(m, flush=True)


def page_url(base, n):
    return base if n == 1 else f"{base}?paged={n}"


def walk(base, label, section, max_pages, category, log, start_page=1):
    """Every distinct post the feed will give up, newest first.

    start_page exists so a walk that stopped at a page cap can be RESUMED
    rather than restarted. Re-walking from page one is not wrong -- the insert
    is idempotent -- but it spends hundreds of requests on a publisher's server
    to re-read what is already held, which is not a polite way to use a feed
    they are serving for free.
    """
    out, seen, dry = [], set(), 0
    for n in range(start_page, max_pages + 1):
        url = page_url(base, n)
        try:
            r = requests.get(url, headers=UA, timeout=45)
        except Exception as e:                          # noqa: BLE001
            log(f"[backfill] page {n}: {type(e).__name__}: {e}")
            break
        if r.status_code == 404:
            log(f"[backfill] page {n}: 404 -- past the end of the archive")
            break
        if r.status_code != 200:
            log(f"[backfill] page {n}: HTTP {r.status_code}; stopping")
            break
        feed = feedparser.parse(r.content)
        if not feed.entries:
            log(f"[backfill] page {n}: no entries; stopping")
            break

        fresh = 0
        for e in feed.entries:
            link = e.get("link", "")
            if not link or link in seen:
                continue
            seen.add(link)
            fresh += 1
            tags = [t.get("term", "") for t in e.get("tags", [])]
            if category and not any(category.lower() in t.lower() for t in tags):
                continue
            d = sources._entry_date(e)
            raw = e.get("description", "") or e.get("summary", "")
            authors, abstract = sources._split_byline(
                raw, sources._clean(e.get("author", "")))
            out.append({
                "title": sources._clean(e.get("title", "")),
                "authors": authors,
                "abstract": abstract[:config.ABSTRACT_CHARS],
                "url": link,
                "date": d.date().isoformat() if d else "",
                "source": label,
                "section": section,
                "_tags": tags,
            })
        if fresh == 0:
            dry += 1
            if dry >= DRY_PAGES:
                log(f"[backfill] page {n}: nothing new twice running; stopping")
                break
        else:
            dry = 0
        if n % 10 == 0 or n == 1:
            last = out[-1]["date"] if out else "-"
            log(f"[backfill] page {n}: {len(seen)} posts seen, "
                f"{len(out)} kept, back to {last}")
        time.sleep(PAUSE)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", default="alpha", choices=sorted(FEEDS))
    ap.add_argument("--max-pages", type=int, default=400)
    ap.add_argument("--start-page", type=int, default=1,
                    help="resume a capped walk instead of restarting it")
    ap.add_argument("--category", default="",
                    help="keep only posts whose feed tags contain this string")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what is out there; write nothing")
    args = ap.parse_args()

    label, base, section = FEEDS[args.feed]
    log(f"[backfill] {label} -- {base}")
    items = walk(base, label, section, args.max_pages, args.category, log,
                 start_page=args.start_page)
    if not items:
        log("[backfill] nothing collected")
        return

    dates = sorted(x["date"] for x in items if x["date"])
    log(f"\n[backfill] {len(items)} posts"
        + (f", {dates[0]} to {dates[-1]}" if dates else ""))
    withabs = sum(1 for x in items if (x.get("abstract") or "").strip())
    log(f"[backfill] with a usable summary: {withabs}/{len(items)}")

    import collections
    tags = collections.Counter(t for x in items for t in x["_tags"])
    log("\n[backfill] categories present:")
    for t, n in tags.most_common(20):
        log(f"    {t[:44]:<46} {n}")

    if args.dry_run:
        log("\n[backfill] --dry-run: nothing written")
        for x in items[:10]:
            log(f"    {x['date']:<12} {x['title'][:66]}")
        return

    for x in items:
        x.pop("_tags", None)
    con = store.connect()
    # filter_new assigns the uid and drops anything already held, so a re-run
    # is a no-op and this can be pointed at an archive it has already walked.
    fresh = store.filter_new(con, items)
    store.save(con, fresh)
    log(f"\n[backfill] inserted {len(fresh)} new rows "
        f"({len(items) - len(fresh)} already in the archive)")
    log("[backfill] they carry no scores yet -- the next rescore picks them up")


if __name__ == "__main__":
    main()
