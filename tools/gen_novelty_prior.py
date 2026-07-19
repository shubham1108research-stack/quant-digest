#!/usr/bin/env python3
"""Regenerate config.NOVELTY_PRIOR from the seminal canon + an archive-volume
snapshot, using a Beta-Binomial empirical-Bayes estimate of

    P(a paper in this topic is seminal-caliber) = (A0 + canon_count) /
                                                  (A0 + B0 + modern_volume)

Run this ONLY when canon.CANON, config.TOPICS, or the archive snapshot change:

    python tools/gen_novelty_prior.py

It prints the NOVELTY_PRIOR dict to paste into config.py. It is deliberately a
one-off generator, not called by the live pipeline: the pipeline must never
recompute priors against the (possibly just-cleared / Day-0-empty) live archive,
which would divide by zero and/or drift run-to-run. The denominators come from
tools/archive_volume_snapshot.json -- a frozen count from a full archive run.

canon.CANON splits "Asset Pricing Theory" and "Factor Models & the
Cross-Section" as separate topics, while config.TOPICS merges them into
"Asset Pricing & Factor Models"; _ALIAS maps canon topics onto config topics so
the numerator (canon) and denominator (archive, tagged with config.TOPICS)
align.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import canon      # noqa: E402
import config     # noqa: E402

# weak conjugate prior: ~5% base rate, ~21 pseudo-observations of smoothing so a
# thin-data topic can't produce a wild estimate
A0 = 1.0
B0 = 20.0

# canon topic -> config.TOPICS bucket (only the entries that differ)
_ALIAS = {
    "Asset Pricing Theory": "Asset Pricing & Factor Models",
    "Factor Models & the Cross-Section": "Asset Pricing & Factor Models",
}

_SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "archive_volume_snapshot.json")


def canon_counts() -> dict:
    counts: dict[str, int] = {}
    for topic, papers in canon.CANON.items():
        key = _ALIAS.get(topic, topic)
        counts[key] = counts.get(key, 0) + len(papers)
    return counts


def main() -> None:
    vol = json.load(open(_SNAPSHOT, encoding="utf-8"))["topic_volume"]
    counts = canon_counts()
    fallback = A0 / (A0 + B0)

    priors = {}
    for topic in config.TOPICS:
        c = counts.get(topic, 0)
        v = vol.get(topic, 0)
        if c == 0 and v == 0:                       # no evidence either way
            priors[topic] = round(fallback, 4)
        else:
            priors[topic] = round((A0 + c) / (A0 + B0 + v), 4)

    print(f"# Beta-Binomial priors (A0={A0}, B0={B0}); regenerate with "
          f"tools/gen_novelty_prior.py")
    print(f"NOVELTY_PRIOR_FALLBACK = {round(fallback, 4)}")
    print("NOVELTY_PRIOR = {")
    for topic in config.TOPICS:
        c, v = counts.get(topic, 0), vol.get(topic, 0)
        print(f'    "{topic}": {priors[topic]},'
              f"  # canon={c}, archive_vol={v}")
    print("}")


if __name__ == "__main__":
    main()
