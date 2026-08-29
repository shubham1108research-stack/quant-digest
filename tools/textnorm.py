#!/usr/bin/env python3
"""One normaliser, because disagreeing ones have cost this project three bugs.

THE DEFECT CLASS. The same string is a KEY in one module and DATA in another,
and only one side gets normalised. It has now happened three times:

  1. TAG_SLEEVE in build_core.py was keyed on term strings that had to match
     core_tags.csv exactly. Three of its 25 keys named terms that file never
     contained -- "trend following", "theory of storage", "backwardation" --
     so route A never searched them and the labeller could never assign them.
     Dead in both directions, silent about it.

  2. Fixing (1) by normalising terms at load re-keyed _TERM_SLEEVE onto
     normalised strings while route A still recorded raw ones, so
     _TERM_SLEEVE.get("time-series momentum") missed a dict keyed "time series
     momentum". trend_cta lost 152 of 154 papers in a single build. The fix
     for the class reproduced the class.

  3. clean_core.norm kept hyphens ([^a-z0-9-]+) while build_core, author_sites,
     backfill_nber and fetch_pdfs all stripped them ([^a-z0-9]+). A hyphenated
     term only matched a hyphenated title, so 46 of 402 terms were invisible to
     a keep-list whose failure mode is DELETION: 101 papers were removed as
     strays, among them "Detecting p-Hacking" and "Inference with 'Difference
     in Differences'".

So: one function, imported everywhere a term meets a title. Hyphens go, because
S2 ignores them in phrase search too -- measured on four pairs, "cross-section
of expected returns" and "cross section of expected returns" both return
exactly 272 results. Normalising costs no recall and buys the guarantee that
both sides of every comparison were prepared the same way.
"""

import re

_RUN = re.compile(r"[^a-z0-9]+")


def norm(t: str) -> str:
    """Lowercase, collapse every non-alphanumeric run to one space, strip."""
    return _RUN.sub(" ", (t or "").lower()).strip()


def padded(t: str) -> str:
    """`norm` with leading/trailing spaces, for whole-word substring tests.

    clean_core matches ' term ' inside ' title ' so that "risk" does not fire
    on "brisk"; the padding is the word boundary.
    """
    return " " + norm(t) + " "
