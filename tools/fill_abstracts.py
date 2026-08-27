#!/usr/bin/env python3
"""Backfill abstracts onto archived rows that were collected without one.

Measured recovery on a random 100-row sample of the gap:
    OpenAlex          25/100
    Semantic Scholar  10/100
    front matter      19/100  (editorials, announcements, author indexes --
                               these correctly have no abstract and are skipped)

So OpenAlex leads and S2 backs it up, rather than the other way round. Roughly a
quarter of the gap is recoverable; much of the rest is genuine absence, where no
source holds an abstract for that DOI. That is worth knowing rather than
retrying forever.

OpenAlex returns an inverted index ({word: [positions]}) instead of prose, so
the text has to be reassembled from it.

  python tools/fill_abstracts.py --limit 200   # trial
  python tools/fill_abstracts.py               # whole gap
"""

import argparse
import json
import pathlib
import re
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import scoring  # noqa: E402
import store    # noqa: E402
import oa as oa_auth   # noqa: E402

MAILTO = "upadhyays1108@gmail.com"
UA = {"User-Agent": f"quant-digest/1.0 (mailto:{MAILTO})"}
OA_BATCH = 50       # OpenAlex OR-filter limit
S2_BATCH = 200      # S2 batch endpoint accepts up to 500; smaller is politer
MIN_WORDS = 20      # below this it is a fragment, not an abstract


def log(m):
    print(m, flush=True)


def deinvert(inv):
    """OpenAlex stores abstracts as {word: [positions]}. Rebuild the prose."""
    if not inv:
        return ""
    slots = {}
    for word, positions in inv.items():
        for p in positions:
            slots[p] = word
    if not slots:
        return ""
    return " ".join(slots[i] for i in sorted(slots))


def from_openalex(dois):
    out = {}
    for i in range(0, len(dois), OA_BATCH):
        chunk = dois[i:i + OA_BATCH]
        try:
            r = requests.get("https://api.openalex.org/works",
                             params={"filter": "doi:" + "|".join(chunk),
                                     "select": "doi,abstract_inverted_index",
                                     "per-page": OA_BATCH, "mailto": MAILTO},
                             headers=oa_auth.headers(UA), timeout=60)
            if not r.ok:
                log(f"  [openalex] HTTP {r.status_code}")
                continue
            for w in r.json().get("results", []):
                doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
                text = deinvert(w.get("abstract_inverted_index"))
                if doi and len(text.split()) >= MIN_WORDS:
                    out[doi] = text
        except Exception as e:                          # noqa: BLE001
            log(f"  [openalex] {type(e).__name__}")
        time.sleep(1.0)
    return out


def from_s2(dois):
    out = {}
    for i in range(0, len(dois), S2_BATCH):
        chunk = dois[i:i + S2_BATCH]
        for attempt in range(5):
            try:
                r = requests.post(
                    "https://api.semanticscholar.org/graph/v1/paper/batch",
                    params={"fields": "abstract,externalIds"},
                    json={"ids": ["DOI:" + d for d in chunk]},
                    headers=UA, timeout=90)
            except Exception:                           # noqa: BLE001
                break
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            if r.ok:
                for j, x in enumerate(r.json()):
                    if not x:
                        continue
                    text = (x.get("abstract") or "").strip()
                    if len(text.split()) >= MIN_WORDS:
                        out[chunk[j].lower()] = text
            break
        time.sleep(2)
    return out


def from_crossref(dois):
    """{doi: abstract} from Crossref, one request per DOI.

    THIRD, not first, and worth the per-DOI cost only because of what it
    covers. OpenAlex and S2 are batch endpoints and cheap; Crossref has no
    batch lookup for a specific DOI list, so this is one round trip each and
    only ever sees what the other two could not find.

    What it adds is preprints. Crossref indexes an SSRN posting the same day it
    appears; OpenAlex lags by weeks and S2 often never has it. Since SSRN is one
    of this archive's main sources, that lag is exactly where the gap lives.
    Crossref abstracts are JATS-wrapped, so they go through the collector's own
    cleaner rather than a second copy of it.
    """
    out = {}
    for i, doi in enumerate(dois, 1):
        try:
            r = requests.get("https://api.crossref.org/works/" + doi,
                             params={"mailto": MAILTO}, headers=UA, timeout=25)
            if r.status_code != 200:
                continue
            raw = ((r.json() or {}).get("message") or {}).get("abstract") or ""
            if not raw:
                continue
            try:
                import sources
                text = sources._clean(raw)
            except Exception:                           # noqa: BLE001
                text = re.sub(r"<[^>]+>", " ", raw).strip()
            if len(text.split()) >= MIN_WORDS:
                out[doi.lower()] = text[:6000]
        except Exception:                               # noqa: BLE001
            pass
        if i % 50 == 0:
            log(f"[abs] crossref {i}/{len(dois)}")
        time.sleep(0.25)                                # polite pool
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    con = store.connect()
    gap, junk = [], 0
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        if not uid.startswith("doi:"):
            continue
        try:
            d = json.loads(meta)
        except Exception:                               # noqa: BLE001
            d = {}
        if (d.get("abstract") or "").strip():
            continue
        # editorials, announcements and author indexes have no abstract to find
        if scoring.is_junk(title):
            junk += 1
            continue
        gap.append(uid[4:])
    log(f"[abs] gap: {len(gap)} rows (skipped {junk} front-matter rows)")
    if args.limit:
        gap = gap[:args.limit]
        log(f"[abs] limited to {len(gap)}")

    found = from_openalex(gap)
    log(f"[abs] OpenAlex recovered {len(found)}")
    still = [d for d in gap if d.lower() not in found]
    if still:
        s2 = from_s2(still)
        log(f"[abs] Semantic Scholar recovered {len(s2)} more")
        found.update(s2)
    # Crossref last: one request per DOI rather than a batch, so it only ever
    # runs on what the two batch sources could not supply. It is here because
    # it is the only one of the three that has fresh preprints -- an SSRN
    # posting is in Crossref the day it appears and in OpenAlex weeks later.
    still = [d for d in gap if d.lower() not in found]
    if still:
        cr = from_crossref(still)
        log(f"[abs] Crossref recovered {len(cr)} more")
        found.update(cr)

    patched, uids = 0, []
    for doi, text in found.items():
        uid = "doi:" + doi
        if store.update_meta(con, uid,
                             {"abstract": text, "abstract_source": "backfill"}):
            patched += 1
            uids.append(uid)

    # The embedding cache is keyed by (uid, model, dim) and knows nothing about
    # the TEXT changing underneath it, so a backfilled paper would keep its old
    # title-only vector forever -- the abstract would show on the page but never
    # reach retrieval. Drop those rows so the next embed run recomputes them.
    if uids:
        try:
            con.executemany("DELETE FROM embeddings WHERE uid=?",
                            [(u,) for u in uids])
            log(f"[abs] invalidated {len(uids)} cached vectors for re-embedding")
        except Exception:                               # noqa: BLE001
            pass                                        # table may not exist yet
    con.commit()
    log(f"\n[abs] patched {patched}/{len(gap)} rows "
        f"({100.0 * patched / max(1, len(gap)):.0f}% of the attempted gap)")

    total, have = 0, 0
    for (meta,) in con.execute("SELECT meta FROM items"):
        total += 1
        try:
            d = json.loads(meta)
        except Exception:                               # noqa: BLE001
            d = {}
        if (d.get("abstract") or "").strip():
            have += 1
    log(f"[abs] archive coverage now {have}/{total} = {100.0*have/total:.0f}%")


if __name__ == "__main__":
    main()
