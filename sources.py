"""Collectors. Each returns list[dict] with keys:
title, authors, abstract, url, date, source, section, plus doi/arxiv_id when known.
Every collector raises on hard failure -- main.py catches per-source so one
dead feed never kills the run.
"""

import datetime as dt
import html as _html
import json
import os
import re
import time

import feedparser
import requests

import config

UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}
MAILTO = None  # set from env in main.py; appended to polite-pool APIs


def _cutoff() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=config.LOOKBACK_DAYS)


def _clean(s: str) -> str:
    """Strip markup, then unescape entities -- in that order.

    Unescape FIRST, then strip. Publishers double-encode: an abstract arrives
    with "&lt;b&gt;index&lt;/b&gt;", and stripping before unescaping leaves the
    tag behind as literal text. This is text extraction, not sanitisation --
    the portal escapes everything at render (esc()) -- so the goal is simply
    that no markup reaches the embedding or the prompt.

    Unescaping was missing entirely: 1,130 abstracts carried raw entities
    ("S&amp;P 500", literal nbsp), and one of the map's 24 clusters came back
    as "risk, nbsp, span" -- the model was clustering on HTML."""
    txt = _html.unescape(s or "")
    # anchored on a real tag name: a bare "<" in maths ("returns < 0", "t > 2")
    # is not markup, and <[^>]+> ate everything between them -- turning
    # "a &lt; b and c &gt; d" into "a d".
    txt = re.sub(r"</?[a-zA-Z][a-zA-Z0-9]*[^>]*>", " ", txt)
    return " ".join(_html.unescape(txt).split())


def _entry_date(e) -> dt.datetime | None:
    for k in ("published_parsed", "updated_parsed"):
        if getattr(e, k, None):
            return dt.datetime(*getattr(e, k)[:6], tzinfo=dt.timezone.utc)
    return None


# ---------------------------------------------------------------- NEP
_REPEC_ARXIV = re.compile(r"RePEc:arx:papers:([\d.]+)", re.I)


def nep() -> list[dict]:
    out = []
    for code in config.NEP_CODES:
        feed = feedparser.parse(config.NEP_URL.format(code=code))
        for e in feed.entries:
            link = e.get("link", "")
            # NEP mailing-list items often just mirror an arXiv paper via a
            # RePEc redirect URL; without this the same paper collected
            # directly from arXiv gets a different uid and dedup misses it
            m = _REPEC_ARXIV.search(link)
            item = {
                "title": _clean(e.get("title", "")),
                "authors": "",
                "abstract": _clean(e.get("description", "") or e.get("summary", "")),
                "url": link,
                "date": (d := _entry_date(e)) and d.date().isoformat() or "",
                "source": f"nep-{code}",
                "section": 1,
            }
            if m:
                item["arxiv_id"] = m.group(1)
            out.append(item)
        time.sleep(0.5)
    return out


# --------------------------------------------------------------- NBER
_NBER_A = re.compile(r"<a[^>]*>([^<]+)</a>")     # strip the <a href> author markup


def _nber_item(e: dict) -> dict:
    wp = (e.get("url") or "").rsplit("/", 1)[-1]         # e.g. 'w35441'
    return {
        "title": _clean(e.get("title", "")),
        "authors": _clean(", ".join(_NBER_A.findall(" ".join(e.get("authors") or [])))),
        "abstract": _clean(e.get("abstract", "")),
        "url": "https://www.nber.org" + (e.get("url") or ""),
        "date": _nber_date(e.get("displaydate")),
        "source": "nber",
        "section": 1,
        "nber_wp": wp,
        # Without this the uid falls through to t:<title-hash>, while
        # backfill._nber_month builds the same paper as doi:10.3386/w... --
        # two namespaces for one paper, and 46 NBER papers were archived
        # twice. make_uid already unifies arXiv this way; NBER was missed.
        "doi": f"10.3386/{wp}" if wp else None,
    }


def _nber_program(program: str, start: str, end: str, log) -> list[dict]:
    """All NBER working papers in [start, end] tagged with one NBER PROGRAM."""
    out = []
    for page in range(1, config.NBER_MAX_PAGES + 1):
        params = {"page": page, "perPage": config.NBER_PER_PAGE,
                  "sortBy": "public_date", "startDate": start, "endDate": end,
                  # verbatim: config.NBER_FACETS already carries the
                  # "programs:"/"topics:" prefix, so both taxonomies work
                  "facet": program if ":" in program else f"programs:{program}"}
        r = requests.get(config.NBER_API, params=params, headers=UA, timeout=45)
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        out += [_nber_item(e) for e in results]
        if len(results) < config.NBER_PER_PAGE:
            break
        time.sleep(0.4)
    return out


def nber(log=print, start: str | None = None, end: str | None = None) -> list[dict]:
    """NBER working papers restricted to the finance PROGRAMS (Asset Pricing,
    Corporate Finance, Monetary Economics, International Finance & Macro) over
    the window -- NBER's own authoritative classification, not a keyword guess
    (also catches methods/AP papers whose titles lack an obvious finance term).
    Complete windowed coverage, unlike the old rolling RSS. Defaults to the
    lookback window; explicit start/end let the portal browse a month."""
    start = start or _cutoff().date().isoformat()
    end = end or dt.date.today().isoformat()
    by_uid = {}
    for program in config.NBER_FACETS:
        try:
            for it in _nber_program(program, start, end, log):
                k = it["nber_wp"] or it["url"]
                # keep WHICH facet matched -- it was dropped entirely, even
                # though the query already knew it, and it is free metadata
                # that the sleeve classifier can be measured against
                if k in by_uid:
                    by_uid[k].setdefault("nber_facets", []).append(program)
                else:
                    it["nber_facets"] = [program]
                    by_uid[k] = it
        except Exception as e:                             # noqa: BLE001
            log(f"[nber] program '{program}' failed: {type(e).__name__}")
        time.sleep(0.3)
    out = list(by_uid.values())
    log(f"[nber] {len(out)} finance-program papers in window {start}..{end}")
    return out


def _nber_date(displaydate: str) -> str:
    """NBER gives 'July 2026' (month granularity); map to the 1st of the month,
    or fall back to today if unparseable."""
    try:
        return dt.datetime.strptime((displaydate or "").strip(), "%B %Y").date() \
            .replace(day=1).isoformat()
    except Exception:                                  # noqa: BLE001
        return dt.date.today().isoformat()


# -------------------------------------------------------------- arXiv
def _arxiv_api() -> list[dict]:
    query = " OR ".join(f"cat:{c}" for c in config.ARXIV_CATS)
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": config.ARXIV_MAX,
    }
    # arXiv rate-limits by IP (429) and asks for spacing between requests; retry
    # with linear backoff before giving up so a busy window doesn't drop the feed.
    r = None
    for attempt in range(config.ARXIV_MAX_RETRIES):
        r = requests.get(config.ARXIV_API, params=params, headers=UA, timeout=60)
        if r.status_code != 429:
            break
        wait = int(float(r.headers.get("retry-after", 0))) or 5 * (attempt + 1)
        time.sleep(min(wait, 30))
    r.raise_for_status()
    feed = feedparser.parse(r.text)
    cut = _cutoff()
    out = []
    for e in feed.entries:
        d = _entry_date(e)
        if d and d < cut:
            break  # date-sorted descending
        aid = e.get("id", "").rsplit("/abs/", 1)[-1]
        out.append({
            "title": _clean(e.get("title", "")),
            "authors": ", ".join(a.name for a in e.get("authors", [])[:4]),
            "abstract": _clean(e.get("summary", "")),
            "url": e.get("id", ""),
            "date": d.date().isoformat() if d else "",
            "source": "arxiv",
            "section": 2,
            "arxiv_id": aid,
        })
    return out


def _arxiv_rss(log) -> list[dict]:
    """Fallback: per-category arXiv RSS feeds -- a lighter, far-less-throttled
    endpoint than the bulk API. Each feed lists the latest announcement day's
    new papers (empty on weekends; arXiv doesn't announce Sat/Sun). Best-effort
    per category; dedup across categories is left to store.filter_new."""
    out, seen = [], set()
    cut = _cutoff()
    for cat in config.ARXIV_CATS:
        try:
            r = requests.get(config.ARXIV_RSS.format(cat=cat), headers=UA, timeout=30)
            r.raise_for_status()
            feed = feedparser.parse(r.text)
        except Exception as e:                       # noqa: BLE001
            log(f"[arxiv] RSS '{cat}' failed: {type(e).__name__}")
            continue
        for e in feed.entries:
            link = e.get("link", "") or e.get("id", "")
            aid = link.rsplit("/abs/", 1)[-1]
            if aid in seen:
                continue
            seen.add(aid)
            d = _entry_date(e)
            if d and d < cut:
                continue
            desc = e.get("summary", "") or e.get("description", "")
            # new-format description embeds "... Abstract: <text>" -- keep the abstract
            parts = re.split(r"Abstract:\s*", desc, maxsplit=1, flags=re.I)
            abstract = _clean(parts[-1] if parts else desc)
            title = re.sub(r"\s*\(arXiv:[^)]*\)\s*$", "", e.get("title", ""))
            out.append({
                "title": _clean(title),
                "authors": _clean(e.get("author", "")),
                "abstract": abstract,
                "url": link,
                "date": d.date().isoformat() if d else "",
                "source": "arxiv",
                "section": 2,
                "arxiv_id": aid,
            })
        time.sleep(1)                                # polite spacing between feeds
    return out


def arxiv(log=print) -> list[dict]:
    """Bulk API first; on any failure (typically a 429 after retries) fall back
    to the per-category RSS feeds so a throttled window doesn't drop arXiv."""
    try:
        return _arxiv_api()
    except Exception as e:                           # noqa: BLE001
        log(f"[arxiv] bulk API failed ({type(e).__name__}); per-category RSS fallback")
        return _arxiv_rss(log)


def _cr_date_parts(node: dict) -> str:
    """Crossref date-parts -> zero-padded ISO. Mirrors backfill._cr_date."""
    dp = (node or {}).get("date-parts") or [[]]
    parts = (dp[0] or [])[:3]
    if not parts or not parts[0]:
        return ""
    y = parts[0]
    m = parts[1] if len(parts) > 1 else 1
    d = parts[2] if len(parts) > 2 else 1
    try:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except (TypeError, ValueError):
        return ""


# ----------------------------------------------------------- Crossref
def _crossref_item(w: dict, source: str) -> dict | None:
    title = _clean(" ".join(w.get("title") or []))
    if not title:
        return None
    authors = ", ".join(
        " ".join(filter(None, [a.get("given"), a.get("family")]))
        for a in (w.get("author") or [])[:4])
    return {
        "title": title,
        "authors": authors,
        "abstract": _clean(w.get("abstract", "")),
        "url": w.get("URL", ""),
        # Zero-padded ISO, not "-".join. Crossref date-parts are integers, so
        # joining them gave "2026-8-3" -- which str(date)[:7] turns into
        # "2026-8-", a month key that can never equal the "%Y-%m" the monthly
        # composite looks for. 34% of the archive was invisible to it, which is
        # why docs/monthly.json holds no journal or SSRN papers at all. It also
        # broke every lexicographic date sort ("2026-7-3" > "2026-10-01").
        "date": _cr_date_parts(w.get("created") or w.get("issued") or {}),
        "source": source,
        "section": 3,
        "doi": w.get("DOI"),
    }


def _crossref_issn(issn: str, label: str) -> list[dict]:
    since = _cutoff().date().isoformat()
    params = {"filter": f"from-created-date:{since}", "rows": 100,
              "sort": "created", "order": "desc"}
    if MAILTO:
        params["mailto"] = MAILTO
    r = requests.get(f"https://api.crossref.org/journals/{issn}/works",
                     params=params, headers=UA, timeout=60)
    r.raise_for_status()
    out = []
    for w in r.json()["message"]["items"]:
        it = _crossref_item(w, f"journal:{label}")
        if it:
            out.append(it)
    return out


# --------------------------------------------- abstract extraction helpers
def _reconstruct_abstract(inv: dict | None) -> str:
    """Rebuild plain text from OpenAlex's abstract_inverted_index
    ({word: [positions]}). Empty when the field is absent."""
    if not inv:
        return ""
    pairs = [(p, word) for word, positions in inv.items() for p in positions]
    pairs.sort()
    return _clean(" ".join(w for _, w in pairs))[:1500]


# publisher article pages that aren't Cloudflare-gated (pm-research, many
# Atypon/Highwire sites) expose the abstract in a meta tag -- try each in order.
# The closing class must BACKREFERENCE the opening quote. Accepting either
# quote meant a double-quoted content="..." attribute ended at the first raw
# apostrophe in the prose -- "the firm's" truncated the abstract to "the firm".
# The guard downstream is len(txt) > 80, which is not "is it complete", so any
# abstract with 80 characters before its first apostrophe was stored as if
# whole and scored by the LLM as a fragment.
_META_ABS = (
    r'<meta[^>]+name=(["\'])citation_abstract\1[^>]+content=(["\'])(.*?)\2',
    r'<meta[^>]+name=(["\'])dc\.?Description\1[^>]+content=(["\'])(.*?)\2',
    r'<meta[^>]+property=(["\'])og:description\1[^>]+content=(["\'])(.*?)\2',
)


def _scrape_abstract(url: str) -> str:
    """Fetch an article page and pull the abstract from its meta tags. Static
    HTML only -- returns '' on non-200 (Cloudflare/paywall), short blurbs, or
    any error, so a blocked publisher never breaks the run."""
    if not url:
        return ""
    try:
        r = requests.get(url, headers=UA, timeout=25)
        if r.status_code != 200:
            return ""
        for pat in _META_ABS:
            m = re.search(pat, r.text, re.I | re.S)
            if m:
                txt = _clean(_html.unescape(m.group(3)))
                if len(txt) > 80:            # skip truncated og:description blurbs
                    return txt[:1500]
    except Exception:                                # noqa: BLE001
        pass
    return ""


def pmr(log, existing: set) -> list[dict]:
    """PM Research practitioner journals -- article list via Crossref (PMR does
    not deposit abstracts there), abstract scraped from each pm-research.com
    page. Abstracts are fetched only for articles NOT already in the archive
    (`existing` uids), so re-runs don't re-scrape. All tagged tier T2."""
    since = _cutoff().date().isoformat()
    out = []
    for label, issn in config.PMR_JOURNALS.items():
        params = {"filter": f"from-created-date:{since}",
                  "rows": config.PMR_MAX_PER_JOURNAL, "sort": "created",
                  "order": "desc"}
        if MAILTO:
            params["mailto"] = MAILTO
        try:
            r = requests.get(f"https://api.crossref.org/journals/{issn}/works",
                             params=params, headers=UA, timeout=60)
            r.raise_for_status()
        except Exception as e:                       # noqa: BLE001
            log(f"[pmr] '{label}' list failed: {type(e).__name__}: {e}")
            continue
        got = new = 0
        for w in r.json()["message"]["items"]:
            it = _crossref_item(w, f"journal:{label}")
            if not it:
                continue
            it["tier"] = "T2"
            doi = (it.get("doi") or "").lower()
            if doi and f"doi:{doi}" not in existing:   # only scrape net-new
                it["abstract"] = _scrape_abstract(it.get("url", ""))
                if it["abstract"]:
                    new += 1
                time.sleep(0.4)                        # polite to pm-research
            out.append(it)
            got += 1
        print(f"  pmr/{label}: {got} articles ({new} new abstracts)")
        time.sleep(0.5)
    return out


# ------------------------------------------- SSRN via Crossref (10.2139)
def ssrn_crossref(log) -> list[dict]:
    """SSRN papers through Crossref (DOI prefix 10.2139) -- fresh, free, no
    Cloudflare. Finance queries narrow the all-discipline SSRN firehose; the
    LLM layer filters the rest. One bad query is logged, not fatal."""
    since = _cutoff().date().isoformat()
    out = []
    for q in config.SSRN_QUERIES:
        params = {
            "filter": f"prefix:{config.SSRN_CROSSREF_PREFIX},"
                      f"from-created-date:{since}",
            "query.bibliographic": q, "rows": config.SSRN_ROWS,
            # sort=created makes Crossref return the NEWEST SSRN papers and
            # ignore query relevance completely -- and SSRN hosts every
            # discipline, so the finance queries were decoration. That is how
            # "Formation of densified activated sludge" and "Value-added
            # utilization of waste polyvinyl chloride" reached a quant archive.
            # from-created-date already bounds recency, so relevance is free.
            "sort": "relevance",
        }
        if MAILTO:
            params["mailto"] = MAILTO
        try:
            r = requests.get("https://api.crossref.org/works", params=params,
                             headers=UA, timeout=60)
            r.raise_for_status()
        except Exception as e:                           # noqa: BLE001
            log(f"[ssrn] query '{q[:30]}...' failed: {type(e).__name__}: {e}")
            continue
        for w in r.json()["message"]["items"]:
            it = _crossref_item(w, "SSRN (via Crossref)")
            if it:
                out.append(it)
        time.sleep(0.5)                                  # Crossref politeness
    return out


def journals(log) -> list[dict]:
    """Crossref by ISSN, tier by tier. One bad ISSN is logged, not fatal."""
    out = []
    for tier, mp in (("T1", config.JOURNALS_T1), ("T2", config.JOURNALS_T2)):
        for label, issn in mp.items():
            try:
                got = _crossref_issn(issn, label)
            except Exception as e:                   # noqa: BLE001
                log(f"[journals] {tier} '{label}' ({issn}) failed: "
                    f"{type(e).__name__}: {e}")
                continue
            for it in got:
                it["tier"] = tier
            out += got
            time.sleep(0.5)
    return out


# --------------------------------------------------------- Quantocracy
def quantocracy() -> list[dict]:
    feed = feedparser.parse(config.QUANTOCRACY_RSS)
    cut = _cutoff()
    out = []
    for e in feed.entries:
        d = _entry_date(e)
        if d and d < cut:
            continue
        out.append({
            "title": _clean(e.get("title", "")),
            "authors": "",
            "abstract": _clean(e.get("description", "") or e.get("summary", ""))[:600],
            "url": e.get("link", ""),
            "date": d.date().isoformat() if d else "",
            "source": "quantocracy",
            "section": 4,
        })
    return out


# ------------------------------------------- Practitioner RSS feeds
def practitioner(log) -> list[dict]:
    """Direct practitioner blogs (Alpha Architect, Quantpedia, ...). One bad
    feed is logged and skipped, never killing the others."""
    cut = _cutoff()
    out = []
    for label, url in config.PRACTITIONER_FEEDS.items():
        try:
            feed = feedparser.parse(url, agent=UA["User-Agent"])
        except Exception as e:                       # noqa: BLE001
            log(f"[practitioner] '{label}' failed: {type(e).__name__}: {e}")
            continue
        got = 0
        for e in feed.entries:
            d = _entry_date(e)
            if d and d < cut:
                continue
            out.append({
                "title": _clean(e.get("title", "")),
                "authors": _clean(e.get("author", "")),
                "abstract": _clean(e.get("description", "")
                                   or e.get("summary", ""))[:600],
                "url": e.get("link", ""),
                "date": d.date().isoformat() if d else "",
                "source": label,
                "section": 4,
            })
            got += 1
        print(f"  practitioner/{label}: {got} posts")
        time.sleep(0.3)
    return out


# ------------------------------------ OpenAlex (preprint repositories)
def _openalex_get(url: str, params: dict, log, retries: int | None = None
                  ) -> requests.Response:
    """GET with linear backoff on a genuine 429 rate limit. `retries` overrides
    config.OPENALEX_MAX_RETRIES -- callers in a hot per-item loop (the watchlist
    author pull) pass a small value to FAIL FAST when OpenAlex is throttling the
    runner IP, so a throttled OpenAlex can't wedge the whole run (those authors
    are covered by the Crossref pull + the full cross-match anyway).

    NOTE: OpenAlex also returns 429 for a *paywalled feature* (e.g. the
    from_created_date filter needs a paid plan) -- that is not a rate limit
    and must NOT be retried, so we detect the 'plan upgrade' body and fail
    fast instead of burning the full backoff budget."""
    if MAILTO:
        params = {**params, "mailto": MAILTO}
    last = None
    for i in range(retries if retries is not None else config.OPENALEX_MAX_RETRIES):
        r = requests.get(url, params=params, headers=UA, timeout=30)
        last = r
        if r.status_code != 429:
            r.raise_for_status()
            return r
        if "upgrade required" in r.text.lower() or "paid plan" in r.text.lower():
            log(f"[openalex] paywalled request, not retrying: {r.text[:160]}")
            r.raise_for_status()          # raises -- caller logs & skips source
        wait = 3 * (i + 1)
        log(f"[openalex] 429; retry {i + 1}/{config.OPENALEX_MAX_RETRIES} "
            f"after {wait}s")
        time.sleep(wait)
    last.raise_for_status()  # exhausted retries -- surface the final 429
    return last


def _resolve_openalex_source(name: str, log) -> str | None:
    r = _openalex_get("https://api.openalex.org/sources",
                      {"search": name}, log)
    for s in r.json().get("results", []):
        if name.lower() in (s.get("display_name") or "").lower():
            sid = s["id"].rsplit("/", 1)[-1]
            log(f"[openalex] resolved '{name}' -> {sid} "
                f"('{s.get('display_name')}') -- verify once, then hardcode "
                f"in config.OPENALEX_PREPRINT_SOURCES")
            return sid
    return None


def _oa_item(w: dict, source: str, section: int) -> dict:
    auths = w.get("authorships") or []
    return {
        "title": _clean(w.get("display_name", "")),
        "authors": ", ".join(a["author"]["display_name"] for a in auths[:4]),
        "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
        "url": (w.get("primary_location") or {}).get("landing_page_url")
               or w.get("id", ""),
        "date": w.get("publication_date", ""),
        "source": source,
        "section": section,
        "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
        # OpenAlex author ids -- reliable for native items; used for the
        # author-citation prominence signal (see prominence.py).
        "oa_author_ids": [a["author"]["id"].rsplit("/", 1)[-1]
                          for a in auths[:4] if a.get("author", {}).get("id")],
    }


def _openalex_works(sid: str, label: str, log) -> list[dict]:
    since = _cutoff().date().isoformat()
    r = _openalex_get("https://api.openalex.org/works", {
        "filter": f"locations.source.id:{sid},from_publication_date:{since}",
        "per-page": 100,
        "sort": "publication_date:desc",
    }, log)
    return [_oa_item(w, f"{label} (via OpenAlex)", 3)
            for w in r.json().get("results", [])]


def openalex_preprints(log) -> list[dict]:
    """Probe each configured OpenAlex preprint repository independently."""
    out = []
    for label, sid in config.OPENALEX_PREPRINT_SOURCES.items():
        if not sid:
            sid = _resolve_openalex_source(label, log)
        if not sid:
            log(f"[openalex] source id for '{label}' not found; skipped")
            continue
        try:
            got = _openalex_works(sid, label, log)
            print(f"  openalex/{label}: {len(got)} works")
            out += got
        except Exception as e:                       # noqa: BLE001
            log(f"[openalex] '{label}' failed: {type(e).__name__}: {e}")
        time.sleep(1.0)  # polite spacing between sources
    return out


# ------------------------------------ P2: author watchlist
def _load_roster() -> dict:
    try:
        return (json.load(open(os.path.join("docs", "watchlist.json"),
                               encoding="utf-8")) or {}).get("authors", {})
    except Exception:                                  # noqa: BLE001
        return {}


def _name_key(name: str) -> tuple[str, str] | None:
    """Normalize a person name to (first_token, last_token), accent/punct
    stripped, for robust cross-source matching (item 'Bryan T. Kelly' and
    roster 'Bryan Kelly' both -> ('bryan','kelly'))."""
    n = _clean(_html.unescape(name or "")).lower()
    n = re.sub(r"[^a-z\s-]", "", n.replace(".", " "))
    toks = [t for t in n.split() if t]
    if len(toks) < 2:
        return None
    return toks[0], toks[-1]


def watchlist_crossmatch(items: list[dict], log=print) -> int:
    """Tag any ALREADY-COLLECTED item (from NBER/journals/SSRN/arXiv/...) whose
    author matches a roster author as watchlist -- so a watched author's paper
    is caught the moment it appears in ANY source, NOT only once OpenAlex
    indexes it (OpenAlex lags weeks-months on working papers, which is why a
    fresh Kelly NBER paper was collected but not flagged). Matches on
    (first, last) with a first-initial fallback; the finance context of the
    already-collected pool keeps false positives low."""
    roster = _load_roster()
    if not roster:
        return 0
    watched: dict[str, list[tuple[str, str]]] = {}     # last -> [(first, name)]
    for v in roster.values():
        k = _name_key(v.get("name", ""))
        if k:
            watched.setdefault(k[1], []).append((k[0], v.get("name", "")))
    tagged = 0
    for it in items:
        if it.get("watchlist"):
            continue                                   # already from the OA pull
        for author in re.split(r"[;,]", it.get("authors", "") or ""):
            k = _name_key(author)
            if not k or k[1] not in watched:
                continue
            for first, name in watched[k[1]]:
                # require a FULL first-name match -- an initial-only match
                # ("S. Gu" == "Shihao Gu") produces rampant false positives on
                # common surnames (Gu/Chen/Li/Wang), pulling in unrelated
                # papers (power electronics, PhD theses). Both names must give a
                # real first token and be equal.
                if len(k[0]) > 1 and len(first) > 1 and k[0] == first:
                    it["watchlist"] = True
                    it["watchlist_author"] = name
                    tagged += 1
                    break
            if it.get("watchlist"):
                break
    log(f"[watchlist] cross-matched {tagged} collected items to watched authors")
    return tagged


def watchlist(log=print) -> list[dict]:
    """Pull recent works by every author on the roster (docs/watchlist.json,
    built quarterly by tools/gen_watchlist.py), regardless of whether a source
    feed carried them -- this is the safety net so a Kelly/Xiu paper is never
    missed. Items are tagged watchlist=True + the author's name; downstream they
    jump the LLM scoring queue (never dropped by budget) and are always
    surfaced (their relevance score is shown as a label, never a filter).

    This is the OpenAlex-native pull; it's complemented by
    watchlist_crossmatch() which tags collected items from OTHER sources whose
    author matches the roster (OpenAlex lags on fresh working papers)."""
    roster = _load_roster()
    if not roster:
        log("[watchlist] no/empty roster; skipped")
        return []
    # deep-pull only a rotating slice this run (date-based, stateless) -- the
    # full cross-match still runs every run, so nothing from a normal source is
    # missed; this just spreads the slow per-author API calls across runs
    slice_ = roster
    per = getattr(config, "WATCHLIST_PER_RUN", 0)
    if per and len(roster) > per:
        import math as _math
        ids = list(roster.items())
        n = _math.ceil(len(ids) / per)
        idx = (dt.date.today().toordinal() // 3) % n     # advances every ~3 days
        slice_ = dict(ids[idx * per:(idx + 1) * per])
        log(f"[watchlist] rotating slice {idx + 1}/{n}: {len(slice_)} of "
            f"{len(roster)} authors this run")
    since = (dt.date.today()
             - dt.timedelta(days=config.WATCHLIST_LOOKBACK_DAYS)).isoformat()
    out, hit_authors = [], 0
    for aid, meta in slice_.items():
        name = meta.get("name", aid)
        try:
            r = _openalex_get("https://api.openalex.org/works", {
                "filter": f"author.id:{aid},from_publication_date:{since}",
                "per-page": config.WATCHLIST_MAX_PER_AUTHOR,
                "sort": "publication_date:desc",
            }, log, retries=1)                         # fail fast; don't wedge the run
        except Exception as e:                         # noqa: BLE001
            log(f"[watchlist] OpenAlex '{name}' skipped ({type(e).__name__})")
            continue
        works = r.json().get("results", [])
        if works:
            hit_authors += 1
        for w in works:
            it = _oa_item(w, "watchlist", 2)
            it["watchlist"] = True
            it["watchlist_author"] = name
            out.append(it)
        time.sleep(0.25)                               # polite spacing
    log(f"[watchlist] OpenAlex: {len(out)} works from {hit_authors}/{len(slice_)} "
        f"watched authors (slice) since {since}")

    # ...then the Crossref deep pull -- covers SSRN + journals + working papers
    # that OpenAlex hasn't indexed yet (this is what catches e.g. Kelly's AIPM),
    # on the SAME rotated slice
    try:
        cr = _watchlist_crossref(slice_, log)
    except Exception as e:                             # noqa: BLE001
        log(f"[watchlist] Crossref pull failed: {type(e).__name__}: {e}")
        cr = []
    return out + cr


def _watchlist_crossref(roster: dict, log) -> list[dict]:
    """For each watched author, query Crossref by name over a long window and
    keep only works whose author (first,last) key matches AND whose title looks
    like finance -- the two gates together strip the common-name noise Crossref
    returns while catching OpenAlex-lagged papers (SSRN/NBER working papers)."""
    since = (dt.date.today()
             - dt.timedelta(days=config.WATCHLIST_CROSSREF_DAYS)).isoformat()
    out, hit = [], 0
    for meta in roster.values():
        name = meta.get("name", "")
        key = _name_key(name)
        if not key:
            continue
        # default RELEVANCE sort (no sort=published) -- it surfaces THIS
        # author's own works near the top; sorting by date instead fills the
        # page with recent papers by unrelated same-name authors
        params = {"query.author": name, "filter": f"from-pub-date:{since}",
                  "rows": config.WATCHLIST_CROSSREF_ROWS,
                  "select": "title,author,container-title,abstract,URL,DOI,created"}
        if MAILTO:
            params["mailto"] = MAILTO
        try:
            r = requests.get("https://api.crossref.org/works", params=params,
                             headers=UA, timeout=60)
            r.raise_for_status()
            works = r.json()["message"]["items"]
        except Exception as e:                         # noqa: BLE001
            log(f"[watchlist] Crossref '{name}' failed: {type(e).__name__}")
            continue
        found = False
        for w in works:
            if not _cr_author_matches(w, key):
                continue
            title = _clean(" ".join(w.get("title") or []))
            abstract = _clean(w.get("abstract", ""))
            if not _nber_is_finance(title, abstract):  # reuse the finance gate
                continue
            it = _crossref_item(w, "watchlist")
            if not it:
                continue
            it["section"] = 2
            it["watchlist"] = True
            it["watchlist_author"] = name
            out.append(it)
            found = True
        hit += int(found)
        time.sleep(0.3)                                # Crossref polite spacing
    log(f"[watchlist] Crossref: {len(out)} works from {hit}/{len(roster)} "
        f"watched authors since {since}")
    return out


def _cr_author_matches(work: dict, key: tuple[str, str]) -> bool:
    first, last = key
    for a in work.get("author") or []:
        af = (a.get("given", "") or "").lower().split()
        al = (a.get("family", "") or "").lower()
        if al == last and af and (af[0] == first
                                  or (len(first) == 1 and af[0].startswith(first))
                                  or (len(af[0]) == 1 and first.startswith(af[0]))):
            return True
    return False


def _nber_is_finance(title: str, abstract: str) -> bool:
    text = (title + " " + abstract).lower()
    return any(t in text for t in config.NBER_FINANCE_TERMS)


# --------------------------------------- OpenAlex topic sweep
_FIN_FIELD = "Economics, Econometrics and Finance"


def resolve_topics(log) -> tuple[dict, list]:
    """Bootstrap: map each seed to a taxonomy topic behind a finance gate, or
    route it to fulltext. Returns ({display_name: topic_id}, [fulltext terms]).
    Only runs when config.OPENALEX_TOPIC_IDS is empty (re-bootstrap)."""
    mapped, fulltext = {}, []
    for term in config.TOPIC_SEARCH_TERMS:
        try:
            r = _openalex_get("https://api.openalex.org/topics",
                              {"search": term, "per-page": 1}, log)
        except Exception as e:                       # noqa: BLE001
            print(f"  topic '{term}': lookup failed ({type(e).__name__}); fulltext")
            fulltext.append(term)
            continue
        res = r.json().get("results", [])
        top = res[0] if res else {}
        field = (top.get("field") or {}).get("display_name")
        score = top.get("relevance_score") or 0
        if top and field == _FIN_FIELD and score >= config.OPENALEX_TOPIC_MIN_SCORE:
            name, tid = top.get("display_name"), top["id"].rsplit("/", 1)[-1]
            mapped[name] = tid
            print(f"  topic '{term}' -> {tid} ('{name}') MAPPED")
        else:
            fulltext.append(term)
            print(f"  topic '{term}' -> FULLTEXT "
                  f"(top={top.get('display_name')!r} field={field!r} score={score})")
        time.sleep(0.25)
    log(f"[topics] bootstrap: {len(mapped)} mapped, {len(fulltext)} fulltext "
        f"-- hardcode mapped into config.OPENALEX_TOPIC_IDS: {mapped}")
    return mapped, fulltext


def openalex_topics(log) -> list[dict]:
    """Topic sweep. Runs last so dedup keeps richer records canonical and this
    contributes only net-new items (section 5). Branch A = batched taxonomy
    topics; branch B = precise fulltext search per unmapped seed."""
    if config.OPENALEX_TOPIC_IDS:
        topic_ids = list(config.OPENALEX_TOPIC_IDS.values())
        fulltext = list(config.OPENALEX_FULLTEXT_TERMS)
    else:
        mapped, fulltext = resolve_topics(log)
        topic_ids = list(mapped.values())

    since = _cutoff().date().isoformat()
    fin_a = f"primary_topic.field.id:{config.OPENALEX_FINANCE_FIELD}"      # econ+fin
    fin_b = f"primary_topic.subfield.id:{config.OPENALEX_FULLTEXT_SUBFIELD}"  # finance
    out = []

    # Branch A -- taxonomy-mapped topics, one batched call
    if topic_ids:
        try:
            r = _openalex_get("https://api.openalex.org/works", {
                "filter": f"topics.id:{'|'.join(topic_ids)},"
                          f"from_publication_date:{since},{fin_a}",
                "per-page": 100,
                "sort": "publication_date:desc",
            }, log)
            got = [_oa_item(w, "topic-sweep", 5)
                   for w in r.json().get("results", [])]
            print(f"  topics/branchA: {len(got)} works ({len(topic_ids)} topics)")
            out += got
        except Exception as e:                       # noqa: BLE001
            log(f"[topics] branch-A failed: {type(e).__name__}: {e}")

    # Branch B -- unmapped seeds via fulltext search, one call per term
    for term in fulltext:
        try:
            r = _openalex_get("https://api.openalex.org/works", {
                "search": term,
                "filter": f"from_publication_date:{since},type:article,{fin_b}",
                "per-page": config.OPENALEX_FULLTEXT_LIMIT,
                "sort": "publication_date:desc",
            }, log)
            out += [_oa_item(w, f"topic:{term}", 5)
                    for w in r.json().get("results", [])]
        except Exception as e:                       # noqa: BLE001
            log(f"[topics] fulltext '{term}' failed: {type(e).__name__}: {e}")
        time.sleep(0.3)
    print(f"  topics/branchB: {len(fulltext)} fulltext seeds")
    return out


# --------------------------------------- Semantic Scholar (preprints)
def semantic_scholar(log) -> list[dict]:
    since = _cutoff().date().isoformat()
    out = []
    for q in config.SEMANTIC_SCHOLAR_QUERIES:
        # RUN-1 VERIFY: parameter name publicationDateOrYear and the open-ended
        # "date:" range syntax, per api.semanticscholar.org/api-docs
        r = None
        for attempt in range(config.S2_MAX_RETRIES):
            r = requests.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": q,
                        "publicationDateOrYear": f"{since}:",
                        "fields": "title,authors,abstract,externalIds,url,"
                                  "publicationDate,venue",
                        "limit": 40},
                headers=UA, timeout=60)
            if r.status_code != 429:
                break
            # best-effort: only wait if another attempt remains
            if attempt + 1 < config.S2_MAX_RETRIES:
                wait = 30 * (attempt + 1)
                log(f"[s2] rate limited on '{q}'; retry {attempt + 1}/"
                    f"{config.S2_MAX_RETRIES} after {wait}s")
                time.sleep(wait)
        if r is None or r.status_code == 429:
            log(f"[s2] '{q}' rate limited; skipped this week (best-effort)")
            continue
        r.raise_for_status()
        for p in r.json().get("data", []):
            ext = p.get("externalIds") or {}
            out.append({
                "title": _clean(p.get("title", "")),
                "authors": ", ".join(a["name"]
                                     for a in (p.get("authors") or [])[:4]),
                "abstract": _clean(p.get("abstract") or "")[:800],
                "url": p.get("url", ""),
                "date": p.get("publicationDate") or "",
                "source": "semantic-scholar",
                "section": 3,
                "doi": ext.get("DOI"),
                "arxiv_id": ext.get("ArXiv"),
            })
        time.sleep(1.2)  # unauthenticated S2 etiquette
    return out


# --------------------------------------- abstract enrichment (post-collect)
def enrich_abstracts(items: list[dict], log) -> list[dict]:
    """Enrich DOI-bearing items via Semantic Scholar (one batched call): fills a
    missing abstract AND attaches the paper citation count + author h-index that
    the monthly composite needs. Fallback: scrape the journal article page's meta
    tags for any abstract S2 still lacks (bounded). Best-effort; mutates in
    place. OpenAlex is intentionally not used here."""
    import scoring
    scoring.attach_s2(items, log)     # abstract + cites + author_h

    scraped = 0
    for it in items:
        if scraped >= config.ENRICH_SCRAPE_CAP:
            break
        if it.get("abstract") or not it.get("doi"):
            continue
        if not str(it.get("source", "")).startswith("journal:"):
            continue
        ab = _scrape_abstract(it.get("url", ""))
        if ab:
            it["abstract"] = ab
            scraped += 1
        time.sleep(0.4)

    filled = sum(1 for it in items if it.get("abstract"))
    log(f"[enrich] S2 + {scraped} page-scraped; {filled}/{len(items)} have abstracts")
    return items
