#!/usr/bin/env python3
"""Tests for the subject-tag matcher. Offline, no archive needed.

A closed vocabulary is only worth having if it is precise. The failure that
matters is not a missed tag -- the LLM fallback covers those -- it is a WRONG
tag, because a tag is a filter, and a filter that lies sends the reader to the
wrong papers silently. So most of these assert what must NOT match.

    python tools/test_tags.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import config       # noqa: E402
import tags as T    # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print("  %-58s %s" % (name, "ok" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        FAILED.append(name)


def has(text, tag):
    return tag in T.tags_for("", text)


def test_matches():
    print("the vocabulary fires on real phrasing")
    check("trend following", has("We study time-series momentum in futures.",
                                 "trend following"), True)
    check("carry", has("Returns to the currency carry trade.", "carry"), True)
    check("roll yield", has("Commodity curves in backwardation.", "roll yield"), True)
    check("ZLB", has("Policy at the zero lower bound.", "ZLB"), True)
    check("term premium", has("We estimate the term premium.", "term premium"), True)
    check("machine learning", has("A neural network for returns.",
                                  "machine learning"), True)
    check("causal inference", has("A lead-lag test of causality.",
                                  "causal inference"), True)
    check("title is searched too",
          "volatility" in T.tags_for("Realized volatility forecasting", ""), True)
    check("summary is searched when there is no abstract",
          "crowding" in T.tags_for("", "", "On crowded trades and alpha decay."), True)


def test_must_not_match():
    print("the vocabulary does NOT fire on the things you objected to")
    # The paper that prompted the prune. It must not acquire a finance tag from
    # words that merely look financial.
    green = ("GREEN FINANCE AS A CATALYST FOR SUSTAINABLE DEVELOPMENT: "
             "A BIBLIOMETRICS REVIEW OF FINANCIAL INNOVATION AND ENVIRONMENTAL "
             "OUTCOMES")
    check("green-finance bibliometrics gets no tags", T.tags_for(green, ""), [])
    # "carry" as an ordinary verb is why the vocabulary has no bare "carry".
    check("'carry out an analysis' is not the carry factor",
          has("We carry out an analysis of returns.", "carry"), False)
    # \b stops substring hits inside longer words.
    check("'etf' does not fire inside 'wetfoot'", has("wetfoot ventures", "ETFs"), False)
    check("'gmm' does not fire inside 'gmmv'", has("the gmmv model", "econometrics"), False)
    check("'oil' does not fire inside 'toil'", has("years of toil", "oil"), False)
    check("no tag from an empty document", T.tags_for("", ""), [])
    check("no tag from whitespace", T.tags_for("   ", "  "), [])


def test_precedence():
    print("longest surface wins inside one tag")
    # "time-series momentum" belongs to trend following, not to momentum: the
    # alternation is sorted longest-first so the specific phrase is consumed
    # before the generic one can match it.
    got = T.tags_for("", "Time-series momentum across asset classes.")
    check("time-series momentum -> trend following", "trend following" in got, True)
    check("...and not the cross-sectional momentum tag",
          "momentum" in got, False)


def test_vocabulary_health():
    print("the vocabulary itself")
    check("every tag has at least one surface",
          all(len(v) >= 1 for v in config.TAGS.values()), True)
    check("no surface is claimed by two tags",
          len({s for v in config.TAGS.values() for s in v})
          == sum(len(v) for v in config.TAGS.values()), True)
    check("all surfaces are lower-case",
          all(s == s.lower() for v in config.TAGS.values() for s in v), True)
    # A one- or two-character surface matches far too much to be useful.
    short = [s for v in config.TAGS.values() for s in v if len(s.strip()) < 3]
    check("no surface shorter than 3 characters", short, [])
    check("TAGS_MAX is a display cap, not zero", config.TAGS_MAX > 0, True)


if __name__ == "__main__":
    for fn in (test_matches, test_must_not_match, test_precedence,
               test_vocabulary_health):
        fn()
    print()
    if FAILED:
        print("FAILED: %d" % len(FAILED))
        for f in FAILED:
            print("  " + f)
        sys.exit(1)
    print("all tag tests passed")
