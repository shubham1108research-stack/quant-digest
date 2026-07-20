#!/usr/bin/env python3
"""Regenerate config.RELEVANCE_PRIOR from the seminal canon + a frozen archive
relevance-rate snapshot, using a Beta-Binomial empirical-Bayes estimate of

    P(a paper in this topic is core-fit) = (A0 + w*canon_count + archive_hits) /
                                            (A0 + B0 + w*canon_count + archive_total)

Unlike the novelty prior (which has no historical "seminal" label to bootstrap
from, only the canon), the archive DOES already carry historical relevance
judgments -- so this blends two signals: how often work in a topic has
historically been rated core-fit (archive_hits/archive_total), boosted by how
canon-dense the topic is (a topic anchored by lots of seminal papers is
intrinsically more central to quant finance, independent of any one run's
judgments).

Run this ONLY when canon.CANON, config.TOPICS, or the archive snapshot change:

    python tools/gen_relevance_prior.py

It prints the RELEVANCE_PRIOR dict to paste into config.py. Deliberately a
one-off generator, not called by the live pipeline -- the pipeline must never
recompute priors against the (possibly just-cleared) live archive, which would
divide by zero and/or drift run-to-run. The archive numbers come from
tools/relevance_archive_snapshot.json, a frozen count refreshed manually.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import canon      # noqa: E402
import config     # noqa: E402

# weak conjugate prior: 20% base rate, 10 pseudo-observations of smoothing so a
# thin-data topic can't produce a wild estimate
A0 = 2.0
B0 = 8.0
CANON_WEIGHT = 1.0    # each canon paper counts as one pseudo core-fit hit

# canon topic -> config.TOPICS bucket (only the entries that differ)
_ALIAS = {
    "Asset Pricing Theory": "Asset Pricing & Factor Models",
    "Factor Models & the Cross-Section": "Asset Pricing & Factor Models",
}

_SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "relevance_archive_snapshot.json")


def canon_counts() -> dict:
    counts: dict[str, int] = {}
    for topic, papers in canon.CANON.items():
        key = _ALIAS.get(topic, topic)
        counts[key] = counts.get(key, 0) + len(papers)
    return counts


def main() -> None:
    snap = json.load(open(_SNAPSHOT, encoding="utf-8"))["topic_relevance"]
    counts = canon_counts()
    fallback = A0 / (A0 + B0)

    priors = {}
    for topic in config.TOPICS:
        c = counts.get(topic, 0)
        hits = snap.get(topic, {}).get("core", 0)
        n = snap.get(topic, {}).get("n", 0)
        priors[topic] = round(
            (A0 + CANON_WEIGHT * c + hits) / (A0 + B0 + CANON_WEIGHT * c + n), 4)

    print(f"# Beta-Binomial priors (A0={A0}, B0={B0}, canon_weight={CANON_WEIGHT}); "
          f"regenerate with tools/gen_relevance_prior.py")
    print(f"RELEVANCE_PRIOR_FALLBACK = {round(fallback, 4)}")
    print("RELEVANCE_PRIOR = {")
    for topic in config.TOPICS:
        c = counts.get(topic, 0)
        hits = snap.get(topic, {}).get("core", 0)
        n = snap.get(topic, {}).get("n", 0)
        print(f'    "{topic}": {priors[topic]},'
              f"  # canon={c}, archive_core={hits}/{n}")
    print("}")


if __name__ == "__main__":
    main()
