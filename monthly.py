"""Monthly top-20 picks + progressive backward backfill.

Runs after the present digest each pipeline run:
  1. refresh_present() -- recompute the top-20 for every month touched by this
     run's fresh items (from the archive; reuses their stored scores, no new LLM).
  2. promote_seminal() -- push any present paper the LLM rates highly innovative
     into Classics' "modern" (emerging-seminal) list.
  3. backfill_step() -- fetch + score ONE earlier month (walking back to
     config.BACKFILL_FLOOR), resumable across runs via store.month_progress when
     the per-run LLM batch budget is hit.

Writes docs/monthly.json ({"YYYY-MM": [entry, ...]}) and updates the "modern"
key of docs/classics.json.
"""

import datetime as dt
import json
import pathlib

import backfill
import config
import llm
import scoring
import store

DOCS = pathlib.Path("docs")
MONTHLY = DOCS / "monthly.json"
CLASSICS = DOCS / "classics.json"


def _load(p: pathlib.Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        return default


def _save(p: pathlib.Path, obj) -> None:
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(obj, default=str), encoding="utf-8")


def _valid(m: str) -> bool:
    return bool(m) and len(m) == 7 and m[4] == "-"


def _month_of(it: dict) -> str:
    return str(it.get("date") or it.get("seen") or "")[:7]


def _this_month() -> str:
    return dt.date.today().strftime("%Y-%m")


def _prev_month(m: str) -> str:
    y, mo = map(int, m.split("-"))
    mo -= 1
    if mo == 0:
        y, mo = y - 1, 12
    return f"{y:04d}-{mo:02d}"


def _archive_items(con) -> list[dict]:
    out = []
    for (meta,) in con.execute("SELECT meta FROM items"):
        try:
            out.append(json.loads(meta))
        except Exception:                              # noqa: BLE001
            pass
    return out


def refresh_present(con, fresh, monthly, log) -> None:
    """Recompute the CURRENT calendar month's top-20 from the archive's stored
    scores. Only the current month -- earlier months are filled fully by the
    backward backfill (Crossref), not from the sparse 30-day digest window."""
    m = _this_month()
    items = [it for it in _archive_items(con) if _month_of(it) == m]
    entries = scoring.composite_entries(items, config.MONTHLY_TOP_N)
    if entries:
        monthly[m] = entries
        log(f"[monthly] {m} (present): {len(entries)} picks from {len(items)} items")


def promote_seminal(fresh, log) -> None:
    entries = scoring.composite_entries(fresh, len(fresh) or 1)
    flagged = [e for e in entries
               if e["contribution"] >= config.SEMINAL_CONTRIB_MIN
               and not e["contribution_provisional"]
               and e["composite"] >= config.SEMINAL_COMPOSITE_MIN]
    if not flagged:
        return
    classics = _load(CLASSICS, {})
    if not isinstance(classics, dict):
        classics = {"overall": classics}
    modern = classics.get("modern", [])
    seen = {(x.get("url") or x.get("title", "")).lower() for x in modern}
    added = 0
    for e in flagged:
        k = (e.get("url") or e.get("title", "")).lower()
        if k in seen:
            continue
        modern.append({
            "title": e["title"], "url": e["url"], "authors": e["authors"],
            "journal": e["journal"], "year": str(e.get("date", ""))[:4],
            "cites": e.get("cites"), "contribution": e["contribution"],
            "composite": e["composite"], "summary": e.get("summary", ""),
            "type": "Modern", "added": _this_month(),
        })
        seen.add(k)
        added += 1
    if added:
        classics["modern"] = modern
        _save(CLASSICS, classics)
        log(f"[seminal] promoted {added} paper(s) to Classics/modern")


def backfill_step(con, monthly, log) -> None:
    if not llm.have_key():
        log("[backfill] no LLM key; skipping")
        return
    floor = config.BACKFILL_FLOOR
    earliest = store.kv_get(con, "backfill_earliest")
    if not (earliest and _valid(earliest)):
        earliest = _this_month()          # start walking back from the current month
    target = _prev_month(earliest)
    if target < floor:
        log(f"[backfill] reached floor {floor}; nothing to do")
        return

    prog = store.get_progress(con, target)
    if prog and prog["candidates"]:
        candidates = prog["candidates"]
        log(f"[backfill] resuming {target}: {len(candidates)} candidates")
    else:
        candidates = backfill.fetch_month(target, log)
        if not candidates:
            store.kv_set(con, "backfill_earliest", target)   # empty month, advance
            log(f"[backfill] {target}: no articles; cursor -> {target}")
            return
        scoring.attach_s2(candidates, log)                   # S2 once, then persist
        store.set_progress(con, target, candidates, False)

    scoring.llm_score(candidates, log, max_batches=config.BACKFILL_LLM_BATCHES)
    try:                                           # ensemble consensus on the shortlist
        llm.consensus(candidates, log, max_batches=config.CONSENSUS_MAX_BATCHES)
    except Exception as e:                         # noqa: BLE001
        log(f"[consensus] backfill failed: {type(e).__name__}: {e}")
    monthly[target] = scoring.composite_entries(candidates, config.MONTHLY_TOP_N)

    # junk records (editorial front matter, etc.) are deliberately NEVER scored
    # (scoring.llm_score skips them to save LLM quota) -- exclude them here too,
    # or the month would never be detected as complete.
    remaining = [c for c in candidates
                 if c.get("relevance") is None and not scoring.is_junk(c.get("title", ""))]
    if remaining:
        store.set_progress(con, target, candidates, False)
        log(f"[backfill] {target} partial: {len(remaining)} unscored; resume next run")
    else:
        # persist every scored candidate to the permanent archive -- not just
        # this month's top-N winners -- so Archive/dedup/a future recompute
        # (e.g. after a scoring-logic change) can see them; previously this
        # only lived in month_progress, which clear_progress below discards
        to_save = store.filter_new(con, candidates)
        store.save(con, to_save)
        store.clear_progress(con, target)
        store.kv_set(con, "backfill_earliest", target)
        log(f"[backfill] {target} complete: {len(monthly[target])} picks "
            f"({len(to_save)} new items archived); cursor -> {target}")


def run(con, fresh, log) -> None:
    monthly = _load(MONTHLY, {})
    if not isinstance(monthly, dict):
        monthly = {}
    for step in (lambda: refresh_present(con, fresh, monthly, log),
                 lambda: promote_seminal(fresh, log),
                 lambda: backfill_step(con, monthly, log)):
        try:
            step()
        except Exception as e:                         # noqa: BLE001
            log(f"[monthly] step failed: {type(e).__name__}: {e}")
    _save(MONTHLY, monthly)
