#!/usr/bin/env python3
"""One sitemap route for every firm that publishes one.

WHY THIS REPLACES THREE MECHANISMS. The repo reaches firm research three
different ways -- headless Chromium (firms.py), RSS (sources.practitioner) and
a bespoke sitemap reader (backfill_macrosynergy.py) -- and the cheapest of the
three is the one used least. A sitemap is a single XML fetch: no JavaScript, no
browser, no listing-page scraping, and it is ADVERTISED BY THE OPERATOR in
robots.txt, which is an invitation rather than a workaround.

THE MEASURED CASE FOR IT IS AQR. config.FIRM_SITES drives Chromium against
aqr.com for FIRM_MAX_ITEMS = 15 articles. The same site publishes a 914-URL
sitemap that one urllib call retrieves. Fifteen against nine hundred, and the
fifteen cost a browser install in CI.

ROBOTS IS READ THROUGH THE RETRYING FETCH, and this is not a detail.
RobotFileParser.read() uses a plain urlopen, so a WAF's 403 makes it deny
everything by rule -- correct for a site that is really refusing, exactly wrong
for one that answers on the third try. backfill_macrosynergy printed
"robots.txt disallows" for all 653 Macrosynergy URLs when their file disallows
a single survey path. A guard that reports the wrong reason is worse than none,
because the reason is what the next person acts on.

NOT KNOWING THE POLICY IS NOT PERMISSION. If robots.txt cannot be read after
the retries, the host is skipped rather than crawled. SSRN is 403 on
robots.txt itself, which is why SSRN is not in this file and is reached through
Crossref instead.

NOTHING IS INGESTED. Writes export/sitemap_urls.json -- URLS ONLY, for review,
the same contract as the other core_* harvests. It said "urls and titles" for
a while and never captured a title: a sitemap carries locations, not headings,
so a title costs one fetch PER ARTICLE (1,227 of them) rather than one XML
fetch per host. The slug carries most of it -- `2024-in-factors`,
`effects-of-valuation` -- and costs nothing.

    python tools/sitemap_harvest.py                 # every configured host
    python tools/sitemap_harvest.py --host AQR
    python tools/sitemap_harvest.py --list          # what is configured
"""

import argparse
import io
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

OUT = pathlib.Path("export") / "sitemap_urls.json"
UA = "quant-digest/1.0 (research aggregator; upadhyays1108@gmail.com)"
RETRIES = 5   # macrosynergy answers on the third attempt; leave headroom
PAUSE = 1.5

# host -> (base, article-path regex). The regex is what separates an article
# from the listing pages, tag pages and author pages that share the sitemap.
# Measured availability is recorded per host so a future reader does not
# re-probe a site that was already found to have nothing.
# PATTERNS ARE READ OFF THE LIVE SITEMAP, NOT COPIED FROM config.FIRM_SITES.
# The first version reused those regexes and matched ZERO of 914 AQR urls:
# FIRM_SITES says /Insights/Research/... and the sitemap says
# /insights/research/journal-article/... -- different case, and an extra
# category segment. Acadian is /investment-insights/, not /insights/. A
# pattern that silently matches nothing looks exactly like a site with no
# research, which is how a route reports success and delivers zero.
HOSTS = {
    # 914 locs, 720 under /insights. Today this site is driven by headless
    # Chromium for FIRM_MAX_ITEMS = 15 articles.
    "AQR": ("https://www.aqr.com",
            r"^/insights/research/[^/]+/[a-z0-9-]{8,}$"),
    # 471 locs; /archive/ is their research path
    "Verdad": ("https://verdadcap.com", r"^/archive/[a-z0-9-]{8,}$"),
    # 360 locs, 251 under /investment-insights. Not a source today at all.
    "Acadian": ("https://www.acadian-asset.com",
                r"^/investment-insights/[a-z0-9-]{8,}"),
    # 653 locs; already harvested by backfill_macrosynergy, kept here so the
    # route is one implementation rather than two
    "Macrosynergy": ("https://macrosynergy.com", r"^/research/[a-z0-9-]{8,}/$"),
    # 45 locs and only ~10 under /research -- small, and mostly not articles
    "Research Affiliates": ("https://www.rafi.com", r"^/research/[a-z0-9-]{6,}"),
}

# Sitemaps list their own listing pages. /research/blog/ came through the
# Macrosynergy run as a record titled "Research Blog" with no date, and would
# have been archived and scored as a paper -- the same failure shape as an
# SSRN table-of-contents row.
INDEX_HINTS = re.compile(
    r"/(page|category|categories|tag|tags|author|authors|search|archive|"
    r"topics?|series)/|/$|/feed/?$", re.I)


def log(m):
    print(m, flush=True)


def fetch(url, tries=RETRIES):
    """Bytes or None. A single refusal is not a verdict -- macrosynergy.com
    answers on the third attempt and a one-shot probe writes it off."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read()
        except Exception as e:                              # noqa: BLE001
            if attempt == tries - 1:
                log(f"[sitemap]     {type(e).__name__} on {url[:70]}")
                return None
            time.sleep(PAUSE * (2 ** attempt))
    return None


def robots_for(base):
    """(parser, disallow-count) or (None, reason) when the policy is unreadable."""
    raw = fetch(f"{base}/robots.txt")
    if not raw:
        return None, "robots.txt unreadable after retries"
    text = raw.decode("utf-8", "replace")
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    rules = [l.strip() for l in text.splitlines()
             if l.strip().lower().startswith("disallow")]
    declared = [l.split(":", 1)[1].strip() for l in text.splitlines()
                if l.strip().lower().startswith("sitemap:")]
    return (rp, rules, declared), None


def sitemap_urls(base, declared):
    """<loc> values, following a <sitemapindex> exactly one hop."""
    candidates = list(declared) or [f"{base}/sitemap.xml"]
    seen, urls = set(), []
    for sm in candidates[:6]:
        raw = fetch(sm)
        if not raw:
            continue
        body = raw.decode("utf-8", "replace")
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        if "<sitemapindex" in body.lower():
            # one hop, and only into sub-sitemaps of the same host
            for sub in locs[:25]:
                if urllib.parse.urlparse(sub).netloc != urllib.parse.urlparse(base).netloc:
                    continue
                sraw = fetch(sub)
                if sraw:
                    urls += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>",
                                       sraw.decode("utf-8", "replace"))
                time.sleep(PAUSE)
        else:
            urls += locs
        seen.add(sm)
        time.sleep(PAUSE)
    return urls


def harvest(name, base, pattern, log=log):
    log(f"\n[sitemap] {name}  {base}")
    got, err = robots_for(base)
    if err:
        log(f"[sitemap]   SKIPPED -- {err}. Not knowing the policy is not "
            f"permission.")
        return {"host": name, "status": "skipped", "reason": err, "urls": []}
    rp, rules, declared = got
    log(f"[sitemap]   robots.txt: {len(rules)} disallow rule(s); "
        f"{len(declared)} sitemap directive(s)"
        + (f" -- {declared[0][:60]}" if declared else " -- trying /sitemap.xml"))

    urls = sitemap_urls(base, declared)
    if not urls:
        log(f"[sitemap]   no <loc> entries found")
        return {"host": name, "status": "empty", "urls": []}

    rx = re.compile(pattern)
    seen, keep, idx, off, blocked = set(), [], 0, 0, 0
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        p = urllib.parse.urlparse(u)
        if p.netloc != urllib.parse.urlparse(base).netloc:
            off += 1
            continue
        if INDEX_HINTS.search(p.path) and not rx.search(p.path):
            idx += 1
            continue
        if not rx.search(p.path):
            off += 1
            continue
        if not rp.can_fetch(UA, u):
            blocked += 1
            continue
        keep.append(u)
    log(f"[sitemap]   {len(urls):,} locs -> {len(keep):,} articles "
        f"({idx} listing pages, {off} non-matching, {blocked} robots-blocked)")
    return {"host": name, "status": "ok", "n_locs": len(urls),
            "urls": sorted(keep)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="", help="one host from HOSTS")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for n, (b, p) in HOSTS.items():
            log(f"  {n:<22} {b:<38} {p}")
        return 0
    names = [args.host] if args.host else list(HOSTS)
    bad = [n for n in names if n not in HOSTS]
    if bad:
        log(f"[sitemap] unknown host(s): {', '.join(bad)}")
        return 2

    # --host MERGES, it does not replace the file. Writing `out` straight over
    # OUT means a single-host run discards every other host: probing one site
    # turned 1,227 harvested urls into a lone 0-url record, and the only copy
    # was the one just overwritten. A partial run must never be able to
    # destroy a complete one -- the failure this repository keeps having, in
    # its narrowest form.
    prior = []
    if OUT.exists():
        try:
            prior = json.loads(OUT.read_text(encoding="utf-8")) or []
        except Exception as e:                              # noqa: BLE001
            raise SystemExit(
                f"[sitemap] {OUT} exists but will not parse ({type(e).__name__}). "
                f"REFUSING to overwrite it -- move it aside to start fresh.")

    fresh = {}
    for n in names:
        base, pat = HOSTS[n]
        fresh[n] = harvest(n, base, pat)

    merged, seen = [], set()
    for rec in prior:
        h = rec.get("host")
        seen.add(h)
        merged.append(fresh.get(h, rec))
    for n in names:
        if n not in seen:
            merged.append(fresh[n])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(merged, indent=1), encoding="utf-8")
    total = sum(len(o.get("urls") or []) for o in merged)
    kept = [o for o in merged if o.get("host") not in fresh]
    log(f"\n[sitemap] {total:,} article urls across {len(merged)} hosts "
        f"-> {OUT}  (nothing ingested)")
    if kept:
        log(f"[sitemap] {len(kept)} host(s) carried over untouched from the "
            f"previous run: {', '.join(o['host'] for o in kept)}")
    for o in merged:
        mark = " " if o.get("host") in fresh else "."
        log(f"[sitemap] {mark} {o['host']:<22} {str(o.get('status')):<8} "
            f"{len(o.get('urls') or []):>5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
