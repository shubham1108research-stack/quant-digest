#!/usr/bin/env python3
"""OpenAlex authentication, in one place.

WHY THIS EXISTS. OpenAlex began charging in February 2026. It is not an auth
wall -- keyless requests still work -- it is a DOLLAR BUDGET, and the keyless
allowance is $0.10/day, which the API spends at $0.0001 per call:

    HTTP 429  "Insufficient budget. This request costs $0.0001 but you only
               have $0 remaining. Resets at midnight UTC."
    X-RateLimit-Limit-USD: 0.1        X-RateLimit-Limit: 1000

One thousand requests a day, shared across everything -- and this repo spends
346 on the PDF resolver's OpenAlex stage alone (which found 1,363 pdf urls in a
single run) plus ~420 more building reference lists for the citation graph. So
the budget was exhausted, and the failure mode is quiet: a 429 looks like
throttling, retries do not help, and the caller records "OpenAlex had nothing".

A free key raises it 10x. The fix is a key, not a redesign.

TWELVE FILES CALL OPENALEX and they build requests three different ways --
requests.get with a params dict, requests.get with a bare url, and urllib
through a local _get. Threading a key through each one by hand is how you end
up with eleven of twelve done. The key rides in a HEADER here rather than the
api_key= query parameter, because a header survives every one of those shapes
without touching the params dict, and because a key in a query string ends up
in server logs and in any url this repo happens to print.

Both forms are documented and equivalent:
    api_key=<key> as a query parameter
    Authorization: Bearer <key>

Set OPENALEX_API_KEY in the environment. With no key set every helper here is a
no-op, so nothing breaks locally and the keyless budget still applies.
"""

import os

ENV = "OPENALEX_API_KEY"


def key() -> str:
    return (os.environ.get(ENV) or "").strip()


def headers(base: dict | None = None) -> dict:
    """`base` plus a bearer token when a key is configured."""
    h = dict(base or {})
    k = key()
    if k:
        h["Authorization"] = f"Bearer {k}"
    return h


def params(base: dict | None = None) -> dict:
    """`base` plus api_key. Prefer headers(); this is for callers that cannot
    set one."""
    p = dict(base or {})
    k = key()
    if k:
        p["api_key"] = k
    return p


def status(log=print) -> bool:
    """Say plainly whether the budget is the free $1 or the keyless $0.10.

    Worth logging at the top of any job that leans on OpenAlex: the difference
    between "no results" and "no budget" is invisible in the results themselves.
    """
    if key():
        log(f"[oa] {ENV} set -- authenticated ($1/day budget)")
        return True
    log(f"[oa] {ENV} NOT set -- keyless $0.10/day budget (~1,000 requests), "
        f"shared with every other anonymous caller from this IP")
    return False
