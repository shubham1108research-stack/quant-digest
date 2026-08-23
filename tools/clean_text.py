#!/usr/bin/env python3
"""One-off: re-clean archived abstracts, and drop papers from other disciplines.

Two problems the knowledge map surfaced that nothing else had. One of its 24
clusters came back labelled "risk, nbsp, span" -- the model was clustering on
HTML -- and another was "high, performance, temperature", which is not a thing
a quant archive should contain.

1. MARKUP. sources._clean stripped tags but never unescaped entities, so 1,130
   abstracts carried "S&amp;P 500" and literal nbsp into the embeddings and
   into the LLM prompt. The collector is fixed; this repairs what was stored.

2. OFF-DISCIPLINE. SSRN hosts every field, and the Crossref query used
   sort=created -- which makes Crossref ignore query relevance entirely and
   return the newest SSRN papers of any kind. The finance queries were
   decoration. That is fixed too (sort=relevance, with from-created-date still
   bounding recency); this removes what already arrived.

Papers are only dropped when they are unambiguously another discipline AND
carry no LLM relevance score saying otherwise -- a deletion cannot be undone
from here, and state.db is the dedup memory, so a wrong drop means the paper
comes back next run.

  python tools/clean_text.py --dry-run
  python tools/clean_text.py
"""

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import sources   # noqa: E402  (the fixed _clean)
import store     # noqa: E402

# Vocabulary with no finance reading whatsoever. Deliberately narrow: "carbon"
# and "energy" are excluded because commodities research uses both.
OFF_FIELD = (
    "activated sludge", "polyvinyl", "nanomaterial", "nanoparticle",
    "photocatal", "wastewater", "graphene", "adsorption isotherm",
    "antibacterial", "electrode", "biosynthesis", "crystallin",
    "microbial", "chromatograph", "in vitro", "in vivo", "polymer composite",
    "thin film", "spectroscop",
)
# "catalyst" was in the list above and flagged 8 papers, one of them
# "The contagion effect: time-varying volatility spillovers..." -- a finance
# paper using the word the way finance writing always does. Removed.

# A veto, because a deletion cannot be undone and state.db is the dedup memory:
# no paper is dropped if it also speaks finance, however chemical it sounds.
FINANCE = (
    "return", "volatil", "portfolio", "asset", "market", "price", "risk",
    "investor", "equity", "bond", "yield", "trading", "hedge", "premium",
    "arbitrage", "liquidity", "credit", "monetary", "inflation", "exchange rate",
    "stock", "futures", "option", "capital", "firm value", "earnings",
)


def log(m):
    print(m, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    con = store.connect()

    recleaned, drops = [], []
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            d = json.loads(meta)
        except Exception:                              # noqa: BLE001
            continue
        abstract = d.get("abstract") or ""
        blob = f"{title or ''} {abstract}".lower()

        if (any(k in blob for k in OFF_FIELD)
                and not any(f in blob for f in FINANCE)
                and not d.get("relevance_category")):
            drops.append((uid, (title or "")[:66]))
            continue

        # TITLES too. They are embedded alongside the abstract (embed.py's
        # _text is title + topic + abstract) and they are what the portal and
        # the neighbourhood view display, so a title carrying "&amp;nbsp;" is
        # both a bad vector and a visibly broken card. The title lives in a
        # COLUMN as well as in meta, so both are rewritten.
        ftitle = sources._clean(title or "")
        fixed = sources._clean(abstract)
        if (abstract and fixed != abstract) or (title and ftitle != title):
            recleaned.append((uid, d, fixed, ftitle))

    log(f"[clean] {len(recleaned):,} titles/abstracts contain markup or entities")
    for uid, _, f, ft in recleaned[:3]:
        log(f"    -> {(ft or f)[:88]}")
    log(f"[clean] {len(drops):,} papers are unambiguously another discipline")
    for uid, t in drops[:5]:
        log(f"    x  {t}")

    if args.dry_run:
        log("[clean] dry run -- nothing written")
        return

    for uid, d, fixed, ftitle in recleaned:
        patch = {}
        if fixed:
            patch["abstract"] = fixed
        if ftitle:
            patch["title"] = ftitle
            con.execute("UPDATE items SET title=? WHERE uid=?", (ftitle, uid))
        if patch:
            store.update_meta(con, uid, patch)
        # the vector was built from the old text, markup included
        con.execute("DELETE FROM embeddings WHERE uid=?", (uid,))
    for uid, _ in drops:
        con.execute("DELETE FROM items WHERE uid=?", (uid,))
        con.execute("DELETE FROM embeddings WHERE uid=?", (uid,))
    con.commit()
    log(f"[clean] recleaned {len(recleaned):,}, dropped {len(drops):,}")
    log("[clean] their vectors are invalidated -- run tools/embed.py next")


if __name__ == "__main__":
    main()
