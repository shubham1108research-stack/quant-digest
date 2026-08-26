#!/usr/bin/env python3
"""Backfill a journal's back-catalogue from OpenAlex, by ISSN.

WHY EVERY CONFIGURED JOURNAL IS NEARLY EMPTY
sources._crossref_issn asks Crossref for `from-created-date:<30 days ago>`.
That is a rolling window, not an archive: the collector has been working
correctly and forgetting everything older than a month. Measured across FAJ and
the nine PM Research journals -- 389 papers held, 13,308 published since 1985.

WHY OPENALEX AND NOT THE THREE SOURCES ALREADY WIRED
Crossref indexes all of these and carries ZERO abstracts for Financial Analysts
Journal -- all 5,510 works, none -- because Taylor & Francis do not deposit
them. RePEc does not index practitioner journals at all: RePEc:taf:ufajxx is
listed in ListSets and returns noRecordsMatch. OpenAlex has the abstracts
(98% for FAJ, 100% for Journal of Wealth Management), plus open-access status
and a PDF link in the same record.

It is also free -- no key, `mailto` for the polite pool -- and measured at
about 2,300 papers a minute, so the whole backfill is roughly 134 calls.

AN ABSTRACT IS REQUIRED BY DEFAULT
2,069 of the 13,308 have none: Journal of Fixed Income is 58% covered, JPM 74%.
A row with no abstract embeds from its title alone, and a title-only vector is
indistinguishable from a good one once stored while still occupying a slot in
Ask's recall. The retrieval eval already puts the vocabulary tier at 0.30, and
diluting recall with two thousand thin rows works directly against it.
--allow-title-only overrides; the skipped count is always reported.

NOT SCORED. These arrive with no relevance_category, so prune.py leaves them
alone and the portal's scored views do not rank them. They are reachable from
Archive, Practitioners, tags and Ask. That is intended: collection is free,
the rubric is not, and the two decisions are separable.

    python tools/backfill_journal.py --issn 0015-198X --since 1985 --dry-run
    python tools/backfill_journal.py --all-pmr --since 1985
    python tools/backfill_journal.py --journal "Journal of Portfolio Management"
"""

import argparse
import datetime as dt
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config    # noqa: E402
import scoring   # noqa: E402
import sources   # noqa: E402
import store     # noqa: E402

OA_WORKS = "https://api.openalex.org/works"
PER_PAGE = 100
PAUSE = 0.3
MIN_YEAR = 1900


def log(m):
    print(m, flush=True)


def _all_journals():
    """Every ISSN this project tracks, name -> issn."""
    out = {}
    for d in (config.JOURNALS_T1, config.JOURNALS_T2, config.PMR_JOURNALS):
        out.update(d)
    return out


def fetch_journal(issn, since, log):
    """Every OpenAlex work for this ISSN, newest first. Cursor-paged."""
    cursor, seen = "*", 0
    while cursor:
        params = {
            "filter": f"primary_location.source.issn:{issn},"
                      f"from_publication_date:{since}-01-01",
            "per-page": PER_PAGE,
            "cursor": cursor,
        }
        try:
            r = sources._openalex_get(OA_WORKS, params, log, retries=4)
            j = r.json()
        except Exception as e:                          # noqa: BLE001
            log(f"[journal] OpenAlex failed after {seen} records: "
                f"{type(e).__name__}: {str(e)[:120]}")
            return
        results = j.get("results") or []
        if not results:
            return
        for w in results:
            seen += 1
            yield w
        cursor = (j.get("meta") or {}).get("next_cursor")
        time.sleep(PAUSE)


def to_item(w, label, section, max_year):
    """One OpenAlex work -> an archive item, or None."""
    title = (w.get("title") or w.get("display_name") or "").strip()
    if not title or scoring.is_junk(title):
        return None
    doi = (w.get("doi") or "").replace("https://doi.org/", "").lower().strip()
    if not doi:
        # Without a DOI the uid falls back to a title hash, which is a weaker
        # identity than these papers deserve -- and it is the identity that
        # decides whether this merges with a row we already hold or duplicates
        # it. Skip rather than create a second copy of a paper under a
        # different name.
        return None

    date = (w.get("publication_date") or "")[:10]
    year = w.get("publication_year")
    if not date and year:
        date = f"{year}-01-01"
    # A future date poisons portal.build's recent window -- prune.py carries a
    # future_date rule for exactly that -- and a wildly old one buries a
    # current paper at the bottom of every sort.
    if date[:4].isdigit() and not (MIN_YEAR <= int(date[:4]) <= max_year):
        date = ""

    names = [(a.get("author") or {}).get("display_name", "")
             for a in (w.get("authorships") or [])]
    loc = (w.get("primary_location") or {}).get("source") or {}
    item = {
        "title": title[:300],
        "authors": ", ".join(n for n in names if n)[:300],
        "abstract": sources._reconstruct_abstract(
            w.get("abstract_inverted_index"))[:config.ABSTRACT_CHARS],
        "url": ((w.get("primary_location") or {}).get("landing_page_url")
                or f"https://doi.org/{doi}"),
        "date": date,
        "doi": doi,
        "journal": loc.get("display_name") or label,
        "cites": w.get("cited_by_count") or 0,
        "source": f"journal:{label}",
        "section": section,
    }
    # Open access: OpenAlex reports it directly, so nothing needs to touch the
    # publisher's own open-access listing -- which for Taylor & Francis is
    # disallowed by their robots.txt in any case.
    best = w.get("best_oa_location") or {}
    if best.get("pdf_url"):
        item["pdf_url"] = best["pdf_url"]
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issn", default="")
    ap.add_argument("--journal", default="", help="name from config")
    ap.add_argument("--all-pmr", action="store_true",
                    help="every PM Research journal plus FAJ")
    ap.add_argument("--all", action="store_true", help="every configured ISSN")
    ap.add_argument("--since", type=int, default=1985)
    ap.add_argument("--section", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="cap records per journal")
    ap.add_argument("--allow-title-only", action="store_true",
                    help="keep records with no abstract (see the module docstring)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sources.MAILTO = (sources.MAILTO
                      or __import__("os").environ.get("CONTACT_EMAIL")
                      or __import__("os").environ.get("GMAIL_ADDRESS") or "")

    known = _all_journals()
    targets = []
    if args.all:
        targets = sorted(known.items())
    elif args.all_pmr:
        targets = sorted(dict(config.PMR_JOURNALS,
                              **{"Financial Analysts Journal":
                                 known.get("Financial Analysts Journal", "0015-198X")}
                              ).items())
    elif args.journal:
        if args.journal not in known:
            log(f"[journal] {args.journal!r} is not in config. Known: "
                + ", ".join(sorted(known)[:8]) + " ...")
            return 1
        targets = [(args.journal, known[args.journal])]
    elif args.issn:
        name = next((n for n, i in known.items() if i == args.issn), args.issn)
        targets = [(name, args.issn)]
    else:
        log("[journal] give --issn, --journal, --all-pmr or --all")
        return 1

    max_year = dt.date.today().year + 1
    con = store.connect()
    grand, skipped_noabs, skipped_other = [], 0, 0

    for name, issn in targets:
        got, noabs, other = [], 0, 0
        for w in fetch_journal(issn, args.since, log):
            it = to_item(w, name, args.section, max_year)
            if it is None:
                other += 1
                continue
            if not it["abstract"] and not args.allow_title_only:
                noabs += 1
                continue
            got.append(it)
            if args.limit and len(got) >= args.limit:
                break
        skipped_noabs += noabs
        skipped_other += other
        grand.extend(got)
        log(f"[journal] {name[:38]:<40} kept {len(got):>5}  "
            f"no-abstract {noabs:>4}  unusable {other:>3}")

    if not grand:
        log("[journal] nothing collected")
        return 1

    dates = sorted(x["date"] for x in grand if x["date"])
    withpdf = sum(1 for x in grand if x.get("pdf_url"))
    log(f"\n[journal] {len(grand):,} records"
        + (f", {dates[0]} to {dates[-1]}" if dates else ""))
    log(f"[journal]   skipped, no abstract : {skipped_noabs:,}")
    log(f"[journal]   skipped, no DOI/junk : {skipped_other:,}")
    log(f"[journal]   with a free PDF      : {withpdf:,}")

    if args.dry_run:
        for x in grand[:8]:
            log(f"    {x['date']:<12} {x['title'][:62]}")
        log("[journal] dry run -- nothing written")
        return 0

    fresh = store.filter_new(con, grand)
    store.save(con, fresh)
    log(f"\n[journal] inserted {len(fresh):,} new rows "
        f"({len(grand)-len(fresh):,} already held and merged)")
    log("[journal] they arrive UNSCORED on purpose -- tools/rescore.py applies "
        "the rubric when that spend is worth deciding on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
