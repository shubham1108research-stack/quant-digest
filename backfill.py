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

import difflib
import json
import os
import pathlib
import re
import time
from urllib.parse import quote

import requests

import canon
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


# ---- seminal canon: resolve each curated paper to its real OpenAlex record ---
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _cr_year(it: dict) -> int:
    dp = (it.get("published") or it.get("issued") or {}).get("date-parts") or [[0]]
    return (dp[0] or [0])[0] or 0


_BOOKISH = {"reference-entry", "book", "book-chapter", "book-part",
            "book-section", "monograph", "dataset", "component"}


def _best_match(items: list[dict], title: str, author: str, year: int):
    """Pick the Crossref candidate that best matches the curated (title, author,
    year), or None if nothing clears the bar. Author matches are weighted hard
    and encyclopedia/book entries penalised, so a near-identical reference-entry
    title (e.g. a SpringerReference stub) never wins over the real article."""
    tnorm = _norm(title)
    best = None
    best_score = best_ratio = 0.0
    best_auth = False
    best_year = 0
    for it in items:
        wt = _norm(" ".join(it.get("title") or []))
        if not wt:
            continue
        ratio = difflib.SequenceMatcher(None, tnorm, wt).ratio()
        auth_ok = any(author.lower() == (a.get("family") or "").lower()
                      for a in (it.get("author") or []))
        yr = _cr_year(it)
        if not auth_ok and year and yr and abs(yr - year) > 7:
            continue                              # wrong era + wrong author = not it
        t = it.get("type", "")
        type_adj = (0.05 if t in ("journal-article", "proceedings-article")
                    else -0.5 if t in _BOOKISH else 0.0)
        score = (ratio + (0.15 if auth_ok else 0) + type_adj
                 - (0.10 if year and yr and abs(yr - year) > 3 else 0))
        if score > best_score:
            best, best_score, best_ratio, best_auth, best_year = \
                it, score, ratio, auth_ok, yr
    if not best:
        return None
    # accept on author confirmation; or on a near-exact title, but only if the
    # year is also close (else it's a same-topic namesake, not the paper)
    far = bool(year and best_year and abs(best_year - year) > 5)
    if (best_auth and best_score >= 0.60) or (best_ratio >= 0.82 and not far):
        return best
    return None


def _canon_item(title, author, year, typ, why, it: dict | None) -> dict:
    if it:
        auths = ", ".join(" ".join(filter(None, [a.get("given"), a.get("family")]))
                          for a in (it.get("author") or [])[:4])
        return {
            "title": (" ".join(it.get("title") or []) or title).strip(),
            "authors": auths or author,
            "journal": (it.get("container-title") or [""])[0],
            "year": _cr_year(it) or year,
            "cites": it.get("is-referenced-by-count"),
            "url": it.get("URL") or (f"https://doi.org/{it['DOI']}" if it.get("DOI") else ""),
            "type": typ, "why": why,
        }
    return {                                    # unresolved -> Scholar search link
        "title": title, "authors": author, "journal": "", "year": year,
        "cites": None, "type": typ, "why": why,
        "url": f"https://scholar.google.com/scholar?q={quote(title)}",
    }


def resolve_canon(log) -> dict:
    """Resolve each curated canon paper to its real record via Crossref (free,
    unmetered, and carries is-referenced-by-count for the cite figure)."""
    out = {}
    for topic, papers in canon.CANON.items():
        rows = []
        for (title, author, year, typ, why) in papers:
            it = None
            try:
                params = {"query.bibliographic": title, "query.author": author,
                          "rows": 6,
                          "select": "DOI,title,published,issued,container-title,"
                                    "author,is-referenced-by-count,URL,type"}
                if _MAILTO:
                    params["mailto"] = _MAILTO
                r = requests.get("https://api.crossref.org/works", params=params,
                                 headers=_UA, timeout=60)
                r.raise_for_status()
                it = _best_match(r.json()["message"]["items"], title, author, year)
            except Exception as e:               # noqa: BLE001
                log(f"  [canon] '{title[:40]}...' lookup failed: {type(e).__name__}")
            rows.append(_canon_item(title, author, year, typ, why, it))
            time.sleep(0.15)
        rows.sort(key=lambda r: (r["year"] or 0, r["title"]))   # chronological
        out[topic] = rows
        res = sum(1 for r in rows if r["cites"] is not None)
        log(f"  {topic}: {len(rows)} papers ({res} resolved)")
    return out


def _existing(key: str, default):
    """The prior value from docs/classics.json -- used to preserve the OpenAlex
    most-cited data when today's OpenAlex free budget is exhausted."""
    try:
        return json.load(open("docs/classics.json", encoding="utf-8")).get(key, default)
    except Exception:                                  # noqa: BLE001
        return default


def main() -> None:
    def log(m):
        print(m)

    # OpenAlex now meters a small daily free budget; if the most-cited fetches
    # fail (budget/HTTP), keep whatever the last good run wrote.
    try:
        overall = fetch_overall(log) or _existing("overall", [])
    except Exception as e:                             # noqa: BLE001
        log(f"overall fetch failed ({type(e).__name__}); keeping existing")
        overall = _existing("overall", [])
    log(f"overall: {len(overall)} most-cited finance papers")

    journals = {}
    for label, issn in _JOURNALS.items():
        try:
            got = fetch_journal(label, issn, log)
        except Exception as e:                         # noqa: BLE001
            log(f"  [{label}] failed ({type(e).__name__}); keeping existing")
            got = _existing("journals", {}).get(label, [])
        if got:
            journals[label] = got
        log(f"  {label}: {len(got)}")
        time.sleep(0.3)

    log("resolving seminal canon (curated -> grounded via Crossref)...")
    topics = resolve_canon(log)

    data = {"overall": overall, "journals": journals, "topics": topics}
    docs = pathlib.Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "classics.json").write_text(json.dumps(data, default=str),
                                        encoding="utf-8")
    total = len(overall) + sum(len(v) for v in journals.values())
    canon_n = sum(len(v) for v in topics.values())
    log(f"wrote docs/classics.json -- {len(overall)} overall + "
        f"{len(journals)} journals ({total} rows) + {canon_n} canon papers "
        f"across {len(topics)} topics")


if __name__ == "__main__":
    main()
