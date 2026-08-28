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

# None = not probed yet, True = the key works, False = OpenAlex rejected it.
# A REJECTED KEY IS WORSE THAN NO KEY: keyless requests still succeed on the
# $0.10/day budget, but a bad bearer token turns every call into 401, and
# callers that treat a failed request as "the source had nothing" then report
# an empty result rather than an error. That is not hypothetical -- it cost a
# whole CI build: build_core's route C made 60 requests, got 401 sixty times,
# swallowed each one at `if not rr.ok: continue`, and logged "resolved 0
# unheld references" while status() cheerfully claimed we were authenticated.
_VERIFIED: bool | None = None


def key() -> str:
    return (os.environ.get(ENV) or "").strip()


def usable() -> bool:
    """Is there a key we have not already seen rejected?"""
    return bool(key()) and _VERIFIED is not False


def headers(base: dict | None = None) -> dict:
    """`base` plus a bearer token when a usable key is configured.

    Drops the token once preflight() has seen OpenAlex reject it, so the
    caller degrades to the keyless budget instead of 401ing on every request.
    """
    h = dict(base or {})
    if usable():
        h["Authorization"] = f"Bearer {key()}"
    return h


def preflight(log=print) -> bool:
    """ASK OPENALEX whether the key works, rather than whether it is set.

    status() only ever checked that the environment variable was non-empty,
    which is a statement about our own process, not about authentication. Call
    this at the top of any job that leans on OpenAlex: one request buys the
    difference between "authenticated", "rejected -- continuing keyless", and
    "no key configured", and it sets the flag headers() consults.
    """
    global _VERIFIED
    k = key()
    if not k:
        _VERIFIED = None
        log(f"[oa] {ENV} NOT set -- keyless $0.10/day budget (~1,000 "
            f"requests), shared with every anonymous caller from this IP")
        return False
    try:
        import requests                                     # noqa: PLC0415
        r = requests.get("https://api.openalex.org/works",
                         headers={"User-Agent": "quant-digest/1.0",
                                  "Authorization": f"Bearer {k}"},
                         params={"filter": "openalex_id:W2741809807",
                                 "select": "id", "per-page": 1},
                         timeout=30)
    except Exception as e:                                  # noqa: BLE001
        log(f"[oa] preflight could not reach OpenAlex ({type(e).__name__}); "
            f"assuming the key is good and continuing")
        _VERIFIED = True
        return True
    if r.status_code in (401, 403):
        _VERIFIED = False
        log(f"[oa] {ENV} REJECTED by OpenAlex (HTTP {r.status_code}: "
            f"{r.text[:120]}). Continuing WITHOUT it on the keyless "
            f"$0.10/day budget -- rotate the key, this is not a soft failure")
        return False
    _VERIFIED = True
    log(f"[oa] {ENV} accepted (HTTP {r.status_code}) -- $1/day budget")
    return True


def params(base: dict | None = None) -> dict:
    """`base` plus api_key. Prefer headers(); this is for callers that cannot
    set one."""
    p = dict(base or {})
    k = key()
    if k:
        p["api_key"] = k
    return p


def status(log=print) -> bool:
    """Whether a key is CONFIGURED. Prefer preflight(): a configured key and a
    working key are different claims, and only one of them predicts results."""
    if key():
        log(f"[oa] {ENV} set (not yet verified -- call preflight() to check "
            f"OpenAlex actually accepts it)")
        return True
    log(f"[oa] {ENV} NOT set -- keyless $0.10/day budget (~1,000 requests), "
        f"shared with every other anonymous caller from this IP")
    return False
