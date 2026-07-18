"""Historical backfill: the most-cited finance papers, overall AND per journal,
plus the curated seminal canon -- all via Crossref (free, unmetered; citations
from is-referenced-by-count). OpenAlex is intentionally not used here.

Writes docs/classics.json (the portal's "Classics" tab):

    {"overall":  [ {paper}, ... ],            # most-cited across tracked journals
     "journals": {"Journal of Finance": [ {paper}, ... ], ...},   # per journal
     "topics":   {"Asset Pricing Theory": [ {paper}, ... ], ...}} # seminal canon

overall/journals are ranked by citation count; topics is the hand-curated canon
(canon.py) grounded to real Crossref records. Journals covered = every journal
the digest tracks (Tier 1 + Tier 2 + the PM-Research titles). Run once, re-run
any time to refresh:

    python backfill.py

Commit docs/classics.json and it ships with the portal.
"""

import difflib
import html as _html
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
_OVERALL_N = 250             # most-cited finance papers overall (aggregated)
_PER_JOURNAL_N = 50          # most-cited papers kept per journal
_CR = "https://api.crossref.org"
_MAILTO = os.environ.get("CONTACT_EMAIL") or os.environ.get("GMAIL_ADDRESS")

# every journal the digest tracks, top-tier first
_JOURNALS = {**config.JOURNALS_T1, **config.JOURNALS_T2, **config.PMR_JOURNALS}


# ---------------------------------------------------------------- helpers
def _tidy(s: str, n: int = 480) -> str:
    """Unescape entities, strip HTML/JATS markup (Crossref abstracts are JATS
    XML; some carry <b>/<i>/<jats:p>), collapse whitespace, and truncate on a
    word boundary with an ellipsis rather than mid-word."""
    s = _html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"^\s*Abstract\s+", "", " ".join(s.split()), flags=re.I)
    if len(s) > n:
        s = s[:n].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"
    return s


def _cr_year(it: dict) -> int:
    dp = (it.get("published") or it.get("issued") or {}).get("date-parts") or [[0]]
    return (dp[0] or [0])[0] or 0


def _cr_get(url: str, params: dict) -> list[dict]:
    if _MAILTO:
        params = {**params, "mailto": _MAILTO}
    r = requests.get(url, params=params, headers=_UA, timeout=60)
    r.raise_for_status()
    return r.json()["message"]["items"]


def _cr_item(it: dict, journal_fallback: str = "") -> dict:
    return {
        "title": (" ".join(it.get("title") or [])).strip(),
        "url": it.get("URL") or (f"https://doi.org/{it['DOI']}" if it.get("DOI") else ""),
        "authors": ", ".join(" ".join(filter(None, [a.get("given"), a.get("family")]))
                             for a in (it.get("author") or [])[:4]),
        "journal": (it.get("container-title") or [journal_fallback])[0] or journal_fallback,
        "year": _cr_year(it),
        "cites": it.get("is-referenced-by-count") or 0,
        "summary": _tidy(it.get("abstract", "")),
        "doi": it.get("DOI"),
    }


# ------------------------------------------------ most-cited (per journal)
_ITEM_SELECT = ("DOI,title,author,container-title,published,issued,"
                "is-referenced-by-count,URL,abstract")


def fetch_journal(label: str, issn: str, log) -> list[dict]:
    items = _cr_get(f"{_CR}/journals/{issn}/works", {
        "filter": "type:journal-article",
        "sort": "is-referenced-by-count", "order": "desc",
        "rows": _PER_JOURNAL_N, "select": _ITEM_SELECT})
    rows = [_cr_item(it, label) for it in items]
    return [r for r in rows if r["title"]]


def build_overall(journals: dict, log) -> list[dict]:
    """Overall most-cited = the union of every tracked journal's top-cited,
    deduped by DOI and ranked by citations. A pure-Crossref stand-in for the
    old OpenAlex finance-subfield sweep, scoped to the journals we care about."""
    seen: dict[str, dict] = {}
    for rows in journals.values():
        for p in rows:
            k = (p.get("doi") or p.get("url") or p.get("title")).lower()
            if k not in seen or p["cites"] > seen[k]["cites"]:
                seen[k] = p
    return sorted(seen.values(), key=lambda p: p["cites"], reverse=True)[:_OVERALL_N]


# ------------------------------------------------ seminal canon (curated)
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


_BOOKISH = {"reference-entry", "book", "book-chapter", "book-part",
            "book-section", "monograph", "dataset", "component"}


def _best_match(items: list[dict], title: str, author: str, year: int):
    """Pick the Crossref candidate that best matches the curated (title, author,
    year). Author matches are weighted hard and encyclopedia/book entries
    penalised; accept only on author confirmation or a near-exact title with a
    close year -- else leave it unresolved (a Scholar link) over a wrong paper."""
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
            continue
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
    out = {}
    for topic, papers in canon.CANON.items():
        rows = []
        for (title, author, year, typ, why) in papers:
            it = None
            try:
                items = _cr_get(f"{_CR}/works", {
                    "query.bibliographic": title, "query.author": author, "rows": 6,
                    "select": "DOI,title,published,issued,container-title,"
                              "author,is-referenced-by-count,URL,type"})
                it = _best_match(items, title, author, year)
            except Exception as e:               # noqa: BLE001
                log(f"  [canon] '{title[:40]}...' lookup failed: {type(e).__name__}")
            rows.append(_canon_item(title, author, year, typ, why, it))
            time.sleep(0.15)
        rows.sort(key=lambda r: (r["year"] or 0, r["title"]))   # chronological
        out[topic] = rows
        res = sum(1 for r in rows if r["cites"] is not None)
        log(f"  {topic}: {len(rows)} papers ({res} resolved)")
    return out


# ---------------------------------------------------------------- driver
def main() -> None:
    def log(m):
        print(m)

    log("most-cited per journal (Crossref)...")
    journals = {}
    for label, issn in _JOURNALS.items():
        try:
            got = fetch_journal(label, issn, log)
        except Exception as e:                         # noqa: BLE001
            log(f"  [{label}] failed: {type(e).__name__}: {e}")
            got = []
        if got:
            journals[label] = got
        log(f"  {label}: {len(got)}")
        time.sleep(0.3)

    overall = build_overall(journals, log)
    log(f"overall: {len(overall)} most-cited across tracked journals")

    log("resolving seminal canon (curated -> grounded via Crossref)...")
    topics = resolve_canon(log)

    # drop the internal doi key from the display data
    for p in overall:
        p.pop("doi", None)
    for rows in journals.values():
        for p in rows:
            p.pop("doi", None)

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
