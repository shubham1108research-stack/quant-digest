#!/usr/bin/env python3
"""CFTC Commitments of Traders -> docs/cot.json, for the For You desk briefing.

Positioning is the one stream in a macro digest that is not papers, and CFTC
gives it away: no key, no auth, no bot check. The catch is that the obvious way
to read it is wrong in four separate ways, all of which fail SILENTLY -- a
plausible-looking number that is not the number you asked for. Each guard below
exists because the naive version produced one of them:

  1. The long/short field names differ between the two reports, and not
     symmetrically: TFF is `lev_money_positions_long` with NO `_all` suffix,
     while the disaggregated report IS `m_money_positions_long_all`. Ask TFF
     for `..._long_all` and every rates, FX and equity row reads net zero.

  2. CFTC renamed most contracts in Feb 2022. "10-YEAR U.S. TREASURY NOTES -
     CHICAGO BOARD OF TRADE" still resolves and still returns 817 rows -- it
     just stops at 2022-02-01. A hardcoded name list does not break, it goes
     quiet, which is worse. So the universe is DISCOVERED from the latest
     published week and every series is pinned by exact equality.

  3. `like '%GOLD%'` also matches MICRO GOLD; `like '%E-MINI S&P 500%'` also
     matches MICRO E-MINI S&P 500 INDEX. Sorting those merged rows by date and
     taking the top two gives you two different contracts.

  4. Which is how a week-over-week change comes out as +140,633 on a net of
     +141,648 -- the "previous week" was a different market entirely. Any
     diff here is checked to be against the same contract, one report period
     back, or it is not reported at all.

Usage:
    python tools/cot.py                 # write docs/cot.json
    python tools/cot.py --dry-run       # print the table, write nothing
"""

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import config  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "cot.json"
TIMEOUT = 60


def log(m):
    print(m, flush=True)


def _num(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _get(dataset: str, params: dict) -> list[dict]:
    r = requests.get(config.COT_API.format(dataset=dataset), params=params,
                     timeout=TIMEOUT,
                     headers={"User-Agent": "quant-digest/1.0"})
    r.raise_for_status()
    return r.json()


def _latest_date(dataset: str) -> str | None:
    rows = _get(dataset, {"$select": "max(report_date_as_yyyy_mm_dd) as d"})
    d = (rows[0].get("d") if rows else None) or ""
    return d[:10] or None


def _universe(dataset: str, as_of: str) -> list[str]:
    """Markets published in the latest week that clear the liquidity floor.

    Discovered, not hardcoded -- see fault 2 in the module docstring. The open
    interest floor is what "everything liquid" means operationally: it drops
    the Coinbase nano/perp listings on their own numbers rather than on a
    blocklist that would need updating every time they list another token.
    """
    rows = _get(dataset, {
        "$select": "market_and_exchange_names,open_interest_all",
        "$where": (f"report_date_as_yyyy_mm_dd = '{as_of}T00:00:00.000' "
                   f"and open_interest_all >= {config.COT_MIN_OI}"),
        "$limit": "5000",
    })
    return sorted({r["market_and_exchange_names"] for r in rows
                   if r.get("market_and_exchange_names")})


def _history(dataset: str, markets: list[str], since: str,
             lf: str, sf: str) -> dict[str, list[dict]]:
    """Three years of weekly rows for the discovered markets, grouped by name.

    One bulk request per dataset rather than one per contract: ~100 markets x
    156 weeks is well inside Socrata's 50k row cap, and it keeps this to two
    round trips instead of two hundred. `in(...)` pins each series by EXACT
    name -- the whole point of fault 3.
    """
    quoted = ",".join("'" + m.replace("'", "''") + "'" for m in markets)
    rows = _get(dataset, {
        "$select": (f"market_and_exchange_names,report_date_as_yyyy_mm_dd,"
                    f"open_interest_all,{lf},{sf}"),
        "$where": (f"report_date_as_yyyy_mm_dd >= '{since}T00:00:00.000' "
                   f"and market_and_exchange_names in({quoted})"),
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "50000",
    })
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["market_and_exchange_names"], []).append(r)
    return out


# Patterns compiled once, on WORD BOUNDARIES. Plain substring matching put
# GASOLINE RBOB, S&P 500 Consolidated and MARYLAND SOLAR REC into the crypto
# bucket -- all three contain "SOL" -- and CIG ROCKIES into credit, on "IG".
_GROUP_RE = [
    # The optional trailing S is not cosmetic: CFTC lists SOYBEANS, and
    # \bSOYBEAN\b does not match it -- there is no word boundary between the
    # N and the S. Enumerating every plural is the kind of list that rots.
    (key, label, re.compile(r"\b(?:" + "|".join(re.escape(p) for p in patterns) + r")S?\b"))
    for key, label, patterns in config.COT_GROUPS
]


def _group_for(name: str) -> tuple[str, str]:
    """First matching bucket wins, so the ordering in config.COT_GROUPS is
    load-bearing: EURO SHORT TERM RATE has to be caught by rates before the FX
    patterns see the word EURO."""
    u = name.upper()
    for key, label, rx in _GROUP_RE:
        if rx.search(u):
            return key, label
    # No name pattern matched. The exchange usually settles it: ~160 of the
    # disaggregated report's contracts are US power and gas hubs (PJM, ERCOT,
    # SP15, Transco, Houston Ship Channel) whose names share nothing but the
    # venue they list on. Matching the venue beats maintaining a hub list.
    for needle, key, label in config.COT_EXCHANGE_GROUPS:
        if needle in u:
            return key, label
    return config.COT_GROUP_FALLBACK


def _percentile(current: int, history: list[int]) -> int | None:
    """Where this week's net sits in its own 3-year range, 0-100.

    A raw |net| / open-interest share says nothing about whether the level is
    unusual FOR THAT CONTRACT -- it flags Gold at 35% and Micro S&P at 30%
    with no way to tell a crowded position from a normally-large one. The
    percentile is the number a trend desk actually reads.
    """
    if len(history) < config.COT_MIN_OBS:
        return None
    below = sum(1 for h in history if h < current)
    ties = sum(1 for h in history if h == current)
    return round((below + ties / 2) / len(history) * 100)


def _row(name: str, rows: list[dict], lf: str, sf: str, group_label: str):
    """One contract -> one display row, or None if it cannot be trusted."""
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r["report_date_as_yyyy_mm_dd"], reverse=True)
    cur = rows[0]
    net = _num(cur.get(lf)) - _num(cur.get(sf))
    oi = _num(cur.get("open_interest_all"))

    # Week-over-week, only against the SAME contract one report period back.
    # CFTC publishes weekly, but a holiday can push a release: anything outside
    # 5-10 days is a gap, and diffing across a gap invents a change that never
    # happened. No number beats a wrong one.
    wow = None
    if len(rows) > 1:
        d0 = dt.date.fromisoformat(cur["report_date_as_yyyy_mm_dd"][:10])
        d1 = dt.date.fromisoformat(rows[1]["report_date_as_yyyy_mm_dd"][:10])
        if 5 <= (d0 - d1).days <= 10:
            wow = net - (_num(rows[1].get(lf)) - _num(rows[1].get(sf)))

    hist = [_num(r.get(lf)) - _num(r.get(sf)) for r in rows]
    return {
        "name": name,
        "group": group_label,
        "net": net,
        "wow": wow,
        "oi": oi,
        "oi_share": round(abs(net) / oi, 4) if oi else None,
        "pct": _percentile(net, hist),
        "obs": len(hist),
    }


def build(dry_run: bool = False) -> dict:
    since = (dt.date.today()
             - dt.timedelta(weeks=config.COT_HISTORY_WEEKS)).isoformat()
    by_group: dict[str, list[dict]] = {}
    dates: list[str] = []
    groups_meta = {k: lbl for k, lbl, _ in config.COT_GROUPS}
    groups_meta[config.COT_GROUP_FALLBACK[0]] = config.COT_GROUP_FALLBACK[1]

    for dataset, trader_group, lf, sf in config.COT_DATASETS:
        try:
            as_of = _latest_date(dataset)
            if not as_of:
                log(f"[cot] {dataset}: no report date; skipping")
                continue
            markets = _universe(dataset, as_of)
            if not markets:
                log(f"[cot] {dataset}: no market cleared OI >= "
                    f"{config.COT_MIN_OI:,}; skipping")
                continue
            hist = _history(dataset, markets, since, lf, sf)
        except Exception as e:                             # noqa: BLE001
            # One dataset failing must not take the other down with it: a
            # commodities outage should still leave rates and FX on the panel.
            log(f"[cot] {dataset} failed ({type(e).__name__}: {e}); skipping")
            continue

        dates.append(as_of)
        log(f"[cot] {dataset} ({trader_group}) {as_of}: "
            f"{len(markets)} markets over OI floor")
        for name in markets:
            key, label = _group_for(name)
            try:
                row = _row(name, hist.get(name, []), lf, sf, label)
            except Exception as e:                         # noqa: BLE001
                log(f"[cot]   {name}: dropped ({type(e).__name__}: {e})")
                continue
            if row is None:
                log(f"[cot]   {name}: dropped (no history returned)")
                continue
            if row["net"] == 0:
                # This trader group holds no position in this contract. A row
                # of zeroes is not a flat view, it is an absence of one, and
                # padding the panel with them buries the contracts that matter.
                continue
            row["trader_group"] = trader_group
            by_group.setdefault(key, []).append(row)

    as_of = max(dates) if dates else ""
    stale = True
    if as_of:
        age = (dt.date.today() - dt.date.fromisoformat(as_of)).days
        stale = age > config.COT_STALE_DAYS
        if stale:
            log(f"[cot] WARNING: latest report is {age} days old")

    # Groups in the configured order, contracts within a group by how large the
    # position is relative to the contract -- the biggest bet first, not the
    # biggest contract.
    order = [k for k, _, _ in config.COT_GROUPS] + [config.COT_GROUP_FALLBACK[0]]
    out = {
        "as_of": as_of,
        "stale": stale,
        "built": dt.date.today().isoformat(),
        "groups": [{
            "key": k,
            "label": groups_meta[k],
            "note": config.COT_NOTES.get(k, ""),
            "rows": sorted(by_group[k],
                           key=lambda r: r["oi_share"] or 0, reverse=True),
        } for k in order if by_group.get(k)],
    }
    total = sum(len(g["rows"]) for g in out["groups"])
    log(f"[cot] {total} contracts in {len(out['groups'])} groups, as of {as_of}")

    if dry_run:
        for g in out["groups"]:
            print(f"\n=== {g['label']} ({len(g['rows'])})")
            print(f"{'Contract':<42}{'Net':>11}{'WoW':>11}{'3y %ile':>9}"
                  f"{'OI share':>10}")
            for r in g["rows"]:
                wow = "n/a" if r["wow"] is None else f"{r['wow']:+,}"
                pct = "n/a" if r["pct"] is None else f"p{r['pct']}"
                shr = "n/a" if r["oi_share"] is None else f"{r['oi_share']*100:.1f}%"
                print(f"{r['name'][:41]:<42}{r['net']:>+11,}{wow:>11}"
                      f"{pct:>9}{shr:>10}")
        return out

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    log(f"[cot] wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the table, write nothing")
    args = ap.parse_args()
    build(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
