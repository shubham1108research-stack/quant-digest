#!/usr/bin/env python3
"""Harvest the backtest- and replication-bearing sources into export/.

WHY THESE THREE AND NOT MORE FEEDS. The archive can already find papers; what
it cannot do is tell you whether a paper's result SURVIVED. tools/artifacts.py
has fields for exactly that -- thesis.evidence in {theoretical, in_sample,
out_of_sample, live}, factors[].reported, factors[].costs, repro.code -- and
fills every one of them with an LLM's opinion about an abstract, unchecked.
That is the same shape as the sleeve classifier that scored F1 0.19 where label
propagation scored 0.86.

These three sources carry EXTERNALLY VERIFIED values for those same fields:

  signaldoc     Chen-Zimmermann Open Source Asset Pricing. ~330 cross-sectional
                predictors, each with the source paper AND two graded columns:
                `Predictability in OP` (did the original result hold) and
                `Signal Rep Quality` (how well it replicates). One CSV, no key.

  pwb           Papers With Backtest. 3,803 papers enumerated from one GitHub
                file, each joining to a strategy page that carries the source
                paper's SSRN id and a real metrics block -- backtestPeriod,
                sharpeRatio, annualReturn, annualVolatility, maxDrawdown -- plus
                a "Publication" annotation. That annotation is the valuable
                part: a backtest spanning 1990-2026 on a paper published in 2011
                makes the post-2011 segment a genuine out-of-sample test, which
                is the McLean-Pontiff question computed per paper.

  quantseeker   A practitioner's weekly research recaps. Measured: ~9 papers per
                post, hand-picked. This is the direct fix for SSRN recall, which
                is otherwise capped near 2.5% because Crossref only returns what
                18 hand-written queries ask for.

EVERY ROUTE JOINS ON A UID WE ALREADY USE. An SSRN abstract_id becomes
doi:10.2139/ssrn.<id>, which is exactly store.make_uid's third branch, so these
land on top of existing rows instead of creating parallel ones.

NOTHING IS INGESTED. Output is export/ only. The archive already carries what
unreviewed sweeps produce -- a Zenodo entry titled "LAB #958 NEUTRAL: VIDEO
SCOUT" that then got scored -- and finding is cheap while deciding is not.

    python tools/core_sources.py signaldoc
    python tools/core_sources.py pwb --limit 50
    python tools/core_sources.py pwb            # ~3,800 pages, resumable
    python tools/core_sources.py quantseeker
"""

import argparse
import csv
import urllib.parse
import os
import collections
import io
import json
import pathlib
import re
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from progress import Progress                              # noqa: E402

OUT = pathlib.Path("export")
UA = "quant-digest/1.0 (research aggregator; upadhyays1108@gmail.com)"

SIGNALDOC = ("https://raw.githubusercontent.com/OpenSourceAP/CrossSection/"
             "master/SignalDoc.csv")
PWB_META = ("https://raw.githubusercontent.com/paperswithbacktest/"
            "awesome-systematic-trading/main/scripts/paper_meta.json")
PWB_PAGE = "https://paperswithbacktest.com/strategies/{slug}"
QS_ARCHIVE = "https://www.quantseeker.com/api/v1/archive"

PAUSE = 1.0                 # per request against a single host
CHECKPOINT = 100            # pwb is ~3,800 pages; do not lose a long run


def log(m):
    print(m, flush=True)


def _get(url, tries=3, timeout=40):
    """Bytes, or None. Retries because a single refusal is not a verdict --
    macrosynergy.com answers on the third attempt and a one-shot probe would
    have written it off."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:                                  # noqa: BLE001
            if attempt == tries - 1:
                return None
            time.sleep(PAUSE * (2 ** attempt))
    return None


# --------------------------------------------------------------- signaldoc
def cmd_signaldoc(args):
    """Chen-Zimmermann predictors: source paper + replication grade."""
    raw = _get(SIGNALDOC)
    if not raw:
        log("[signaldoc] could not fetch SignalDoc.csv")
        return 1
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig", "replace"))))
    log(f"[signaldoc] {len(rows)} rows")

    out = []
    for r in rows:
        # The column names carry spaces and a trailing dot in the source file;
        # read them exactly rather than normalising, so a rename upstream is a
        # visible KeyError-shaped gap instead of a silently empty column.
        out.append({
            "acronym": (r.get("Acronym") or "").strip(),
            "authors": (r.get("Authors") or "").strip(),
            "year": (r.get("Year") or "").strip(),
            "journal": (r.get("Journal") or "").strip(),
            "title_desc": (r.get("LongDescription") or "").strip(),
            "kind": (r.get("Cat.Signal") or "").strip(),
            "predictability": (r.get("Predictability in OP") or "").strip(),
            "replication": (r.get("Signal Rep Quality") or "").strip(),
            "economic_cat": (r.get("Cat.Economic") or "").strip(),
            "sample_start": (r.get("SampleStartYear") or "").strip(),
            "sample_end": (r.get("SampleEndYear") or "").strip(),
            "evidence": (r.get("Evidence Summary") or "").strip()[:300],
            "found_by": "signaldoc",
        })
    out = [r for r in out if r["acronym"]]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "core_signaldoc.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    with io.open(OUT / "core_signaldoc.csv", "w", newline="",
                 encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    import collections                                     # noqa: PLC0415
    rep = collections.Counter(r["replication"] for r in out)
    pred = collections.Counter(r["predictability"] for r in out)
    log(f"[signaldoc] replication quality: {dict(rep.most_common())}")
    log(f"[signaldoc] predictability:      {dict(pred.most_common())}")
    log(f"[signaldoc] {len(out)} predictors -> {OUT}/core_signaldoc.csv")
    log("[signaldoc] candidates only -- nothing ingested")
    return 0


# --------------------------------------------------------------------- pwb
_SSRN = re.compile(r"abstract[_-]?id=(\d+)", re.I)
_METRICS = re.compile(r'"metrics"\s*:\s*\{([^}]{0,400})\}')
_ANNOT = re.compile(r'"annotation"\s*:\s*\{([^}]{0,200})\}')


def _num(blob, key):
    m = re.search(rf'"{key}"\s*:\s*(-?[\d.eE+]+)', blob)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_strategy(html):
    """(ssrn_id, metrics dict) from one strategy page."""
    # The page ships its chart data as escaped JSON inside a script tag, so the
    # metrics block is read from the raw text rather than parsed as a document.
    h = html.replace('\\"', '"')
    ssrn = _SSRN.search(h)
    met = {}
    m = _METRICS.search(h)
    if m:
        blob = m.group(1)
        per = re.search(r'"backtestPeriod"\s*:\s*"([^"]{0,32})"', blob)
        met = {
            "backtest_period": per.group(1) if per else "",
            "sharpe": _num(blob, "sharpeRatio"),
            "annual_return": _num(blob, "annualReturn"),
            "annual_vol": _num(blob, "annualVolatility"),
            "max_drawdown": _num(blob, "maxDrawdown"),
        }
    a = _ANNOT.search(h)
    if a:
        d = re.search(r'"date"\s*:\s*"([^"]{0,40})"', a.group(1))
        if d:
            met["publication_date"] = d.group(1)
    return (ssrn.group(1) if ssrn else None), met


def cmd_pwb(args):
    """Papers With Backtest: enumerate from GitHub, fetch each strategy page."""
    raw = _get(PWB_META, timeout=60)
    if not raw:
        log("[pwb] could not fetch paper_meta.json")
        return 1
    meta = json.loads(raw.decode("utf-8", "replace"))
    papers = {k: v for k, v in meta.items()
              if not k.startswith("__") and isinstance(v, dict)}
    log(f"[pwb] {len(papers):,} papers enumerated from paper_meta.json")

    dest = OUT / "core_pwb.json"
    have = {}
    if dest.exists() and not args.restart:
        try:
            have = {r["slug"]: r for r in json.loads(dest.read_text("utf-8"))}
            log(f"[pwb] resuming: {len(have):,} already fetched")
        except Exception:                                  # noqa: BLE001
            have = {}

    todo = [(k, v) for k, v in papers.items()
            if k.replace("_", "-") not in have]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        log("[pwb] nothing to do")
        return 0
    log(f"[pwb] fetching {len(todo):,} strategy pages at {PAUSE}s each "
        f"(~{len(todo) * PAUSE / 60:.0f} min)")

    OUT.mkdir(parents=True, exist_ok=True)
    rows = dict(have)
    prog = Progress(len(todo), "pwb", every_s=30)
    got = miss = 0

    def flush():
        dest.write_text(json.dumps(list(rows.values()), indent=1,
                                   ensure_ascii=False), encoding="utf-8")

    for i, (key, rec) in enumerate(todo, 1):
        slug = key.replace("_", "-")
        body = _get(PWB_PAGE.format(slug=slug), tries=2, timeout=40)
        if body:
            ssrn, met = _parse_strategy(body.decode("utf-8", "replace"))
            row = {"slug": slug, "title": rec.get("title") or "",
                   "markets": rec.get("markets") or "",
                   "ssrn_id": ssrn,
                   # the join that makes this useful: an SSRN id IS our uid
                   "uid": f"doi:10.2139/ssrn.{ssrn}" if ssrn else None,
                   "found_by": "pwb"}
            row.update(met)
            rows[slug] = row
            got += 1 if ssrn else 0
            miss += 0 if ssrn else 1
        else:
            miss += 1
        prog.tick()
        if i % CHECKPOINT == 0:
            flush()
        time.sleep(PAUSE)
    prog.done()
    flush()

    withid = [r for r in rows.values() if r.get("ssrn_id")]
    withsharpe = [r for r in withid if r.get("sharpe") is not None]
    log(f"[pwb] {len(rows):,} pages held; {len(withid):,} carry an SSRN id "
        f"({100.0*len(withid)/max(len(rows),1):.0f}%); "
        f"{len(withsharpe):,} carry a Sharpe")
    log(f"[pwb] written to {dest} -- candidates only, nothing ingested")
    return 0


# ------------------------------------------------------------- quantseeker
_IDS = (
    ("ssrn", re.compile(r"abstract[_-]?id=(\d+)", re.I)),
    ("arxiv", re.compile(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})", re.I)),
    ("doi", re.compile(r"(10\.\d{4,9}/[A-Za-z0-9._;()/-]{3,60})")),
)


def cmd_quantseeker(args):
    """Mine paper identifiers out of the weekly research recaps."""
    OUT.mkdir(parents=True, exist_ok=True)
    posts, offset = [], 0
    while True:
        body = _get(f"{QS_ARCHIVE}?sort=new&limit=12&offset={offset}")
        if not body:
            break
        try:
            page = json.loads(body.decode("utf-8", "replace"))
        except Exception:                                  # noqa: BLE001
            break
        if not page:
            break
        posts += page
        offset += len(page)
        log(f"[quantseeker] {len(posts)} posts")
        if args.limit and len(posts) >= args.limit:
            break
        time.sleep(PAUSE)

    # TWO STAGES, because the archive listing carries body_html as NULL. It is
    # in the key list, which is what made a one-stage harvest look reasonable --
    # and it returned 221 posts and zero identifiers. The body lives on the
    # per-post endpoint: measured 15,148 chars and 7 SSRN ids for one recap.
    log(f"[quantseeker] fetching {len(posts)} post bodies at {PAUSE}s each")
    prog = Progress(len(posts), "qs-bodies", every_s=30)
    for p in posts:
        slug = p.get("slug")
        if slug:
            b = _get(f"https://www.quantseeker.com/api/v1/posts/{slug}",
                     tries=2)
            if b:
                try:
                    p["body_html"] = (json.loads(
                        b.decode("utf-8", "replace")).get("body_html") or "")
                except Exception:                          # noqa: BLE001
                    pass
        prog.tick()
        time.sleep(PAUSE)
    prog.done()

    out = []
    for p in posts:
        html = (p.get("body_html") or "") + " " + (p.get("description") or "")
        found = {}
        for kind, rx in _IDS:
            for v in dict.fromkeys(rx.findall(html)):
                found.setdefault(kind, []).append(v)
        for kind, vals in found.items():
            for v in vals:
                uid = (f"doi:10.2139/ssrn.{v}" if kind == "ssrn" else
                       f"arxiv:{v}" if kind == "arxiv" else f"doi:{v.lower()}")
                out.append({"uid": uid, "kind": kind, "value": v,
                            "post": (p.get("title") or "")[:110],
                            "post_date": (p.get("post_date") or "")[:10],
                            "post_url": p.get("canonical_url") or "",
                            "found_by": "quantseeker"})
    # one row per identifier per post; dedupe on uid, keeping the earliest post
    seen, uniq = set(), []
    for r in sorted(out, key=lambda x: x["post_date"]):
        if r["uid"] in seen:
            continue
        seen.add(r["uid"])
        uniq.append(r)

    (OUT / "core_quantseeker.json").write_text(
        json.dumps(uniq, indent=1, ensure_ascii=False), encoding="utf-8")
    import collections                                     # noqa: PLC0415
    kinds = collections.Counter(r["kind"] for r in uniq)
    log(f"[quantseeker] {len(posts)} posts -> {len(out)} mentions, "
        f"{len(uniq)} unique papers {dict(kinds)}")
    log(f"[quantseeker] written to {OUT}/core_quantseeker.json "
        f"-- candidates only")
    return 0


# ------------------------------------------------------------------- sweep
S2_BULK = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
TAGS_CSV = OUT / "core_tags.csv"


def cmd_sweep(args):
    """Route A: search the validated 299-term taxonomy, all years.

    THE ROUTE THAT WAS NEVER RUN, and its absence is why `carry` held one paper
    of two thousand. Nothing else in the pipeline looks for carry research by
    name: Papers With Backtest tags by asset class and has no carry concept,
    NBER has no carry topic (carry is a practitioner sleeve, not an academic
    one), and the archive's own labels reach 111 papers of 11,764. Measured on
    the bulk endpoint, `"carry trade"` alone returns 951 papers.

    /paper/search/bulk returns up to 1,000 rows in ONE request and only issues
    a continuation token beyond that -- so s2_harvest's `[:per_term]` slice,
    not the paging, was what capped the old discover at ten rows per term.
    """
    if not TAGS_CSV.exists():
        log(f"[sweep] {TAGS_CSV} missing -- run tools/core_tags.py first")
        return 1
    terms = []
    for r in csv.DictReader(io.open(TAGS_CSV, encoding="utf-8")):
        tot = (r.get("total") or "").strip()
        if tot.isdigit() and int(tot) == 0:
            continue                       # validated as returning nothing
        terms.append((r["family"], r["term"]))
    if args.limit:
        terms = terms[:args.limit]
    key = os.environ.get("S2_API_KEY", "").strip()
    pause = 1.1 if key else 3.2
    log(f"[sweep] {len(terms)} terms, {'key set' if key else 'NO KEY -- slow'}, "
        f"~{len(terms) * pause / 60:.0f} min")

    hdr = {"User-Agent": "quant-digest/1.0"}
    if key:
        hdr["x-api-key"] = key
    seen, out = set(), []
    prog = Progress(len(terms), "sweep", every_s=30)
    for family, term in terms:
        q = urllib.parse.quote(f'"{term}"' if " " in term else term)
        url = (f"{S2_BULK}?query={q}&fields=title,year,externalIds,"
               f"citationCount,abstract&fieldsOfStudy=Economics,Business"
               f"&sort=citationCount:desc")
        token, pages = None, 0
        while pages < args.max_pages:
            u = url + (f"&token={token}" if token else "")
            body = _get(u, tries=3, timeout=60)
            if not body:
                break
            try:
                d = json.loads(body.decode("utf-8", "replace"))
            except Exception:                              # noqa: BLE001
                break
            for x in (d.get("data") or []):
                ext = x.get("externalIds") or {}
                doi = (ext.get("DOI") or "").lower()
                arx = ext.get("ArXiv")
                uid = (f"doi:{doi}" if doi else
                       f"arxiv:{arx}" if arx else None)
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                out.append({
                    "uid": uid, "title": x.get("title") or "",
                    "year": x.get("year"),
                    "cites": x.get("citationCount") or 0,
                    "family": family, "tag": term,
                    "has_abstract": bool((x.get("abstract") or "").strip()),
                    "found_by": "sweep",
                })
            token = d.get("token")
            pages += 1
            if not token:
                break
            time.sleep(pause)
        prog.tick()
        time.sleep(pause)
    prog.done()

    out.sort(key=lambda r: -(r["cites"] or 0))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "core_sweep.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    fam = collections.Counter(r["family"] for r in out)
    log(f"[sweep] {len(out):,} unique papers across {len(terms)} terms")
    for k, v in sorted(fam.items()):
        log(f"[sweep]   {v:>6,}  {k}")
    log(f"[sweep] written to {OUT}/core_sweep.json -- candidates only")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    for name in ("signaldoc", "pwb", "quantseeker", "sweep"):
        p = sub.add_parser(name)
        p.add_argument("--limit", type=int, default=0)
        if name == "sweep":
            p.add_argument("--max-pages", type=int, default=3)
        if name == "pwb":
            p.add_argument("--restart", action="store_true",
                           help="ignore the existing file and refetch all")
    args = ap.parse_args()
    return {"signaldoc": cmd_signaldoc, "pwb": cmd_pwb,
            "quantseeker": cmd_quantseeker, "sweep": cmd_sweep}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
