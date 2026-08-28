#!/usr/bin/env python3
"""Settle a roster author's S2 id by what they PUBLISH, not by how cited they are.

WHY THE CURRENT RULE FAILS. core_roster.py flags `needs_review` when a second
candidate profile has h >= 10 -- a popularity test that knows nothing about
subject. On "Bryan Kelly" that is exactly backwards: the orthopaedic surgeon
has h=78 and the Yale economist h=18, so h-index points AT the wrong person.
75 of 175 roster members are blocked behind that flag, which is 12,141
author-paper slots never fetched, including Andrew Ang, Ben Bernanke, Allan
Timmermann and Brad Barber.

THE DISCRIMINATOR IS THE PUBLICATION FIELD. Ask S2 for each candidate WITH
their papers attached and compute the share that sits in Economics or
Business. Measured on the case that created the flag:

    h=0   econ/bus   0%   B. Kelly         (physics)
    h=4   econ/bus   0%   B. Kelly         (post-COVID clinics)
    h=6   econ/bus  67%   Bryan T. Kelly   <- the economist
    h=3   econ/bus   0%   Bryan T. Kelly   (surgeon longevity)

fieldsOfStudy is domain-level and cannot tell carry from FX -- it was measured
and rejected for sleeve labelling. Here the question is only "is this person an
economist at all", which is precisely what a domain label answers.

ONE REQUEST PER PERSON. Nested fields (`papers.fieldsOfStudy`) return the
candidates and their papers together, so 175 people cost 175 requests rather
than one per candidate profile.

VALIDATE BEFORE TRUSTING. `--check` runs over the people whose id is already
settled and reports how often this method picks the same one. A rule that
cannot reproduce known answers has no business deciding unknown ones.

    python tools/core_roster_verify.py --check      # validate on the settled 100
    python tools/core_roster_verify.py --resolve    # decide the flagged 75
"""

import argparse
import collections
import csv
import io
import json
import os
import pathlib
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from progress import Progress                              # noqa: E402

ROSTER = pathlib.Path("data") / "core_roster.csv"
OUT = pathlib.Path("export") / "core_roster_verified.json"
API = "https://api.semanticscholar.org/graph/v1/author/search"
UA = "quant-digest/1.0"
# Economics and Business are the desk's domains. A profile below this share is
# a different person with the same name, however well cited.
MIN_SHARE = 40.0
MIN_PAPERS = 3          # a share computed over one paper is not evidence


def log(m):
    print(m, flush=True)


def _candidates(name, hdr):
    for attempt in range(6):
        try:
            # LIMIT 100, NOT 8. S2 fragments a common name across dozens of
            # profiles and does not rank the substantial one first: the real
            # Andrew J. Patton (h=29, 67 papers) sits at rank 11 of 36, so a
            # top-8 window returned only fragments and the method blamed
            # itself for what was a truncation.
            r = requests.get(API, headers=hdr, timeout=90, params={
                "query": name, "limit": 100,
                "fields": "name,hIndex,paperCount,papers.fieldsOfStudy"})
        except Exception:                                   # noqa: BLE001
            time.sleep(4 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(6 * (attempt + 1))
            continue
        if not r.ok:
            return None
        return r.json().get("data") or []
    return None


def _score(c):
    """(econ/business share, papers counted) for one candidate profile."""
    fos = collections.Counter()
    for w in (c.get("papers") or []):
        for f in (w.get("fieldsOfStudy") or []):
            fos[f] += 1
    tot = sum(fos.values())
    if not tot:
        return 0.0, 0
    econ = fos.get("Economics", 0) + fos.get("Business", 0)
    return 100.0 * econ / tot, tot


def _pick(cands):
    """Best profile by field share, breaking ties on h-index."""
    scored = []
    for c in cands:
        share, n = _score(c)
        if n >= MIN_PAPERS and share >= MIN_SHARE:
            scored.append((share, c.get("hIndex") or 0, c))
    if not scored:
        return None, 0.0
    # SIZE BREAKS THE TIE, NOT PURITY. S2 fragments people across several
    # profiles, and a fragment holding six Economics papers scores 100% while
    # the person's main profile scores 85% -- so ranking by share alone picked
    # an "Andrew W. Lo" with h=7 over the real one. Among profiles that ARE
    # economists, the right one is the substantial one.
    scored.sort(key=lambda x: (-x[1], -x[0]))
    return scored[0][2], scored[0][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="validate against people whose id is already settled")
    ap.add_argument("--resolve", action="store_true",
                    help="decide the needs_review people")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if not (args.check or args.resolve):
        log("[verify] pass --check or --resolve")
        return 2
    if not ROSTER.exists():
        log(f"[verify] {ROSTER} missing")
        return 2

    people = list(csv.DictReader(io.open(ROSTER, encoding="utf-8")))
    if args.check:
        subset = [p for p in people if p.get("needs_review") != "1"
                  and (p.get("s2_id") or "").strip()]
        label = "settled"
    else:
        subset = [p for p in people if p.get("needs_review") == "1"]
        label = "flagged"
    if args.limit:
        subset = subset[:args.limit]
    key = os.environ.get("S2_API_KEY", "").strip()
    hdr = {"User-Agent": UA}
    if key:
        hdr["x-api-key"] = key
    pause = 1.1 if key else 3.2
    log(f"[verify] {len(subset)} {label} people, one request each "
        f"({'key set' if key else 'NO KEY -- slow'})")

    agree = disagree = nopick = failed = 0
    out = []
    prog = Progress(len(subset), "verify", every_s=30)
    for p in subset:
        cands = _candidates(p["name"], hdr)
        if cands is None:
            failed += 1
            prog.tick(); time.sleep(pause); continue
        best, share = _pick(cands)
        rec = {"name": p["name"], "current_s2_id": p.get("s2_id", ""),
               "n_candidates": len(cands)}
        if best is None:
            rec["verdict"] = "no_economist_candidate"
            nopick += 1
        else:
            rec.update({"picked_s2_id": best.get("authorId"),
                        "picked_name": best.get("name"),
                        "picked_h": best.get("hIndex"),
                        "econ_share": round(share, 1)})
            if args.check:
                same = str(best.get("authorId")) == str(p.get("s2_id"))
                rec["verdict"] = "agrees" if same else "DISAGREES"
                agree += same
                disagree += (not same)
            else:
                rec["verdict"] = "resolved"
        out.append(rec)
        prog.tick()
        time.sleep(pause)
    prog.done()

    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    if args.check:
        tot = agree + disagree
        log(f"[verify] VALIDATION: agrees {agree}/{tot} "
            f"({100*agree/max(1,tot):.1f}%); no economist found {nopick}; "
            f"request failures {failed}")
        log("[verify] disagreements are worth reading -- this method can be "
            "right where the old one was wrong:")
        for r in out:
            if r.get("verdict") == "DISAGREES":
                log(f"[verify]   {r['name']:<26} had {r['current_s2_id']:<12} "
                    f"-> {r['picked_s2_id']} ({r['picked_name']}, "
                    f"econ {r['econ_share']}%, h={r['picked_h']})")
    else:
        ok = sum(1 for r in out if r["verdict"] == "resolved")
        log(f"[verify] resolved {ok}/{len(subset)}; "
            f"{nopick} had no economist candidate; {failed} request failures")
    log(f"[verify] written to {OUT} -- roster NOT modified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
