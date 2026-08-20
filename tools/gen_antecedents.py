#!/usr/bin/env python3
"""Regenerate antecedents.json -- the prior-art reference the novelty rubric
judges against.

`antecedent_match` decides whether a paper's claimed contribution already
exists, and it drives novelty_posterior. Until now it worked from canon.py's
43 Method-typed entries alone. The archive now holds 309 curated classics with
citation counts, so the reference can be both broader and better ordered:
the most-cited established work is exactly what a "has this been done before?"
check should be measured against.

Deliberately carries only title, authors, year and a SHORT editorial note --
never abstract text. The note is canon.py's own one-line rationale where one
exists. That keeps the file small enough to sit in a prompt already near 18k
characters, and keeps third-party text out of the repo.

Written as generated data rather than read from the database at import time,
so llm.py stays importable without a database.

  python tools/gen_antecedents.py
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store  # noqa: E402

OUT = pathlib.Path("antecedents.json")
# Reserved split, not one cap. Canon alone is 96 entries, so a single
# ceiling let it fill every slot and admitted no cited classics at all --
# which was the entire point of broadening beyond canon.
N_CANON = 35      # prefer Method/Theory: frameworks a paper can duplicate
N_CITED = 20      # most-cited non-canon: established by adoption


def main():
    con = store.connect()
    rows = []
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        try:
            d = json.loads(meta)
        except Exception:                                   # noqa: BLE001
            continue
        if not d.get("classic"):
            continue
        rows.append({
            "title": (title or "").strip()[:120],
            "authors": (d.get("authors") or "").split(",")[0].strip()[:40],
            "year": (d.get("date") or "")[:4],
            "cites": d.get("cites") or 0,
            # our own editorial line, never the paper's text
            "note": (d.get("canon_why") or "").strip()[:150],
            "type": d.get("canon_type", ""),
        })

    # Curated canon first (someone judged these seminal), then the most-cited.
    # Both are what "already established" means; citations alone would bury a
    # foundational theory paper under a popular recent one.
    canon = [r for r in rows if r["note"]]
    # a framework can be duplicated; an empirical finding usually cannot, so
    # Method and Theory earn the canon slots first
    canon.sort(key=lambda r: (r["type"] not in ("Method", "Theory"),
                              -(r["cites"] or 0)))
    rest = sorted([r for r in rows if not r["note"]],
                  key=lambda r: -(r["cites"] or 0))
    picked = canon[:N_CANON] + rest[:N_CITED]

    OUT.write_text(json.dumps(picked, indent=1), encoding="utf-8")
    n_canon = min(len(canon), N_CANON)
    print(f"wrote {OUT} — {len(picked)} antecedents "
          f"({n_canon} curated, {len(picked)-n_canon} by citation), "
          f"{OUT.stat().st_size/1000:.0f} KB")


if __name__ == "__main__":
    main()
