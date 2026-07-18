"""One-time historical backfill: the most-cited finance papers (1970 - Jun 2026).

Pulls the top-N most-cited works in OpenAlex's Finance subfield, reconstructs
abstracts, LLM-classifies them (score + summary), and writes docs/classics.json
-- the data behind the portal's permanent "Classics" section. Run once (and
re-run any time to refresh, e.g. once an LLM key/quota is available):

    python backfill.py

Commit docs/classics.json and it ships with the portal forever.
"""

import json
import os
import pathlib
import time

import requests

import config
import llm

_UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}
_N = 400                     # how many all-time most-cited finance papers to keep
_FILTER = ("primary_topic.subfield.id:2003,"          # OpenAlex Finance subfield
           "from_publication_date:1970-01-01,"
           "to_publication_date:2026-06-30,type:article")


def _abstract(w: dict) -> str:
    inv = w.get("abstract_inverted_index")
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))[:1500]


def _item(w: dict) -> dict:
    auths = w.get("authorships") or []
    loc = w.get("primary_location") or {}
    return {
        "title": (w.get("title") or "").strip(),
        "authors": ", ".join(a["author"]["display_name"] for a in auths[:4]),
        "url": loc.get("landing_page_url") or w.get("id", ""),
        "journal": (loc.get("source") or {}).get("display_name", ""),
        "year": w.get("publication_year"),
        "cited_by_count": w.get("cited_by_count") or 0,
        "abstract": _abstract(w),
        "source": "classic",
        "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
    }


def fetch(log) -> list[dict]:
    out, cursor = [], "*"
    mailto = os.environ.get("CONTACT_EMAIL") or os.environ.get("GMAIL_ADDRESS")
    while len(out) < _N:
        params = {
            "filter": _FILTER, "sort": "cited_by_count:desc",
            "per-page": 200, "cursor": cursor,
            "select": "id,doi,title,publication_year,cited_by_count,"
                      "authorships,primary_location,abstract_inverted_index",
        }
        if mailto:
            params["mailto"] = mailto
        r = requests.get("https://api.openalex.org/works", params=params,
                         headers=_UA, timeout=60)
        r.raise_for_status()
        j = r.json()
        res = j.get("results") or []
        if not res:
            break
        out += [_item(w) for w in res]
        log(f"  fetched {len(out)}")
        cursor = (j.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.3)
    return out[:_N]


def main() -> None:
    def log(m):
        print(m)

    papers = fetch(log)
    log(f"fetched {len(papers)} most-cited finance papers (1970-2026)")

    llm.rank(papers, log)    # attaches rank_score + summary (best-effort)

    data = [{
        "title": p["title"], "url": p["url"], "authors": p["authors"],
        "journal": p["journal"], "year": p["year"],
        "cites": p["cited_by_count"], "score": p.get("rank_score"),
        "summary": p.get("summary", ""),
    } for p in papers]

    docs = pathlib.Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "classics.json").write_text(json.dumps(data, default=str),
                                        encoding="utf-8")
    got = sum(1 for p in papers if p.get("summary"))
    log(f"wrote docs/classics.json ({len(data)} papers, {got} LLM-summarised)")


if __name__ == "__main__":
    main()
