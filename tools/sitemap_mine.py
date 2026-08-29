#!/usr/bin/env python3
"""Propose taxonomy terms from the firms' own research articles.

THE SAME ARGUMENT AS core_expand.py, AGAINST A DIFFERENT CORPUS. The vocabulary
was written top-down, and "trend following" -- the most common phrase across
2,963 practitioner articles and the name of a desk sleeve -- was missing from
it, so route A never searched for it. core_expand mines the sweep's own
results, which can only ever return vocabulary the sweep already reaches. This
mines text the sweep has never seen: 1,227 research articles that AQR, Verdad,
Acadian and Research Affiliates publish and advertise in their sitemaps.

WHY THE SLUGS ARE NOT ENOUGH. A slug is a title, and titles are where this
project has repeatedly been caught short -- only 27.3% of swept papers contain
their own term in the title. Mining the 1,227 slugs alone surfaces `quick
take` and `part ii`; the body text is where `trend following` lives.

SCORING IS LIFT, NOT FREQUENCY, for the reason core_expand records: ranking by
raw count returns "asset pricing" and "stock returns" for every corpus, because
common phrases are common everywhere. Lift asks whether this corpus says a
phrase disproportionately OFTEN compared with the 242,066-paper pool -- which
is what makes it a subject the academy names differently, or does not name.

POLITENESS IS THE CONTRACT. One fetch per article, rate-limited, resumable, and
robots is re-checked per host through the retrying reader (a WAF's 403 makes
RobotFileParser deny everything by rule, which is how Macrosynergy's 653 urls
were once written off wholesale). A url the policy disallows is skipped and
counted, never fetched.

NOTHING IS INGESTED AND NOTHING IS ADDED. Article text is cached under
export/, which is gitignored, because this repository is public and the text
belongs to the firms that wrote it. The output is a reviewable candidate list;
terms enter the taxonomy only after core_tags.py --validate measures them on
S2, because a phrase nobody writes costs a request and returns nothing.

    python tools/sitemap_mine.py --fetch            # cache the articles
    python tools/sitemap_mine.py --fetch --limit 40 # a sample first
    python tools/sitemap_mine.py --mine             # propose terms
"""

import argparse
import collections
import csv
import html
import io
import json
import math
import pathlib
import re
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import textnorm                                              # noqa: E402
from sitemap_harvest import PAUSE, fetch, robots_for         # noqa: E402

OUT = pathlib.Path("export")
URLS = OUT / "sitemap_urls.json"
CACHE = OUT / "_sitemap_html"          # gitignored: third-party article text
DEST = OUT / "_sitemap_terms.json"
TAGS = OUT / "core_tags.csv"
CAND = OUT / "core_candidates.csv"

# Phrases that are structure, not subject. Kept deliberately short -- the lift
# score removes generic finance vocabulary on its own, and a long stop-list
# starts deciding the answer in advance.
STOP = set(
    "the a an of and or in on for to with from by is are as at we this that "
    "its their new evidence using use does do it more than what how why can "
    "be has have not but who when where which some our you your they them "
    "these those about into over under vs via towards toward within across "
    "between during after before through against among each other others "
    "case study approach analysis model models method methods based paper "
    "results result effect effects impact role empirical figure table "
    "read more download pdf share print copyright reserved rights all "
    "please see also may might would could should will shall must been "
    "was were be being had here there where one two three first second".split())


def log(m):
    print(m, flush=True)


# ---------------------------------------------------------------- text

_DROP = re.compile(
    r"<(script|style|nav|header|footer|form|svg|noscript)\b.*?</\1>",
    re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def to_text(raw):
    """HTML bytes -> visible text.

    Regex, not a parser, and deliberately: bs4 is installed here but absent
    from requirements.txt, which is pinned exactly because unattended runs
    break on surprise releases. Term mining does not need correct HTML -- it
    needs the words in reading order, and a dropped <em> costs nothing.
    """
    s = raw.decode("utf-8", "replace")
    s = _DROP.sub(" ", s)
    s = _TAG.sub(" ", s)
    return _WS.sub(" ", html.unescape(s)).strip()


def _grams(text, n_max=4):
    w = [x for x in textnorm.norm(text).split() if x]
    for n in range(2, n_max + 1):
        for i in range(len(w) - n + 1):
            g = w[i:i + n]
            if g[0] in STOP or g[-1] in STOP:
                continue
            if any(x.isdigit() for x in g):
                continue
            yield " ".join(g)


# ---------------------------------------------------------------- fetch

def _slug(url):
    return re.sub(r"[^a-z0-9]+", "_",
                  urllib.parse.urlparse(url).path.strip("/").lower())[:120]


def cmd_fetch(args):
    if not URLS.exists():
        raise SystemExit(f"[mine] {URLS} missing -- run tools/sitemap_harvest.py")
    hosts = json.loads(URLS.read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)

    # Re-check robots per host. The harvest already did, but that was a
    # different run against a policy that can change, and the cost is one
    # request per host against 1,227 article fetches.
    allowed, blocked_hosts = {}, []
    for h in hosts:
        base = h.get("base") or ""
        if not h.get("urls"):
            continue
        if not base:
            base = "{0.scheme}://{0.netloc}".format(
                urllib.parse.urlparse(h["urls"][0]))
        got, why = robots_for(base)
        if not got:
            blocked_hosts.append((h["host"], why))
            log(f"[mine] {h['host']:<22} SKIPPED -- {why}. "
                f"Not knowing the policy is not permission.")
            continue
        allowed[h["host"]] = got[0]
    if blocked_hosts:
        log(f"[mine] {len(blocked_hosts)} host(s) skipped on robots")

    todo = []
    for h in hosts:
        rp = allowed.get(h["host"])
        if rp is None:
            continue
        for u in (h.get("urls") or []):
            todo.append((h["host"], u, rp))

    n_disallow = sum(1 for _h, u, rp in todo if not rp.can_fetch("*", u))
    todo = [(h, u, rp) for h, u, rp in todo if rp.can_fetch("*", u)]
    have = {p.name for p in CACHE.glob("*.txt")}
    todo = [(h, u, rp) for h, u, rp in todo if f"{_slug(u)}.txt" not in have]
    if args.limit:
        todo = todo[:args.limit]
    log(f"[mine] {len(have):,} cached; {n_disallow:,} disallowed by robots; "
        f"{len(todo):,} to fetch at {PAUSE}s apart "
        f"(~{len(todo)*PAUSE/60:.0f} min)")
    if not todo:
        return 0

    ok = 0
    fail = collections.Counter()
    for i, (host, u, _rp) in enumerate(todo, 1):
        raw = fetch(u, tries=3)
        if not raw:
            # A FAILED FETCH IS NOT AN EMPTY ARTICLE. Recording it as one is
            # the defect this repository has fixed fifteen times; it is
            # counted, reported, and left uncached so a re-run retries it.
            fail[host] += 1
        else:
            t = to_text(raw)
            if len(t) < 400:
                fail[host] += 1
                log(f"[mine]   thin ({len(t)} chars), not caching: {u[:76]}")
            else:
                (CACHE / f"{_slug(u)}.txt").write_text(t, encoding="utf-8")
                ok += 1
        if i % 50 == 0:
            log(f"[mine]   {i:,}/{len(todo):,}  ok={ok:,} failed={sum(fail.values()):,}")
        time.sleep(PAUSE)

    log(f"[mine] fetched {ok:,}; {sum(fail.values()):,} failed")
    if fail:
        log(f"[mine] !! failures by host: {dict(fail)} -- re-run to retry them")
    log(f"[mine] cached under {CACHE} (gitignored -- third-party text)")
    return 0


# ---------------------------------------------------------------- mine

def cmd_mine(args):
    files = sorted(CACHE.glob("*.txt"))
    if not files:
        raise SystemExit(
            f"[mine] {CACHE} is empty -- run --fetch first. (An empty corpus "
            f"would 'propose' nothing and look like a clean result.)")
    if not TAGS.exists():
        raise SystemExit(f"[mine] {TAGS} missing -- run tools/core_tags.py")

    tax = {textnorm.norm(r["term"])
           for r in csv.DictReader(io.open(TAGS, encoding="utf-8"))}

    # Corpus side: document frequency, so one article repeating a phrase
    # forty times cannot manufacture a term. Counted PER HOST, because the
    # thing that ruins this corpus is site chrome: every AQR article carries
    # "Fraudulent Schemes Impersonating AQR Capital Management", which lands
    # in 551 documents and appears in no paper title anywhere -- maximum
    # document frequency and maximum lift, and it is a legal banner.
    #
    # Chrome is near-universal within its own host and absent from the others,
    # so a phrase is discounted only for the hosts where it saturates. Verdad
    # writing about private equity in half its articles is a subject; a footer
    # in 99% of them is a template. Hence a high threshold, not a middling one.
    host_of = {}
    for h in json.loads(URLS.read_text(encoding="utf-8")):
        for u in (h.get("urls") or []):
            host_of[f"{_slug(u)}.txt"] = h["host"]

    per_host = collections.defaultdict(collections.Counter)
    n_host = collections.Counter()
    for p in files:
        hn = host_of.get(p.name, "?")
        n_host[hn] += 1
        per_host[hn].update(set(_grams(p.read_text(encoding="utf-8"))))
    n_doc = len(files)
    log(f"[mine] {n_doc:,} articles across {len(n_host)} hosts: "
        f"{dict(n_host.most_common())}")

    df = collections.Counter()
    chrome = collections.Counter()
    for hn, counts in per_host.items():
        cap = args.chrome * n_host[hn]
        for g, c in counts.items():
            if n_host[hn] >= 20 and c > cap:
                chrome[g] += c
            else:
                df[g] += c
    log(f"[mine] {len(df):,} distinct phrases; {len(chrome):,} discounted as "
        f"site chrome (>{args.chrome:.0%} of a host's own articles)")
    if chrome:
        log(f"[mine]   e.g. {', '.join(g for g, _ in chrome.most_common(6))}")

    # Background side: the paper pool's titles. A phrase common in both is
    # finance vocabulary; a phrase common HERE and rare there is what we want.
    bg = collections.Counter()
    n_bg = 0
    if CAND.exists():
        for r in csv.DictReader(io.open(CAND, encoding="utf-8", newline="")):
            t = r.get("title") or ""
            if not t:
                continue
            n_bg += 1
            bg.update(set(_grams(t)))
        log(f"[mine] background: {n_bg:,} paper titles")
    else:
        log(f"[mine] !! {CAND} missing -- scoring on frequency alone, which "
            f"returns generic finance vocabulary. Build the core list for lift.")

    rows = []
    for g, c in df.items():
        if c < args.min_count or g in tax:
            continue
        p_here = c / n_doc
        p_bg = (bg.get(g, 0) + 1) / (n_bg + 1) if n_bg else 1.0
        lift = p_here / p_bg
        if n_bg and lift < args.min_lift:
            continue
        rows.append({"term": g, "articles": c,
                     "share_pct": round(100 * p_here, 2),
                     "papers": bg.get(g, 0), "lift": round(lift, 1)})
    rows.sort(key=lambda r: (-r["lift"], -r["articles"]))

    # A phrase whose longer form scores as well is noise from the shorter one
    # ("momentum crashes" under "crashes"). Keep the longest of each nest.
    keep, dropped = [], 0
    chosen = set()
    for r in rows:
        if any(r["term"] != o and r["term"] in o for o in chosen):
            dropped += 1
            continue
        chosen.add(r["term"])
        keep.append(r)
    log(f"[mine] {len(rows):,} candidates -> {len(keep):,} after folding "
        f"{dropped:,} into a longer phrase")

    keep = keep[:args.top]
    DEST.write_text(json.dumps(keep, indent=1), encoding="utf-8")
    log(f"\n[mine] top {min(args.top, len(keep))} candidates "
        f"(not in the {len(tax)}-term taxonomy):\n")
    log(f"    {'articles':>8} {'papers':>7} {'lift':>7}  term")
    for r in keep[:args.show]:
        log(f"    {r['articles']:>8} {r['papers']:>7} {r['lift']:>7.1f}  {r['term']}")
    log(f"\n[mine] written to {DEST} -- NOTHING added to the taxonomy. "
        f"Validate on S2 before adopting any of these.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="cache the articles")
    ap.add_argument("--mine", action="store_true", help="propose terms")
    ap.add_argument("--limit", type=int, default=0, help="fetch this many")
    ap.add_argument("--min-count", type=int, default=8,
                    help="phrase must appear in this many ARTICLES")
    ap.add_argument("--min-lift", type=float, default=3.0)
    ap.add_argument("--chrome", type=float, default=0.85,
                    help="a phrase in more than this share of ONE host's "
                         "articles is that site's template, not a subject")
    ap.add_argument("--top", type=int, default=300)
    ap.add_argument("--show", type=int, default=60)
    args = ap.parse_args()
    if not (args.fetch or args.mine):
        ap.error("pass --fetch and/or --mine")
    if args.fetch:
        cmd_fetch(args)
    if args.mine:
        return cmd_mine(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
