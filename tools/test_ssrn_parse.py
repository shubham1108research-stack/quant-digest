#!/usr/bin/env python3
"""Tests for the SSRN eJournal mail parser. Offline, no mailbox needed.

This exists because the parser failed twice in ways that produced no error at
all. First the link regex stopped matching when SSRN changed its URL shape, and
24 mailings parsed to zero papers -- indistinguishable from nothing being
subscribed. Then, with that fixed, it produced 60 papers whose titles were
"_____ T A B L E", the table-of-contents rule, which is worse: a plausible
record that reaches the archive, gets scored, and is shown as a paper.

Neither failure could have been caught by anything except a fixture, because
both produced well-formed output. The fixtures below are the real layouts, cut
down; the assertions are about the FIELDS, not the count, since a count was
green in both broken states.

    python tools/test_ssrn_parse.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import sources  # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print("  %-56s %s" % (name, "ok" if ok else "FAIL\n      got  %r\n      want %r"
                          % (got, want)))
    if not ok:
        FAILED.append(name)


# The current layout, as it actually arrives (2026-08). Two papers, one with a
# wrapped title and two authors, plus the table-of-contents block that links
# the same ids and must NOT become a record.
CURRENT = '''
                     T A B L E   O F   C O N T E N T S
______________________________
"Chapter 3 : FINANCIAL WELL-BEING"
   AFM JALAL AHAMED
   https://ssrn.com/abstract=6262319?dgcid=ejournal_email_behavioral

______________________________
"Is Sector Rotation Causal? A Geometric Test of the
Growth-to-Defensive Lead-Lag"
  Contact:  AGUS SUDJIANTO
              Wells Fargo
    Email:  agus@example.com
  Contact:  ARPIT NARAIN
              Some Institute
    Email:  arpit@example.com
Auth-Page:  https://ssrn.com/author=6673635?dgcid=ejournal_email_x
Full Text:  https://ssrn.com/abstract=7313339?dgcid=ejournal_email_x
ABSTRACT: Sector rotation, growth sectors leading defensive
sectors, is a staple of tactical allocation, and a lagged
correlation matrix confirms it.

______________________________
"Chapter 3 : FINANCIAL WELL-BEING"
  Contact:  AFM JALAL AHAMED
              University of Skovde
    Email:  jalal.ahamed@his.se
Auth-Page:  https://ssrn.com/author=6673635?dgcid=ejournal_email_behavioral
Full Text:  https://ssrn.com/abstract=6262319?dgcid=ejournal_email_behavioral
ABSTRACT: This chapter views financial wellbeing as a
multidimensional concept.
'''

# The layout SSRN used before, still sitting in the mailbox.
LEGACY = '''
1. Currency Carry Trades Revisited
by Jane Smith, John Doe
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1234567
We examine the empirical properties of the payoffs to carry.

2. Another Paper About Something Else Entirely
by A Third Person
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7654321
A second abstract, long enough to be kept by the length filter.
'''


def test_current():
    print("current SSRN layout")
    got = sources._parse_ssrn_ejournal(CURRENT, "publish.ssrn.com")
    check("papers extracted", len(got), 2)
    by_doi = {p["doi"]: p for p in got}

    p = by_doi.get("10.2139/ssrn.7313339")
    check("wrapped title is joined, quotes stripped",
          p and p["title"],
          "Is Sector Rotation Causal? A Geometric Test of the "
          "Growth-to-Defensive Lead-Lag")
    check("both Contact: authors captured", p and p["authors"],
          "AGUS SUDJIANTO, ARPIT NARAIN")
    check("affiliation is NOT in the author list",
          bool(p and "Wells Fargo" not in p["authors"]), True)
    check("abstract starts at ABSTRACT:",
          bool(p and p["abstract"].startswith("Sector rotation")), True)
    check("email address is not in the abstract",
          bool(p and "agus@example.com" not in p["abstract"]), True)
    check("canonical url", p and p["url"], "https://ssrn.com/abstract=7313339")

    # THE REGRESSION THAT SHIPPED 60 BAD RECORDS
    titles = [x["title"] for x in got]
    check("no table-of-contents rule became a title",
          any("T A B L E" in t or t.strip("_ ") == "" for t in titles), False)
    check("the id linked twice yields ONE record",
          sum(1 for x in got if x["doi"] == "10.2139/ssrn.6262319"), 1)


def test_legacy():
    print("legacy SSRN layout")
    got = sources._parse_ssrn_ejournal(LEGACY, "publish.ssrn.com")
    check("papers extracted", len(got), 2)
    p = got[0]
    check("title", p["title"], "Currency Carry Trades Revisited")
    check("authors", p["authors"], "Jane Smith, John Doe")
    check("doi", p["doi"], "10.2139/ssrn.1234567")
    check("abstract", p["abstract"].startswith("We examine"), True)


def test_junk():
    print("nothing to find")
    check("empty body", sources._parse_ssrn_ejournal("", "x"), [])
    check("no abstract link",
          sources._parse_ssrn_ejournal("just some prose, no links", "x"), [])
    check("author link alone is not a paper",
          sources._parse_ssrn_ejournal(
              "___\nsee https://ssrn.com/author=123456?x\n", "x"), [])


if __name__ == "__main__":
    for fn in (test_current, test_legacy, test_junk):
        fn()
    print()
    if FAILED:
        print("FAILED: %d" % len(FAILED))
        sys.exit(1)
    print("all SSRN parser tests passed")
