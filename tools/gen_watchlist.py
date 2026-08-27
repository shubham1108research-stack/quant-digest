#!/usr/bin/env python3
"""Build/refresh docs/watchlist.json -- the author roster P2 tracks.

Three pools feed the roster (all resolved to OpenAlex author ids, so matching
is by id, never by ambiguous name):

  seed   -- config.WATCHLIST_SEED (hand-curated ~50 names + disambiguation hint)
  canon  -- first authors of the seminal papers in docs/classics.json (folded in
            automatically; deceased/inactive ones just return no recent papers
            from the daily collector, so they're harmless no-ops)
  auto   -- anyone who recurred in our OWN archive (state.db) last quarter above
            config.WATCHLIST_PROMOTE_MIN_* (the archive teaches the roster)

For each resolved author it also records venue mix / volume / impact (h-index,
recent citations) as a snapshot, so you can see WHERE they publish, HOW MUCH,
and their IMPACT. Run quarterly (see .github/workflows/watchlist-refresh.yml):

    python tools/gen_watchlist.py

Deliberately a generator, not part of the daily run: name->id resolution and
per-author metrics are OpenAlex-heavy, and the roster only needs refreshing
every ~90 days. The daily collector (sources.watchlist) just reads the file.
"""

import json
import os
import sys
import time
from collections import Counter

import requests
import oa   # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config    # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "watchlist.json")
CLASSICS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "classics.json")
_UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}
_MAILTO = os.environ.get("CONTACT_EMAIL") or os.environ.get("GMAIL_ADDRESS")
_ECON = {"economics", "finance", "financial economics", "econometrics",
         "monetary economics", "actuarial science", "mathematical economics"}


def _get(url, params):
    if _MAILTO:
        params = {**params, "mailto": _MAILTO}
    for attempt in range(4):
        try:
            r = requests.get(url, params=params,
                             headers=oa.headers(_UA), timeout=45)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:                              # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    return None


_ECON_TOP = {"economics", "finance", "financial economics", "econometrics",
             "business", "monetary economics", "actuarial science"}


def _is_econ(cand: dict) -> bool:
    """A candidate counts as the finance/econ author only if one of its TOP
    concepts is economics/finance/business -- NOT merely that an econ word
    appears somewhere. Names like 'Bryan Kelly' also match a hip surgeon with
    huge output; the top-concept gate is what excludes them."""
    top = [c.get("display_name", "").lower() for c in cand.get("x_concepts", [])[:3]]
    return any(t in _ECON_TOP for t in top)


def _rank(cand: dict, hint: str) -> float:
    """Among econ-qualified candidates, prefer higher h-index (the person's
    'main' OpenAlex profile; fragmented duplicates have lower h), with a small
    hint/institution nudge to break ties."""
    ss = cand.get("summary_stats") or {}
    inst = (cand.get("last_known_institution") or {}).get("display_name", "").lower()
    concepts = " ".join(c.get("display_name", "").lower()
                        for c in cand.get("x_concepts", [])[:6])
    hint_words = [w for w in hint.lower().split() if len(w) > 3]
    nudge = sum(1 for w in hint_words if w in concepts or w in inst)
    return (ss.get("h_index") or 0) + nudge * 2


def resolve(name: str, hint: str) -> dict | None:
    data = _get("https://api.openalex.org/authors",
                {"search": name, "per_page": 10})
    cands = [c for c in ((data or {}).get("results") or []) if _is_econ(c)]
    if not cands:
        return None
    return max(cands, key=lambda c: _rank(c, hint))


def metrics(author_id: str) -> dict:
    """Venue mix / volume / impact snapshot from the author's recent works."""
    data = _get(f"https://api.openalex.org/works",
                {"filter": f"author.id:{author_id},"
                           f"from_publication_date:{_year_ago()}",
                 "per_page": 100, "select": "primary_location,publication_year"})
    works = (data or {}).get("results") or []
    venues = Counter()
    for w in works:
        src = ((w.get("primary_location") or {}).get("source") or {})
        venues[src.get("display_name") or "working paper / preprint"] += 1
    return {"volume_1y": len(works), "venue_mix": dict(venues.most_common(6))}


def _year_ago() -> str:
    # avoid Date.now-style nondeterminism concerns; plain date arithmetic is fine
    import datetime as dt
    return (dt.date.today() - dt.timedelta(days=365)).isoformat()


def _this_quarter() -> str:
    import datetime as dt
    t = dt.date.today()
    return f"{t.year}-Q{(t.month - 1) // 3 + 1}"


def canon_authors() -> dict:
    """Resolve the CURATED seminal canon (canon.CANON, ~96 papers) to OpenAlex
    author ids -- PRECISELY, by searching each paper's title and matching the
    authorship whose surname equals the canon's author_hint. Bare-surname
    search ('Chen', 'Li') is hopeless; the paper disambiguates. Returns
    {openalex_id: display_name} for the living/resolvable ones. Deceased or
    unresolvable authors just don't appear (and would return no recent papers
    anyway). Uses the curated canon, NOT classics.json's most-cited dump."""
    import canon                                       # noqa: E402
    seen_hints, out = set(), {}
    for _topic, papers in canon.CANON.items():
        for (title, hint, _year, _typ, _why) in papers:
            if hint in seen_hints:                     # one lookup per surname
                continue
            seen_hints.add(hint)
            data = _get("https://api.openalex.org/works",
                        {"search": title, "per_page": 3,
                         "select": "authorships,title"})
            for w in (data or {}).get("results", []):
                for a in w.get("authorships") or []:
                    au = a.get("author") or {}
                    dn = (au.get("display_name") or "")
                    if hint.lower() in dn.lower() and au.get("id"):
                        out[au["id"].rsplit("/", 1)[-1]] = dn
                        break
                else:
                    continue
                break
            time.sleep(0.2)
    return out


def auto_promoted() -> list[str]:
    """First authors who recurred in state.db above the promotion bar this
    quarter -- returned as names to resolve (they may already be seed/canon)."""
    import sqlite3
    try:
        con = sqlite3.connect(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state.db"))
    except Exception:                                  # noqa: BLE001
        return []
    counts, composites = Counter(), {}
    # composite lives in monthly.json entries, not items; approximate promotion
    # off archive recurrence at high relevance posterior instead (composite
    # isn't stored per-item). >=MIN_PAPERS appearances at core relevance.
    hi = Counter()
    for (meta,) in con.execute("SELECT meta FROM items"):
        try:
            m = json.loads(meta)
        except Exception:                              # noqa: BLE001
            continue
        if m.get("relevance_category") != "core_fit":
            continue
        auth = (m.get("authors") or "").split(",")[0].strip()
        if auth:
            hi[auth] += 1
    return [a for a, n in hi.items() if n >= config.WATCHLIST_PROMOTE_MIN_PAPERS]


def main() -> None:
    existing = {}
    if os.path.exists(OUT):
        try:
            existing = json.load(open(OUT, encoding="utf-8")).get("authors", {})
        except Exception:                              # noqa: BLE001
            existing = {}

    roster = {}

    def add(aid: str, name: str, source: str, author_obj: dict | None):
        if aid in roster:                              # already added via higher pool
            return
        prior = existing.get(aid, {})
        ss = (author_obj or {}).get("summary_stats") or {}
        roster[aid] = {
            "name": name,
            "source": prior.get("source", source),     # keep original source label
            "added": prior.get("added", _this_quarter()),
            "h_index": ss.get("h_index"),
            "recent_cites": ss.get("2yr_cited_by_count"),
            **metrics(aid),
            "last_refreshed": _this_quarter(),
        }
        print(f"  [ok] {name} -> {aid} ({source}, "
              f"h={roster[aid]['h_index']}, vol={roster[aid]['volume_1y']})")
        time.sleep(0.2)

    # 1) seed -- resolve name -> id (with hint disambiguation + econ gate)
    for s in config.WATCHLIST_SEED:
        a = resolve(s["name"], s.get("hint", ""))
        if not a:
            print(f"  [skip] could not resolve seed: {s['name']}")
            continue
        add(a["id"].rsplit("/", 1)[-1], a.get("display_name") or s["name"],
            "seed", a)

    # 2) canon -- already resolved to ids via paper-title match
    print("resolving curated canon authors via paper titles...")
    for aid, dn in canon_authors().items():
        add(aid, dn, "canon", None)

    # 3) auto -- archive recurrence -> resolve name -> id
    for name in auto_promoted():
        a = resolve(name, name.split()[-1])
        if a:
            add(a["id"].rsplit("/", 1)[-1], a.get("display_name") or name,
                "auto", a)

    json.dump({"generated": _this_quarter(), "authors": roster},
              open(OUT, "w", encoding="utf-8"), indent=1)
    print(f"\nwrote {OUT}: {len(roster)} authors")


if __name__ == "__main__":
    main()
