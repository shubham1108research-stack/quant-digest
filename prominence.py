"""Best-effort author-citation prominence via OpenAlex.

For items carrying OpenAlex author ids (native OpenAlex works -- the preprint
probes and the topic sweep), fetch each author's h-index + citation count and
attach the most-prominent author's numbers. Journal/arXiv items usually lack
author ids in OpenAlex, so they rely on their journal tier instead; this is an
enrichment signal, not a gate, and any failure degrades silently.
"""

import time

import requests

import config
import oa as oa_auth   # noqa: E402

MAILTO = None  # set from main (mirrors sources.MAILTO)
_UA = {"User-Agent": "quant-digest/1.0 (personal research tool)"}


def annotate(items: list[dict], log) -> list[dict]:
    ids = set()
    for it in items:
        ids.update(it.get("oa_author_ids") or [])
    if not ids:
        log("[prominence] no OpenAlex author ids on this batch; skipped")
        return items

    ids = list(ids)
    stats: dict[str, tuple[int, int, str]] = {}   # id -> (h_index, cites, name)
    b = 50                                         # OpenAlex OR-filter batch cap
    for start in range(0, len(ids), b):
        params = {
            "filter": "ids.openalex:" + "|".join(ids[start:start + b]),
            "select": "id,display_name,cited_by_count,summary_stats",
            "per-page": b,
        }
        if MAILTO:
            params["mailto"] = MAILTO
        try:
            r = requests.get("https://api.openalex.org/authors",
                             params=params, headers=oa_auth.headers(_UA), timeout=60)
            r.raise_for_status()
        except Exception as e:                     # noqa: BLE001
            log(f"[prominence] author batch failed: {type(e).__name__}: {e}")
            continue
        for a in r.json().get("results", []):
            aid = a["id"].rsplit("/", 1)[-1]
            h = (a.get("summary_stats") or {}).get("h_index") or 0
            stats[aid] = (int(h), int(a.get("cited_by_count") or 0),
                          a.get("display_name", ""))
        time.sleep(0.3)

    annotated = 0
    for it in items:
        best = None
        for aid in (it.get("oa_author_ids") or []):
            s = stats.get(aid)
            if s and (best is None or s[0] > best[0]):
                best = s
        if best:
            it["prom_hindex"], it["prom_cites"], it["prom_author"] = best
            annotated += 1
            # scoring.author_score reads author_h and falls back to a neutral
            # 50.0 when it is None -- so 643 papers with a KNOWN h-index were
            # scored at the prior. These are exactly the DOI-less OpenAlex
            # items the S2 path cannot reach, which is what this module is for.
            if it.get("author_h") is None:
                it["author_h"] = best[0]
    log(f"[prominence] annotated {annotated}/{len(items)} items (top-author h-index)")
    return items
