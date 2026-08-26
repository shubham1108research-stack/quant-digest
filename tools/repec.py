#!/usr/bin/env python3
"""RePEc as a data source: abstracts, free PDFs, impact factors, citations.

WHY REPEC AND NOT THE THREE SOURCES ALREADY HERE
tools/fill_abstracts.py asks OpenAlex, then Semantic Scholar, then Crossref.
All three depend on the PUBLISHER depositing an abstract, and the economics
publishers largely do not. Measured on this archive's gap: 941 rows, 27
recovered, 3%.

RePEc's abstracts come from the archive maintainers instead. Measured against
the same gap, one journal, one page of records: 57 of 75 matched. It succeeds
exactly where the others fail, and for a structural reason rather than a lucky
one -- it is an economics archive, and this gap is economics journals.

FOUR THINGS, ALL FROM SANCTIONED CHANNELS
Their getdata page asks plainly: "We want to discourage you strongly to scrape
the data from the websites. This put unnecessary strain on our servers." So
nothing here touches ideas.repec.org or econpapers.repec.org article pages.
Everything comes from OAI-PMH, the published statistics files, or the CitEc
API -- the routes they offer for exactly this.

    factors     journal impact factors, from the published statistics file.
                config.JOURNAL_IMPACT is currently hand-maintained approximate
                figures with a comment saying "editable".
    abstracts   fill the abstract gap by matching normalised titles.
    pdfs        record the free file URL RePEc lists for a paper we hold.
                Working papers only -- a journal record points at the
                publisher's paywalled page, and this does not go near those.
    cites       citation counts and edges from CitEc.

WHAT IT DELIBERATELY DOES NOT DO
Discovery. OAI-PMH's datestamps are not maintained -- `from=` returns
noRecordsMatch and an NBER record carries a 2022 datestamp -- so incremental
harvesting does not work and finding NEW papers would mean re-pulling whole
sets. NEP RSS, which sources.nep() already reads, is the channel RePEc intends
for that.

Non-commercial use only, per their terms.

    python tools/repec.py factors
    python tools/repec.py abstracts --dry-run
    python tools/repec.py abstracts
    python tools/repec.py pdfs
    python tools/repec.py cites --limit 400
"""

import argparse
import collections
import gzip
import json
import pathlib
import re
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config    # noqa: E402
import scoring   # noqa: E402
import store     # noqa: E402

UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}
OAI = "https://oai.repec.org/"
FACTORS_URL = "https://ideas.repec.org/top/seriesfactors10.txt"
FACTORS_OUT = pathlib.Path("repec_factors.json")
CITEC_API = "http://citec.repec.org/api/plain/"
# CitEc says: "The API has an initial limitation of 500 requests. If you need
# more, please get in contact with us in order to obtain an access key." So the
# default run stays under that and says what it did rather than being throttled
# mid-way and leaving a half-written table.
CITEC_FREE_CALLS = 450
PAUSE = 0.4


def log(m):
    print(m, flush=True)


def _get(url, tries=4):
    for i in range(tries):
        try:
            rq = urllib.request.Request(url, headers=dict(
                UA, **{"Accept-Encoding": "gzip"}))
            with urllib.request.urlopen(rq, timeout=120) as r:
                b = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    b = gzip.decompress(b)
                return b.decode("utf-8", "replace")
        except Exception as e:                          # noqa: BLE001
            if i == tries - 1:
                log(f"[repec] GET failed: {type(e).__name__}: {str(e)[:120]}")
                return ""
            time.sleep(2 * (i + 1))
    return ""


# ------------------------------------------------------------ series index
def _norm_journal(s):
    """Journal names to a comparable form.

    RePEc writes "Journal of Banking & Finance, Elsevier" where the archive
    holds "Journal of Banking and Finance": an ampersand and a publisher
    suffix. Both have to go or nothing matches -- the first attempt at this
    scored 0 of 29 for exactly that reason.
    """
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def series_index():
    """{normalised name: row} for every ARTICLE series RePEc lists."""
    txt = _get(FACTORS_URL)
    rows = [l.split("\t") for l in txt.split("\n")
            if l.startswith("RePEc:") and len(l.split("\t")) >= 10]
    idx = {}
    for r in rows:
        if not r[8].startswith("ReDIF-art"):
            continue
        for key in (_norm_journal(r[9]), _norm_journal(r[9].split(",")[0])):
            cur = idx.get(key)
            # Two series can claim one name (a journal that moved publisher).
            # Prefer the one with more items -- that is the live archive.
            if cur is None or int(r[1] or 0) > int(cur[1] or 0):
                idx[key] = r
    log(f"[repec] {len(rows):,} series listed, {len(idx):,} article names indexed")
    return idx


def our_journals(con):
    """{journal name: paper count} for what the archive actually holds."""
    out = collections.Counter()
    for uid, src, meta in con.execute("SELECT uid, source, meta FROM items"):
        try:
            d = json.loads(meta or "{}")
        except Exception:                               # noqa: BLE001
            continue
        if d.get("retired"):
            continue
        s = str(src or "").split(",")[0]
        if s.startswith("journal:"):
            out[s[8:]] += 1
    return out


def matched_series(con, idx):
    """[(our name, RePEc row, paper count)] for the journals RePEc carries."""
    hits, misses = [], []
    for name, n in our_journals(con).most_common():
        r = idx.get(_norm_journal(name))
        (hits if r else misses).append((name, r, n))
    log(f"[repec] matched {len(hits)} of {len(hits)+len(misses)} journals "
        f"({sum(h[2] for h in hits):,} papers)")
    if misses:
        # Practitioner titles -- JPM, Journal of Investing, Practical
        # Applications. RePEc indexes economics research archives, not
        # practitioner magazines, so these are absent by design, not by error.
        log(f"[repec] not in RePEc (expected, practitioner titles): "
            + ", ".join(m[0][:34] for m in misses[:6]))
    return hits


# --------------------------------------------------------------- harvesting
_REC = re.compile(r"<record>(.*?)</record>", re.S)
_TOKEN = re.compile(r"<resumptionToken[^>]*>([^<]+)</resumptionToken>")


def _field(rec, tag):
    return [re.sub(r"\s+", " ", x).strip()
            for x in re.findall(r"<dc:%s>(.*?)</dc:%s>" % (tag, tag), rec, re.S)]


def harvest(setspec, max_pages=40):
    """Yield (title, abstract, identifiers, date) for a RePEc set."""
    url = f"{OAI}?verb=ListRecords&metadataPrefix=oai_dc&set={setspec}"
    for page in range(max_pages):
        xml = _get(url)
        if not xml:
            return
        for rec in _REC.findall(xml):
            t = _field(rec, "title")
            if not t:
                continue
            yield (t[0], (_field(rec, "description") or [""])[0],
                   _field(rec, "identifier"), (_field(rec, "date") or [""])[0])
        tok = _TOKEN.search(xml)
        if not tok:
            return
        url = f"{OAI}?verb=ListRecords&resumptionToken={tok.group(1)}"
        time.sleep(PAUSE)


# ------------------------------------------------------------------- modes
def cmd_factors(args):
    """Journal impact factors, replacing hand-maintained approximations."""
    con = store.connect()
    idx = series_index()
    hits = matched_series(con, idx)
    out = {}
    for name, r, n in hits:
        out[name] = {"handle": r[0], "repec_name": r[9], "items": int(r[1] or 0),
                     "impact": float(r[2] or 0), "recursive": float(r[3] or 0),
                     "h_index": int(r[6] or 0), "papers_held": n}
    FACTORS_OUT.write_text(json.dumps(
        {"_source": "https://ideas.repec.org/top/seriesfactors10.txt",
         "_note": "RePEc's own caveat: 'this is experimental data at this "
                  "point'. Non-commercial use only.",
         "series": out}, indent=1, sort_keys=True), encoding="utf-8")
    log(f"[repec] wrote {FACTORS_OUT} ({len(out)} journals)")
    log("\n  journal                                    held  RePEc IF  hand-set")
    for name, v in sorted(out.items(), key=lambda kv: -kv[1]["impact"])[:14]:
        old = config.JOURNAL_IMPACT.get(name)
        log("  %-42s %4d %8.2f  %s" % (name[:40], v["papers_held"], v["impact"],
                                       ("%.1f" % old) if old else "-"))
    return 0


def _gap(con, want_pdf=False):
    """{normalised title: (uid, title)} for rows missing what we are after."""
    out = {}
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            d = json.loads(meta or "{}")
        except Exception:                               # noqa: BLE001
            continue
        if d.get("retired") or scoring.is_junk(title or ""):
            continue
        if want_pdf:
            if d.get("pdf_url"):
                continue
        elif (d.get("abstract") or "").strip():
            continue
        out[store.norm_title(title or "")] = (uid, title or "")
    return out


def cmd_abstracts(args):
    con = store.connect()
    gap = _gap(con)
    log(f"[repec] {len(gap):,} rows have no abstract")
    hits = matched_series(con, series_index())
    found, seen = {}, 0
    for name, r, n in hits:
        setspec = r[0]
        before = len(found)
        for title, abstract, ids, date in harvest(setspec):
            seen += 1
            if len(abstract.split()) < 20:
                continue
            key = store.norm_title(title)
            if key in gap and key not in found:
                found[key] = abstract
        log(f"[repec]   {name[:38]:<40} +{len(found)-before}")
    log(f"\n[repec] scanned {seen:,} RePEc records, matched {len(found):,} "
        f"of {len(gap):,} ({100.0*len(found)/max(1,len(gap)):.0f}%)")
    if args.dry_run:
        for k in list(found)[:8]:
            log(f"    {gap[k][1][:64]:<66} {len(found[k])} chars")
        log("[repec] dry run -- nothing written")
        return 0
    n = 0
    uids = []
    for key, abstract in found.items():
        uid = gap[key][0]
        if store.update_meta(con, uid, {"abstract": abstract[:6000],
                                        "abstract_source": "repec"}):
            n += 1
            uids.append(uid)
    # The embedding cache is keyed on a hash of the TEXT, so a backfilled row
    # would otherwise keep the vector built from its title alone -- the abstract
    # would show on the page and never reach retrieval.
    if uids:
        con.executemany("DELETE FROM embeddings WHERE uid=?", [(u,) for u in uids])
        log(f"[repec] invalidated {len(uids)} cached vectors for re-embedding")
    con.commit()
    log(f"[repec] wrote {n:,} abstracts")
    return 0


def cmd_pdfs(args):
    """Record the free file URL RePEc lists, where we hold the paper.

    Working papers only. A journal record's identifier points at the
    publisher's own page, which is paywalled and not what this is for -- so
    anything that is not plainly a PDF is skipped rather than stored and
    discovered later by a fetcher.
    """
    con = store.connect()
    gap = _gap(con, want_pdf=True)
    log(f"[repec] {len(gap):,} rows have no pdf_url")
    sets = [s.strip() for s in (args.sets or "RePEc:nbr:nberwo").split(",") if s.strip()]
    found = {}
    for setspec in sets:
        before = len(found)
        for title, abstract, ids, date in harvest(setspec):
            key = store.norm_title(title)
            if key not in gap or key in found:
                continue
            for i in ids:
                if i.lower().startswith("http") and i.lower().endswith(".pdf"):
                    found[key] = i
                    break
        log(f"[repec]   {setspec:<26} +{len(found)-before}")
    log(f"[repec] {len(found):,} free PDFs found for papers we hold")
    if args.dry_run:
        for k in list(found)[:8]:
            log(f"    {gap[k][1][:56]:<58} {found[k][:52]}")
        log("[repec] dry run -- nothing written")
        return 0
    n = 0
    for key, url in found.items():
        if store.update_meta(con, gap[key][0], {"pdf_url": url}):
            n += 1
    con.commit()
    log(f"[repec] recorded {n:,} pdf_url values -- tools/fetch_pdfs.py will "
        f"collect them")
    return 0


def cmd_cites(args):
    """Citation counts from CitEc, for papers whose RePEc handle we can find.

    Bounded by CitEc's own stated free limit rather than by our appetite: "The
    API has an initial limitation of 500 requests." Going past that quietly
    would be taking something not offered.

    Counts, not edges. The edges live in the AMF bulk files and would need both
    ends resolved from a RePEc handle back to one of our uids; counts are
    useful on their own -- they feed the same ranking signal cites already
    does -- and they are one call each.
    """
    con = store.connect()
    idx = series_index()
    hits = matched_series(con, idx)
    # title -> uid, for the papers we hold in RePEc-covered journals
    ours = {}
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            d = json.loads(meta or "{}")
        except Exception:                               # noqa: BLE001
            continue
        if d.get("retired") or d.get("cites") is not None:
            continue
        ours[store.norm_title(title or "")] = uid
    log(f"[repec] {len(ours):,} rows have no citation count")

    budget = min(args.limit or CITEC_FREE_CALLS, CITEC_FREE_CALLS)
    handles = []
    for name, r, n in hits:
        for title, abstract, ids, date in harvest(r[0]):
            key = store.norm_title(title)
            if key not in ours:
                continue
            h = next((i for i in ids if i.startswith("RePEc:")), "")
            if h:
                handles.append((ours[key], h))
            if len(handles) >= budget:
                break
        if len(handles) >= budget:
            break
    log(f"[repec] {len(handles)} papers resolved to a RePEc handle "
        f"(capped at CitEc's free {budget})")

    n = 0
    for uid, h in handles:
        xml = _get(CITEC_API + h, tries=2)
        m = re.search(r"<citedBy>(\d+)</citedBy>", xml or "")
        if not m:
            continue
        if store.update_meta(con, uid, {"cites": int(m.group(1)),
                                        "cites_source": "citec"}):
            n += 1
        time.sleep(PAUSE)
        if n and n % 50 == 0:
            log(f"[repec]   {n}/{len(handles)}")
    con.commit()
    log(f"[repec] wrote {n:,} citation counts")
    if len(handles) >= budget:
        log("[repec] stopped at CitEc's free limit. More needs an access key: "
            "see http://citec.repec.org/api.html")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("factors", "abstracts", "pdfs", "cites"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sets", default="",
                    help="pdfs: comma-separated RePEc sets to scan")
    args = ap.parse_args()
    return {"factors": cmd_factors, "abstracts": cmd_abstracts,
            "pdfs": cmd_pdfs, "cites": cmd_cites}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
