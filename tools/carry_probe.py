#!/usr/bin/env python3
"""Is the carry/rates boundary a MODEL limitation or a PROMPT limitation?

The full rubric routed convenience-yield papers to rates_credit and vol_options
despite an explicit rule saying they are carry. Two competing explanations, and
they need different fixes:

  A  the model cannot make the distinction        -> change the taxonomy
  B  the rule is buried in an ~18,600-char prompt -> restructure the prompt

Controlled test: the SAME papers, asked twice.
  focused  -- one question, rule stated plainly, nothing else competing
  embedded -- the rule as it appears now, wrapped in the full rubric's bulk

If focused succeeds where embedded failed, it is B and the model is fine.
"""

import json
import os
import pathlib
import re
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import store   # noqa: E402

URL = "https://openrouter.ai/api/v1/chat/completions"

# titles the full rubric got wrong, plus controls that should stay rates_credit
TARGETS = [
    ("Convenience Yields around the World", "carry"),
    ("Bond Convenience Yields in the Eurozone", "carry"),
    ("Convenience Yield of US Treasuries", "carry"),
    ("Optimal Currency Exposure When Interest Parity Fails", "carry"),
    ("Forward Premium Anomaly", "carry"),
    ("Quantifying the Determinants of the US Treasury Term Premium", "?"),
    ("unspanned macro risks in dynamic term structure", "rates_credit"),
    ("Reconstructing a Century of U.S. Corporate Bonds", "rates_credit"),
]

FOCUSED = """You classify finance papers into ONE sleeve of a systematic macro book.

The only distinction that matters here:
  carry        -- the paper is about the RETURN EARNED FROM HOLDING an asset:
                  a yield, roll or interest differential. This includes
                  convenience yield, roll yield, forward premium, deviations
                  from interest parity, and term premium treated as a
                  harvestable return. The asset class is irrelevant.
  rates_credit -- the paper MODELS the term structure, curve dynamics, credit
                  risk or default, without the return-from-holding being the
                  subject.
  fx           -- exchange-rate determination, with no yield differential as
                  the subject.

Answer with ONE word: carry, rates_credit, or fx. Then a 10-word reason."""


def ask(system, user, key, model):
    r = requests.post(URL, headers={"Authorization": f"Bearer {key}",
                                    "Content-Type": "application/json"},
                      json={"model": model, "temperature": 0,
                            "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}]},
                      timeout=120)
    if not r.ok:
        return f"__ERR__ {r.status_code}"
    return r.json()["choices"][0]["message"]["content"].strip()


def main():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY not set")
    model = config.OPENROUTER_MODEL

    con = store.connect()
    papers = []
    for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
        for frag, expect in TARGETS:
            if frag.lower() in (title or "").lower():
                try:
                    m = json.loads(meta)
                except Exception:                          # noqa: BLE001
                    m = {}
                a = (m.get("abstract") or "").strip()
                if len(a.split()) > 40:
                    papers.append((title, a[:1400], expect))
                break
    seen, uniq = set(), []
    for t, a, e in papers:
        k = t[:60].lower()
        if k not in seen:
            seen.add(k)
            uniq.append((t, a, e))
    print(f"probing {len(uniq)} boundary papers with {model}\n")

    import llm                                              # the live rubric prompt

    # BOTH arms. The docstring defines a controlled A/B -- the same papers,
    # asked twice, focused vs embedded -- and states the decision rule "if
    # focused succeeds where embedded failed, it is B". Only the focused arm
    # was ever run, so a score of 5/7 could not distinguish hypothesis A (the
    # model cannot make the distinction) from B (the rule is buried in an
    # 18,600-char prompt). The closing line asserting the rubric "got the carry
    # ones wrong" was remembered, not measured.
    EMBEDDED = llm._SYSTEM + (
        "\n\nAnswer with ONLY the single most appropriate sleeve key for this "
        "paper, lowercase, no JSON and no other text.")

    f_hits = e_hits = 0
    for title, abstract, expect in uniq:
        user = f"Title: {title}\n\nAbstract: {abstract}"
        focused = ask(FOCUSED, user, key, model)
        embedded = ask(EMBEDDED, user, key, model)
        f_got = re.split(r"[^a-z_]", focused.lower().strip())[0]
        e_got = re.split(r"[^a-z_]", embedded.lower().strip())[0]
        f_ok = (expect == "?") or (f_got == expect)
        e_ok = (expect == "?") or (e_got == expect)
        if expect != "?":
            f_hits += bool(f_ok)
            e_hits += bool(e_ok)
        flag = "OK " if f_ok else "MISS"
        print(f"  expect={expect:<13} focused={f_got:<13} embedded={e_got:<13} "
              f"{flag}  {title[:44]}")
        if not f_ok:
            print(f"        focused said: {focused[:100]}")
    scoreable = [e for _, _, e in uniq if e != "?"]
    n = len(scoreable)
    print(f"\n  FOCUSED  ({len(FOCUSED):>6} chars): {f_hits}/{n}")
    print(f"  EMBEDDED ({len(EMBEDDED):>6} chars): {e_hits}/{n}")
    if f_hits > e_hits:
        print("\n  -> focused beats embedded: the rule is BURIED, not wrong."
              "\n     Fix the prompt (hoist the sleeve block, or split the call).")
    elif e_hits >= f_hits and f_hits < n:
        print("\n  -> focused does not beat embedded: prompt length is not the"
              "\n     problem. The TAXONOMY is. Redraw the carry boundary.")
    else:
        print("\n  -> both arms correct: neither hypothesis holds on this sample.")


if __name__ == "__main__":
    main()
