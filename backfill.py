"""Historical backfill: the most-cited finance papers, overall AND per journal.

Writes docs/classics.json -- the data behind the portal's permanent "Classics"
(history) tab -- as:

    {"overall":  [ {paper}, ... ],           # most-cited finance papers, all-time
     "journals": {"Journal of Finance": [ {paper}, ... ], ...}}   # per journal

Ranked purely by citation count (no LLM -- the history is meant to be the full,
objective citation record, not a curated pick). Each {paper} is
{title,url,authors,journal,year,cites,summary}, where summary is the abstract
when OpenAlex has one. Journals covered = every journal the digest tracks
(Tier 1 + Tier 2 + the PM-Research titles). Run once, and re-run any time to
refresh:

    python backfill.py

Commit docs/classics.json and it ships with the portal.
"""

import json
import os
import pathlib
import time

import requests

import config

_UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}
_OVERALL_N = 250             # most-cited finance papers overall
_PER_JOURNAL_N = 40          # most-cited papers per journal
_SELECT = ("id,doi,title,publication_year,cited_by_count,"
           "authorships,primary_location,abstract_inverted_index")
_MAILTO = os.environ.get("CONTACT_EMAIL") or os.environ.get("GMAIL_ADDRESS")

# every journal the digest tracks, top-tier first
_JOURNALS = {**config.JOURNALS_T1, **config.JOURNALS_T2, **config.PMR_JOURNALS}


def _abstract(w: dict) -> str:
    inv = w.get("abstract_inverted_index")
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))[:600]


def _item(w: dict) -> dict:
    auths = w.get("authorships") or []
    loc = w.get("primary_location") or {}
    return {
        "title": (w.get("title") or "").strip(),
        "url": loc.get("landing_page_url") or w.get("id", ""),
        "authors": ", ".join(a["author"]["display_name"] for a in auths[:4]),
        "journal": (loc.get("source") or {}).get("display_name", ""),
        "year": w.get("publication_year"),
        "cites": w.get("cited_by_count") or 0,
        "summary": _abstract(w),
    }


def _get(params: dict) -> dict:
    if _MAILTO:
        params = {**params, "mailto": _MAILTO}
    r = requests.get("https://api.openalex.org/works", params=params,
                     headers=_UA, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_overall(log) -> list[dict]:
    flt = ("primary_topic.subfield.id:2003,"           # OpenAlex Finance subfield
           "from_publication_date:1970-01-01,"
           "to_publication_date:2026-06-30,type:article")
    out, cursor = [], "*"
    while len(out) < _OVERALL_N:
        j = _get({"filter": flt, "sort": "cited_by_count:desc",
                  "per-page": 200, "cursor": cursor, "select": _SELECT})
        res = j.get("results") or []
        if not res:
            break
        out += [_item(w) for w in res]
        log(f"  overall: {len(out)}")
        cursor = (j.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.3)
    return out[:_OVERALL_N]


def fetch_journal(label: str, issn: str, log) -> list[dict]:
    j = _get({"filter": f"primary_location.source.issn:{issn},type:article",
              "sort": "cited_by_count:desc", "per-page": _PER_JOURNAL_N,
              "select": _SELECT})
    return [_item(w) for w in (j.get("results") or [])]


def main() -> None:
    def log(m):
        print(m)

    overall = fetch_overall(log)
    log(f"overall: {len(overall)} most-cited finance papers (1970-2026)")

    journals = {}
    for label, issn in _JOURNALS.items():
        try:
            got = fetch_journal(label, issn, log)
        except Exception as e:                         # noqa: BLE001
            log(f"  [{label}] failed: {type(e).__name__}: {e}")
            continue
        if got:
            journals[label] = got
        log(f"  {label}: {len(got)}")
        time.sleep(0.3)

    data = {"overall": overall, "journals": journals}
    docs = pathlib.Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "classics.json").write_text(json.dumps(data, default=str),
                                        encoding="utf-8")
    total = len(overall) + sum(len(v) for v in journals.values())
    log(f"wrote docs/classics.json -- {len(overall)} overall + "
        f"{len(journals)} journals ({total} rows)")


if __name__ == "__main__":
    main()
