"""Collectors. Each returns list[dict] with keys:
title, authors, abstract, url, date, source, section, plus doi/arxiv_id when known.
Every collector raises on hard failure -- main.py catches per-source so one
dead feed never kills the run.
"""

import datetime as dt
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
    return " ".join(re.sub(r"<[^>]+>", " ", s or "").split())


def _entry_date(e) -> dt.datetime | None:
    for k in ("published_parsed", "updated_parsed"):
        if getattr(e, k, None):
            return dt.datetime(*getattr(e, k)[:6], tzinfo=dt.timezone.utc)
    return None


# ---------------------------------------------------------------- NEP
def nep() -> list[dict]:
    out = []
    for code in config.NEP_CODES:
        feed = feedparser.parse(config.NEP_URL.format(code=code))
        for e in feed.entries:
            out.append({
                "title": _clean(e.get("title", "")),
                "authors": "",
                "abstract": _clean(e.get("description", "") or e.get("summary", "")),
                "url": e.get("link", ""),
                "date": (d := _entry_date(e)) and d.date().isoformat() or "",
                "source": f"nep-{code}",
                "section": 1,
            })
        time.sleep(0.5)
    return out


# --------------------------------------------------------------- NBER
def nber() -> list[dict]:
    feed = feedparser.parse(config.NBER_RSS)
    cut = _cutoff()
    out = []
    for e in feed.entries:
        d = _entry_date(e)
        if d and d < cut:
            continue
        out.append({
            "title": _clean(e.get("title", "")),
            "authors": _clean(e.get("author", "")),
            "abstract": _clean(e.get("description", "") or e.get("summary", "")),
            "url": e.get("link", ""),
            "date": d.date().isoformat() if d else "",
            "source": "nber",
            "section": 1,
        })
    return out


# -------------------------------------------------------------- arXiv
def arxiv() -> list[dict]:
    query = " OR ".join(f"cat:{c}" for c in config.ARXIV_CATS)
    r = requests.get(config.ARXIV_API, params={
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": config.ARXIV_MAX,
    }, headers=UA, timeout=60)
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
        "date": "-".join(str(x) for x in
                         (w.get("created", {}).get("date-parts") or [[""]])[0]),
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
            "sort": "created", "order": "desc",
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
def _openalex_get(url: str, params: dict, log) -> requests.Response:
    """GET with linear backoff on a genuine 429 rate limit.

    NOTE: OpenAlex also returns 429 for a *paywalled feature* (e.g. the
    from_created_date filter needs a paid plan) -- that is not a rate limit
    and must NOT be retried, so we detect the 'plan upgrade' body and fail
    fast instead of burning the full backoff budget."""
    if MAILTO:
        params = {**params, "mailto": MAILTO}
    last = None
    for i in range(config.OPENALEX_MAX_RETRIES):
        r = requests.get(url, params=params, headers=UA, timeout=60)
        last = r
        if r.status_code != 429:
            r.raise_for_status()
            return r
        if "upgrade required" in r.text.lower() or "paid plan" in r.text.lower():
            log(f"[openalex] paywalled request, not retrying: {r.text[:160]}")
            r.raise_for_status()          # raises -- caller logs & skips source
        wait = 5 * (i + 1)
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
        "abstract": "",  # OpenAlex abstracts are inverted-index; skip
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
