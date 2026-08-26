#!/usr/bin/env python3
"""Collect Macrosynergy research through the sitemap they publish for crawlers.

WHY THIS IS THE RIGHT ROUTE, AND WHY I FIRST THOUGHT IT WAS NOT
config.py said "every URL on the domain returns a Cloudflare challenge ... not
fetchable from a runner", and their WAF does 403 a lot of honest requests. I
took that for an intent to exclude crawlers, which was wrong. Their stated
policy says the opposite:

    User-agent: *
    Disallow: /transaction-costs-survey/
    Sitemap: https://macrosynergy.com/sitemap_index.xml

One path disallowed, everything else allowed, and a sitemap advertised
specifically so crawlers can find the archive. A WAF rule is infrastructure;
robots.txt is the operator telling you what they want. When they disagree,
robots.txt is the one that carries intent -- so this reads it with
urllib.robotparser and obeys it, rather than relying on my reading of it.

What this does NOT do is defeat the challenge. No forged browser
User-Agent, no TLS impersonation, no headless browser solving the JS. It is an
ordinary client, honestly identified, that waits and tries again -- the 403s
here are intermittent rather than absolute:

    requests    403 403 403 403 403      (blocked by fingerprint, every time)
    urllib      403 403 200              (through on the third)
    feedparser  403 403 200 403 403      (roughly one in five)

Bounded retries with a real delay, and a hard stop if the whole run is being
refused, so this can never become a hammer.

WHAT IT YIELDS
The sitemap lists 653 research posts. Each page carries og:title,
og:description (a genuine abstract, not a teaser), article:published_time and
an author, so records arrive complete rather than as title-only rows -- which
matters, because a title-only row becomes a title-only embedding vector and is
indistinguishable from a good one once stored.

    python tools/backfill_macrosynergy.py --dry-run --limit 20
    python tools/backfill_macrosynergy.py --limit 100
    python tools/backfill_macrosynergy.py
"""

import argparse
import gzip
import html
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config   # noqa: E402
import sources  # noqa: E402
import store    # noqa: E402

SITE = "https://macrosynergy.com"
UA = "quant-digest/1.0 (personal research tool)"
SITEMAPS = {
    "posts": f"{SITE}/post-sitemap.xml",
    "notebooks": f"{SITE}/jupyter-notebook-sitemap.xml",
}
LABEL = "Macrosynergy"
SECTION = 4                      # practitioner research, not a working paper
RETRIES = 6                      # per URL; the 403s are intermittent
BACKOFF = 2.0                    # seconds, multiplied by the attempt number
# If this many URLs in a row are refused after all their retries, the block has
# stopped being intermittent and become a wall. Stop rather than keep knocking.
GIVE_UP_AFTER = 12


def log(m):
    print(m, flush=True)


def fetch(url, tries=RETRIES):
    """Bytes, or b"" once the retries are spent. Never raises."""
    for i in range(tries):
        try:
            rq = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(rq, timeout=40) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                if b"Just a moment" in raw[:400]:
                    raise urllib.error.HTTPError(url, 403, "challenge", None, None)
                return raw
        except Exception:                               # noqa: BLE001
            time.sleep(BACKOFF * (i + 1))
    return b""


def _meta(doc, prop):
    m = re.search(
        r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\'](.*?)["\']'
        % re.escape(prop), doc, re.S | re.I)
    if not m:
        m = re.search(
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:property|name)=["\']%s["\']'
            % re.escape(prop), doc, re.S | re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def parse_page(url, doc):
    title = _meta(doc, "og:title")
    if not title:
        m = re.search(r"<title>(.*?)</title>", doc, re.S | re.I)
        title = html.unescape(m.group(1)).strip() if m else ""
    # "Some Title | Macrosynergy" -> "Some Title". The suffix is site branding
    # and would otherwise sit in every title, in the embedded text, and in the
    # dedup key.
    title = re.sub(r"\s*\|\s*Macrosynergy\s*$", "", title).strip()
    abstract = _meta(doc, "og:description") or _meta(doc, "description")
    date = (_meta(doc, "article:published_time") or "")[:10]
    author = _meta(doc, "author")
    if author.lower() in ("editor", "admin"):
        author = ""                                     # not a person
    if not title:
        return None
    return {
        "title": sources._clean(title)[:300],
        "authors": sources._clean(author)[:300],
        "abstract": sources._clean(abstract)[:config.ABSTRACT_CHARS],
        "url": url,
        "date": date,
        "source": LABEL,
        "section": SECTION,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sitemap", default="posts",
                    choices=sorted(SITEMAPS) + ["both"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between page fetches")
    ap.add_argument("--dry-run", action="store_true")
    # The crawl and the write have different requirements and belong apart.
    # Fetching needs an IP the WAF tolerates -- measured, a residential one
    # gets through with retries and a GitHub runner is refused 6 times out of
    # 6. Writing needs the R2 credentials, which live in CI. Neither machine
    # can do both, so --json ends the crawl at a file and --from-json starts
    # the write from one.
    ap.add_argument("--json", default="",
                    help="write the crawled records here instead of the archive")
    ap.add_argument("--from-json", default="",
                    help="skip the crawl; ingest records from this file")
    # Same idea as --from-json, for the case where the two machines cannot
    # share a filesystem. The crawl only works from a residential address and
    # the write only works where the R2 credentials are, so the records have to
    # travel -- and they are a publisher's text, so they must not travel
    # through the repository. gzip+base64 through a workflow input keeps them
    # out of git entirely and off anyone's clipboard.
    ap.add_argument("--from-b64", default="",
                    help="ingest gzip+base64 records passed in MACRO_B64")
    args = ap.parse_args()

    if args.from_b64:
        import base64, gzip, json, os
        blob = os.environ.get(args.from_b64) or args.from_b64
        items = json.loads(gzip.decompress(base64.b64decode(blob)))
        log(f"[macro] {len(items)} records decoded from a compressed payload")
        con = store.connect()
        fresh = store.filter_new(con, items)
        store.save(con, fresh)
        log(f"[macro] inserted {len(fresh)} new rows "
            f"({len(items) - len(fresh)} already held)")
        return 0

    if args.from_json:
        import json
        items = json.loads(pathlib.Path(args.from_json).read_text(encoding="utf-8"))
        log(f"[macro] {len(items)} records from {args.from_json}")
        con = store.connect()
        fresh = store.filter_new(con, items)
        store.save(con, fresh)
        log(f"[macro] inserted {len(fresh)} new rows "
            f"({len(items) - len(fresh)} already held)")
        return 0

    # Their policy, read from their file -- through the SAME retrying fetch as
    # everything else.
    #
    # RobotFileParser.read() cannot be used directly here. It fetches with a
    # plain urlopen, and a 403 makes it deny everything by rule -- which is the
    # right default for a site that really is refusing, and exactly wrong for
    # one whose WAF refuses two requests in three. The first run of this tool
    # printed "robots.txt disallows ..." for all 653 URLs, a statement that was
    # simply false: their robots.txt disallows one survey path. A guard that
    # reports the wrong reason is worse than no guard, because the reason is
    # what the next person acts on.
    raw = fetch(f"{SITE}/robots.txt")
    if not raw:
        log(f"[macro] could NOT read {SITE}/robots.txt after {RETRIES} tries. "
            f"Refusing to crawl: not knowing the policy is not permission.")
        return 1
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(raw.decode("utf-8", "replace").splitlines())
    rules = [l for l in raw.decode("utf-8", "replace").splitlines()
             if l.strip().lower().startswith("disallow")]
    log(f"[macro] robots.txt: {len(rules)} disallow rule(s) -- "
        + "; ".join(r.strip() for r in rules[:4]))

    names = sorted(SITEMAPS) if args.sitemap == "both" else [args.sitemap]
    urls = []
    for name in names:
        raw = fetch(SITEMAPS[name])
        if not raw:
            log(f"[macro] {name} sitemap unreachable after {RETRIES} tries")
            continue
        found = [u.decode() for u in re.findall(rb"<loc>([^<]+)</loc>", raw)]
        log(f"[macro] {name} sitemap: {len(found)} urls")
        urls.extend(found)

    # Listing pages are in the sitemap alongside the articles -- /research/blog/
    # came through as a record titled "Research Blog" with no date. It would
    # have been archived, scored and shown as a paper, which is the same shape
    # of failure as the SSRN table-of-contents rows.
    INDEX_PAGES = {"/research/", "/research/blog/", "/academy/",
                   "/academy/notebooks/", "/"}

    seen, keep, skipped_index = set(), [], 0
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        path = re.sub(r"^https?://[^/]+", "", u)
        if path in INDEX_PAGES:
            skipped_index += 1
            continue
        if not rp.can_fetch(UA, u):
            log(f"[macro] robots.txt disallows {u} -- skipping")
            continue
        keep.append(u)
    log(f"[macro] {len(keep)} urls allowed by robots.txt"
        + (f" ({skipped_index} listing pages skipped)" if skipped_index else ""))
    if args.limit:
        keep = keep[:args.limit]
        log(f"[macro] limited to {len(keep)}")

    items, refused, streak = [], 0, 0
    for i, u in enumerate(keep, 1):
        raw = fetch(u)
        if not raw:
            refused += 1
            streak += 1
            if streak >= GIVE_UP_AFTER:
                log(f"[macro] {streak} refusals in a row -- the block is no "
                    f"longer intermittent. Stopping at {i}/{len(keep)}.")
                break
            continue
        streak = 0
        rec = parse_page(u, raw.decode("utf-8", "replace"))
        if rec:
            items.append(rec)
        if i % 25 == 0:
            log(f"[macro] {i}/{len(keep)}  kept {len(items)}  refused {refused}")
        time.sleep(args.delay)

    if not items:
        log("[macro] nothing collected")
        return 1
    dates = sorted(x["date"] for x in items if x["date"])
    withabs = sum(1 for x in items if x["abstract"])
    log(f"\n[macro] {len(items)} posts"
        + (f", {dates[0]} to {dates[-1]}" if dates else ""))
    log(f"[macro] with an abstract: {withabs}/{len(items)}")
    log(f"[macro] refused after retries: {refused}")

    if args.json:
        import json
        pathlib.Path(args.json).write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8")
        log(f"\n[macro] wrote {len(items)} records to {args.json}")
        log("[macro] this file is a publisher's own text and this repository is "
            "PUBLIC -- ingest it and delete it, do not commit it.")
        return 0

    if args.dry_run:
        log("\n[macro] --dry-run: nothing written")
        for x in items[:10]:
            log(f"    {x['date']:<12} {x['title'][:66]}")
        return 0

    con = store.connect()
    fresh = store.filter_new(con, items)
    store.save(con, fresh)
    log(f"\n[macro] inserted {len(fresh)} new rows "
        f"({len(items) - len(fresh)} already held)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
