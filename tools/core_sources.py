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


S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"


def _s2_titles(records, log=log):
    """Fill in each record's REAL paper title from Semantic Scholar.

    WHY THIS IS NOT COSMETIC. A harvest that carries the post title instead of
    the paper title is not merely untidy -- build_core deduplicates on the
    normalised title, so 1,665 QuantSeeker papers sharing 10 post titles
    ("Weekly Recap" 1,000+ times) collapsed into each other and 1,406 of them
    disappeared. Distinct papers merging under a shared non-title is the same
    defect as one paper splitting across four identifiers, pointed the other
    way, and it is invisible in the harvest itself.

    S2 takes SSRN and arXiv ids directly -- DOI:10.2139/ssrn.<id> and
    ARXIV:<id> -- 500 per POST, so 1,665 papers cost four requests. Measured
    coverage on a mixed sample: 15 of 18, SSRN included.
    """
    import requests                                        # noqa: PLC0415

    def s2id(r):
        if r.get("kind") == "ssrn":
            return f"DOI:10.2139/ssrn.{r['value']}"
        if r.get("kind") == "arxiv":
            return f"ARXIV:{r['value']}"
        return f"DOI:{r['value']}"

    key = os.environ.get("S2_API_KEY", "").strip()
    hdr = {"User-Agent": UA}
    if key:
        hdr["x-api-key"] = key
    got = 0
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        ids = [s2id(r) for r in chunk]
        body = None
        for attempt in range(4):
            try:
                rr = requests.post(
                    S2_BATCH, headers=hdr,
                    params={"fields": "title,year,citationCount"},
                    json={"ids": ids}, timeout=90)
            except Exception as e:                          # noqa: BLE001
                log(f"[quantseeker]   title batch error: {type(e).__name__}")
                break
            if rr.status_code == 429:
                time.sleep(4 * (attempt + 1))
                continue
            if not rr.ok:
                log(f"[quantseeker]   title batch HTTP {rr.status_code}: "
                    f"{rr.text[:120]}")
                break
            body = rr.json()
            break
        if not body:
            continue
        for rec, hit in zip(chunk, body):
            if hit and hit.get("title"):
                rec["title"] = hit["title"]
                rec["year"] = hit.get("year")
                rec["cites"] = hit.get("citationCount")
                got += 1
        time.sleep(PAUSE)
    log(f"[quantseeker] resolved {got:,} of {len(records):,} real titles "
        f"via Semantic Scholar ({(len(records)+499)//500} requests)")
    return got


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
    fetch_failed = False
    while True:
        body = _get(f"{QS_ARCHIVE}?sort=new&limit=12&offset={offset}")
        if not body:
            # THE SAME `if not body: break` THAT COST THE SWEEP 42 TERMS,
            # forty lines below this one and left unfixed. A block or a
            # rate-limit at offset 0 gave `posts == []`, the run logged
            # "0 posts -> 0 papers", and the empty result was WRITTEN OVER
            # core_quantseeker.json. Every layer read it as "the newsletter
            # mentioned no papers".
            if offset == 0:
                fetch_failed = True
            break
        try:
            page = json.loads(body.decode("utf-8", "replace"))
        except Exception as e:                             # noqa: BLE001
            log(f"[quantseeker] !! archive page at offset {offset} did not "
                f"decode ({type(e).__name__}) -- a challenge page or an error "
                f"body, not an empty archive")
            if offset == 0:
                fetch_failed = True
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

    _s2_titles(uniq)

    if fetch_failed and not uniq:
        raise SystemExit(
            "[quantseeker] the archive returned nothing on the FIRST request. "
            "That is a fetch failure, not an empty newsletter -- refusing to "
            "overwrite core_quantseeker.json with an empty list.")

    (OUT / "core_quantseeker.json").write_text(
        json.dumps(uniq, indent=1, ensure_ascii=False), encoding="utf-8")
    import collections                                     # noqa: PLC0415
    kinds = collections.Counter(r["kind"] for r in uniq)
    log(f"[quantseeker] {len(posts)} posts -> {len(out)} mentions, "
        f"{len(uniq)} unique papers {dict(kinds)}")
    log(f"[quantseeker] written to {OUT}/core_quantseeker.json "
        f"-- candidates only")
    return 0


# ------------------------------------------------------------------ roster
# The roster is an APPROVED artifact, so it is committed under data/ rather
# than regenerated. export/ is gitignored, so a roster living only there does
# not exist in CI at all -- and re-running core_roster.py to recreate it would
# re-resolve every name against S2, quietly replacing the list that was
# reviewed and approved with whatever /author/search returns today. Same
# reasoning as data/core_judgments.json: our work product is committed, the
# third-party harvest is not.
ROSTER = pathlib.Path("data") / "core_roster.csv"
if not ROSTER.exists():
    ROSTER = OUT / "core_roster.csv"
S2_AUTHOR_PAPERS = "https://api.semanticscholar.org/graph/v1/author/{aid}/papers"


def cmd_roster(args):
    """The approved practitioner roster's publication history (route D).

    THE ROSTER WAS BUILT AND THEN NEVER READ. `s2_harvest.py cmd_authors`
    seeds from `config.WATCHLIST_SEED` and resolves ids through papers the
    archive already holds, so it reached 30 people and 1,218 papers. The
    roster is 175 people, every one already carrying a resolved S2 author id
    -- and it is the reason Roncalli on risk parity, Ang on factor investing
    and Campbell-Viceira on strategic allocation appear only as uncorroborated
    single-route rows, if at all.

    NEEDS_REVIEW IS A HARD GATE, NOT A HINT. core_roster.py states the
    contract in its own docstring: nothing downstream may consume `s2_id`
    while the flag is set, because /author/search on "Bryan Kelly" returns an
    orthopaedic surgeon with h=78 rather than the economist with h=18, and 75
    of the 175 have a second plausible profile. Importing the wrong person's
    bibliography would poison every author-anchored feature with no error
    anywhere, so the flagged rows are SKIPPED and counted, never guessed at.
    """
    import requests                                        # noqa: PLC0415
    if not ROSTER.exists():
        log(f"[roster] {ROSTER} missing -- run tools/core_roster.py first")
        return 1
    people = list(csv.DictReader(io.open(ROSTER, encoding="utf-8")))
    usable, flagged, unresolved = [], 0, 0
    for p in people:
        if p.get("needs_review") == "1":
            flagged += 1
        elif p.get("s2_status") == "ok" and (p.get("s2_id") or "").strip():
            usable.append(p)
        else:
            unresolved += 1
    log(f"[roster] {len(people)} people: {len(usable)} harvestable, "
        f"{flagged} skipped as needs_review (ambiguous S2 profile), "
        f"{unresolved} unresolved")
    if args.limit:
        usable = usable[:args.limit]
        log(f"[roster] --limit {args.limit}: harvesting {len(usable)}")

    key = os.environ.get("S2_API_KEY", "").strip()
    hdr = {"User-Agent": UA}
    if key:
        hdr["x-api-key"] = key
    pause = 1.1 if key else 3.2

    # THE DEDUP IS PER AUTHOR, NOT GLOBAL, and that distinction is the whole
    # point of this route. `seen` used to live out here, shared across every
    # person on the roster, so a paper harvested for one author was skipped
    # for all the others: Fama-French 1993 landed under Eugene Fama and
    # Kenneth French -- also on the roster -- got no credit for his own paper.
    # Every per-author count downstream was a floor rather than a total, and
    # nothing said so.
    #
    # A paper still cannot appear twice for the SAME author (pagination can
    # repeat one), which is what a per-author set gives. The output is now one
    # row per (author, paper), so co-authorship between roster members is
    # visible instead of silently collapsed. build_core is unaffected: it
    # calls add(uid, "authors") per row and a repeated uid is a dict
    # setdefault, not a duplicate candidate.
    out = []
    failed = []          # people whose fetch DIED rather than came back empty
    prog = Progress(len(usable), "roster", every_s=30)
    for p in usable:
        aid = p["s2_id"].strip()
        seen = set()
        offset, kept = 0, 0
        died = False
        while offset < 1000:                 # nobody indexed here exceeds it
            body = None
            for attempt in range(6):
                try:
                    rr = requests.get(
                        S2_AUTHOR_PAPERS.format(aid=aid), headers=hdr,
                        params={"fields": "title,year,citationCount,"
                                          "externalIds",
                                "limit": 100, "offset": offset}, timeout=60)
                except Exception as e:                     # noqa: BLE001
                    log(f"[roster]   {p['name']}: {type(e).__name__}")
                    died = True
                    break
                if rr.status_code == 429:
                    time.sleep(4 * (attempt + 1))
                    continue
                if not rr.ok:
                    log(f"[roster]   {p['name']}: HTTP {rr.status_code} "
                        f"{rr.text[:90]}")
                    died = True
                    break
                body = rr.json()
                break
            else:
                # RETRIES EXHAUSTED WITHOUT A break -- every attempt was a 429.
                # This used to fall through to `if not body: break` and say
                # NOTHING, so a rate-limited author was indistinguishable from
                # one who has published nothing. It cost Stephen A. Ross (94
                # papers), Torben Andersen (63) and Greg Duffee (23) their
                # entire bibliographies in a run that exited 0 and looked
                # clean. Retried at the end rather than written off here.
                log(f"[roster]   !! {p['name']}: rate-limited out after 6 "
                    f"attempts at offset {offset} -- NOT an empty profile")
                died = True
            if not body:
                break
            data = body.get("data") or []
            for w in data:
                ext = w.get("externalIds") or {}
                doi = (ext.get("DOI") or "").lower()
                arx = ext.get("ArXiv") or ""
                # A DOI is preferred, but an arXiv id is a real identifier and
                # build_core makes uids from both. Dropping DOI-less papers
                # would silently lose the working-paper end of the literature,
                # which for this roster is much of the interesting work.
                if not doi and not arx:
                    continue
                k = f"doi:{doi}" if doi else f"arxiv:{arx}"
                if k in seen:
                    continue
                seen.add(k)
                out.append({"doi": doi, "arxiv": arx,
                            "title": w.get("title") or "",
                            "year": w.get("year") or "",
                            "cites": w.get("citationCount") or 0,
                            "author": p["name"], "s2_author": aid,
                            "sleeve": p.get("sleeve") or ""})
                kept += 1
            if len(data) < 100:
                break
            offset += 100
            time.sleep(pause)
        if died and kept == 0:
            failed.append(p)
        prog.tick()
        time.sleep(pause)
    prog.done()

    # ONE RETRY PASS, same pattern as cmd_sweep. An author lost to a transient
    # 429 is a silently missing bibliography, and this route exists precisely
    # because those were missing before.
    if failed:
        log(f"[roster] {len(failed)} author(s) returned NOTHING after a failed "
            f"fetch -- retrying: {', '.join(x['name'] for x in failed)}")
        for p in failed:
            aid = p["s2_id"].strip()
            seen, offset, got = set(), 0, 0
            while offset < 1000:
                try:
                    rr = requests.get(
                        S2_AUTHOR_PAPERS.format(aid=aid), headers=hdr,
                        params={"fields": "title,year,citationCount,"
                                          "externalIds",
                                "limit": 100, "offset": offset}, timeout=60)
                except Exception:                          # noqa: BLE001
                    break
                if rr.status_code == 429:
                    time.sleep(10)
                    continue
                if not rr.ok:
                    break
                data = (rr.json() or {}).get("data") or []
                for w in data:
                    ext = w.get("externalIds") or {}
                    doi = (ext.get("DOI") or "").lower()
                    arx = ext.get("ArXiv") or ""
                    if not doi and not arx:
                        continue
                    k = f"doi:{doi}" if doi else f"arxiv:{arx}"
                    if k in seen:
                        continue
                    seen.add(k)
                    out.append({"doi": doi, "arxiv": arx,
                                "title": w.get("title") or "",
                                "year": w.get("year") or "",
                                "cites": w.get("citationCount") or 0,
                                "author": p["name"], "s2_author": aid,
                                "sleeve": p.get("sleeve") or ""})
                    got += 1
                if len(data) < 100:
                    break
                offset += 100
                time.sleep(2.0)
            log(f"[roster]   retry {p['name']}: +{got}")
            time.sleep(2.0)

    (OUT / "core_roster_papers.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    by_sleeve = collections.Counter(r["sleeve"] for r in out)
    n_distinct = len({(f"doi:{r['doi']}" if r["doi"] else f"arxiv:{r['arxiv']}")
                      for r in out})
    log(f"[roster] {len(out):,} author-paper rows from {len(usable)} people "
        f"({n_distinct:,} distinct papers; the gap is co-authorship BETWEEN "
        f"roster members, which is signal, not duplication)")
    log(f"[roster]   {sum(1 for r in out if r['doi']):,} with a DOI, "
        f"{sum(1 for r in out if not r['doi']):,} arXiv-only")
    log(f"[roster] by sleeve: {dict(by_sleeve.most_common())}")
    log(f"[roster] written to {OUT}/core_roster_papers.json -- candidates only")
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
    # A TERM THAT FAILS ITS REQUEST IS NOT A TERM WITH NO PAPERS. `_get`
    # returns None after three attempts -- a 429, a timeout, a transient 5xx --
    # and the loop below used to `break` on that, so the term contributed zero
    # and said nothing. Measured on the 311-term run: 42 terms returned zero,
    # including "momentum", "gold", "business cycle" and "VIX", which between
    # them are tens of thousands of papers. The sweep still printed healthy
    # per-family totals and the job exited green, and the missing 20,606 papers
    # were read as a real change in the corpus.
    failed, empty = [], []
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
                if pages == 0:
                    failed.append(term)      # nothing at all came back
                break
            try:
                d = json.loads(body.decode("utf-8", "replace"))
            except Exception:                              # noqa: BLE001
                # A 200 CARRYING AN HTML CHALLENGE PAGE IS A FAILURE TOO. The
                # `if not body` branch above records the term for retry; this
                # one used to `break` silently, so a WAF interstitial was
                # invisible to both the retry and the "!! N terms STILL
                # returned nothing" warning -- the same 42-terms-return-zero
                # failure through a different door.
                if pages == 0:
                    failed.append(term)
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
                    # KEEP THE ABSTRACT, DO NOT JUST COUNT IT. The request
                    # already asks for it (fields=...,abstract) and the old
                    # code stored a boolean and dropped the text -- 124,228 of
                    # 264,320 abstracts fetched and deleted. Labelling matches
                    # terms against the TITLE while S2 searched title AND
                    # abstract, so 72.7% of swept papers do not contain their
                    # own term in the title. Storing what we already paid for
                    # closes that asymmetry at zero extra requests.
                    "abstract": (x.get("abstract") or "").strip(),
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
    # Report the failures LOUDLY, and retry them once before giving up: a
    # transient 429 on a 30,000-paper term costs more than the whole retry.
    if failed:
        log(f"[sweep] {len(failed)} terms returned NOTHING on the first pass "
            f"-- retrying them once: {', '.join(failed[:8])}"
            + (" ..." if len(failed) > 8 else ""))
        still = []
        for term in failed:
            fam = next((f for f, t in terms if t == term), "")
            q = urllib.parse.quote(f'"{term}"' if " " in term else term)
            body = _get(f"{S2_BULK}?query={q}&fields=title,year,externalIds,"
                        f"citationCount,abstract&fieldsOfStudy=Economics,Business"
                        f"&sort=citationCount:desc", tries=5, timeout=90)
            if not body:
                still.append(term)
                continue
            try:
                d = json.loads(body.decode("utf-8", "replace"))
            except Exception:                              # noqa: BLE001
                still.append(term)
                continue
            n0 = len(out)
            for x in (d.get("data") or []):
                ext = x.get("externalIds") or {}
                doi = (ext.get("DOI") or "").lower()
                arx = ext.get("ArXiv")
                uid = (f"doi:{doi}" if doi else
                       f"arxiv:{arx}" if arx else None)
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                out.append({"uid": uid, "title": x.get("title") or "",
                            "year": x.get("year"),
                            "cites": x.get("citationCount") or 0,
                            "family": fam, "tag": term,
                            "abstract": (x.get("abstract") or "").strip(),
                            "has_abstract": bool(x.get("abstract")),
                            "found_by": "sweep"})
            log(f"[sweep]   retry {term!r}: +{len(out)-n0}")
            time.sleep(pause)
        if still:
            log(f"[sweep] !! {len(still)} terms STILL returned nothing after "
                f"a retry -- these are lost from this run, not empty: "
                f"{', '.join(still)}")
        else:
            log(f"[sweep] all retried terms recovered")

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
    for name in ("signaldoc", "pwb", "quantseeker", "sweep", "roster"):
        p = sub.add_parser(name)
        p.add_argument("--limit", type=int, default=0)
        if name == "sweep":
            p.add_argument("--max-pages", type=int, default=3)
        if name == "pwb":
            p.add_argument("--restart", action="store_true",
                           help="ignore the existing file and refetch all")
    args = ap.parse_args()
    return {"signaldoc": cmd_signaldoc, "pwb": cmd_pwb,
            "quantseeker": cmd_quantseeker, "sweep": cmd_sweep,
            "roster": cmd_roster}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
