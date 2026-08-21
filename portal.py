"""Generate the static portal (docs/) from the SQLite archive.

Exports every archived item to docs/data.json and writes a self-contained
"research journal" browser (docs/index.html): three pages -- Recent (last 7
days), Monthly (grouped by calendar month, Jul 2026 onward), and Classics (the
all-time most-cited finance papers, docs/classics.json, produced by backfill.py).
Each page groups entries by source category (Academic T1 / T2 / Preprints /
Practitioner) latest-first. Self-hosted Newsreader serif in docs/fonts/.
"""

import datetime as dt
import json
import pathlib

import config
import scoring


def _export(con) -> list[dict]:
    rows = con.execute(
        "SELECT uid, title, source, section, url, meta, first_seen FROM items"
    ).fetchall()
    out = []
    for uid, title, source, section, url, meta, first_seen in rows:
        if scoring.is_junk(title):    # editorial front matter, blog link-roundups
            continue
        try:
            m = json.loads(meta)
        except Exception:                          # noqa: BLE001
            m = {}
        out.append({
            # uid joins a paper to its row in docs/vec.bin (the semantic index)
            "uid": uid,
            "title": title or m.get("title", ""),
            "url": url or m.get("url", ""),
            "authors": m.get("authors", ""),
            "source": source or m.get("source", ""),
            "section": str(section or m.get("section", "")),
            "tier": m.get("tier"),
            "date": m.get("date") or first_seen,
            "seen": first_seen,
            "score": m.get("rank_score"),
            "relevance": (m.get("relevance") or {}).get("level"),
            "relevance_category": m.get("relevance_category"),
            "relevance_posterior": m.get("relevance_posterior"),
            "generality": (m.get("generality") or {}).get("level"),
            "contribution": (m.get("contribution") or {}).get("level"),
            "contribution_provisional": (m.get("contribution") or {}).get("provisional", True),
            "testability": (m.get("testability") or {}).get("level"),
            "novelty_type": m.get("novelty_type"),
            "novelty_posterior": m.get("novelty_posterior"),
            "consensus_n": m.get("consensus_n"),
            "consensus_agree": m.get("consensus_agree"),
            "author_score": m.get("author_score"),
            "reputation": m.get("reputation"),
            "watchlist": bool(m.get("watchlist")),
            "watchlist_author": m.get("watchlist_author"),
            "topic": m.get("topic", ""),
            # the second classification: which parts of the desk's book this
            # touches, and how usable it is there
            "sleeves": m.get("sleeves") or [],
            "desk_fit": m.get("desk_fit"),
            # practitioners aren't LLM-summarised -> fall back to their RSS blurb
            "summary": m.get("summary") or m.get("why") or (m.get("abstract") or "")[:400],
        })
    out.sort(key=lambda x: (x["seen"] or "", x["date"] or ""), reverse=True)
    return out


def build(con) -> int:
    data = _export(con)
    docs = pathlib.Path("docs")
    docs.mkdir(exist_ok=True)

    # data.json ships on every page load (Recent/For You/Practitioners) --
    # keep it bounded to a recent window so it doesn't grow forever as the
    # archive does. archive.json carries everything, lazy-fetched only when
    # the Archive tab is opened. Windowed on the paper's own date (falling
    # back to when we first saw it), NOT "seen" alone -- a backfilled item
    # can be freshly persisted today but be genuinely months old, and it's
    # the age of the CONTENT that should decide whether it ships by default.
    # The cutoff anchors on the REAL current date, not max(dates in the
    # archive) -- a single bad/future-dated record from an upstream source
    # (seen: a stray "2027-04-01") would otherwise poison the whole window.
    cutoff = (dt.date.today()
              - dt.timedelta(days=config.PORTAL_RECENT_WINDOW_DAYS)).isoformat()
    recent = [x for x in data if (x["date"] or x["seen"] or "")[:10] >= cutoff]
    (docs / "data.json").write_text(json.dumps(recent, default=str), encoding="utf-8")
    (docs / "archive.json").write_text(json.dumps(data, default=str), encoding="utf-8")

    if not (docs / "classics.json").exists():      # placeholder until backfill runs
        (docs / "classics.json").write_text("[]", encoding="utf-8")
    if not (docs / "monthly.json").exists():        # placeholder until monthly runs
        (docs / "monthly.json").write_text("{}", encoding="utf-8")
    if not (docs / "nber.json").exists():           # placeholder until NBER backfill
        (docs / "nber.json").write_text("{}", encoding="utf-8")
    html = _INDEX.replace(
        "__RELEVANCE_CONFIDENCE_PCT__", str(round(config.RELEVANCE_CONFIDENCE * 100))
    ).replace(
        "__TOPICS_JSON__", json.dumps(config.TOPICS)
    ).replace(
        "__SLEEVES_JSON__", json.dumps(
            [[k, SLEEVE_LABEL.get(k, k)] for k in config.SLEEVES]))
    (docs / "index.html").write_text(html, encoding="utf-8")
    return len(data)


# Short display names for the desk sleeves. config.SLEEVES holds the LLM-facing
# definitions -- paragraphs, deliberately verbose, so a classifier can draw the
# boundary. A facet needs something that fits in a dropdown.
SLEEVE_LABEL = {
    "trend_cta":      "Trend / CTA",
    "carry":          "Carry",
    "fx":             "FX",
    "rates_credit":   "Rates & credit",
    "commodities":    "Commodities",
    "macro_regime":   "Macro regime",
    "cross_asset":    "Cross-asset",
    "vol_options":    "Vol & options",
    "equity_xs":      "Equity cross-section",
    "microstructure": "Microstructure",
    "other":          "Other",
}

_INDEX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Quant Research Digest</title>
<style>
@font-face{font-family:Newsreader;font-style:normal;font-weight:400;font-display:swap;
  src:url(fonts/newsreader-400.woff2) format('woff2');}
@font-face{font-family:Newsreader;font-style:normal;font-weight:600;font-display:swap;
  src:url(fonts/newsreader-600.woff2) format('woff2');}
@font-face{font-family:Newsreader;font-style:italic;font-weight:400;font-display:swap;
  src:url(fonts/newsreader-400-italic.woff2) format('woff2');}

:root{
  --ground:#F6F8F7; --panel:#FFFFFF; --ink:#171C1A; --muted:#5E6B66;
  --faint:#8A968F; --line:#E4E8E5; --line2:#EDF0EE; --accent:#0C5C4A;
  --strong:#1F7A3D; --medium:#9A6B00; --low:#8A8F87; --cite:#9A6B00;
  --serif:Newsreader,ui-serif,Georgia,'Iowan Old Style',Palatino,serif;
  --sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#0F1311; --panel:#141917; --ink:#E9ECE8; --muted:#94A099; --faint:#6E7A73;
  --line:#232A26; --line2:#1C221F; --accent:#43B08F; --strong:#3FAE63; --medium:#C79A3A;
  --low:#7D857D; --cite:#C79A3A;}}
:root[data-theme="dark"]{
  --ground:#0F1311; --panel:#141917; --ink:#E9ECE8; --muted:#94A099; --faint:#6E7A73;
  --line:#232A26; --line2:#1C221F; --accent:#43B08F; --strong:#3FAE63; --medium:#C79A3A;
  --low:#7D857D; --cite:#C79A3A;}
:root[data-theme="light"]{
  --ground:#F6F8F7; --panel:#FFFFFF; --ink:#171C1A; --muted:#5E6B66; --faint:#8A968F;
  --line:#E4E8E5; --line2:#EDF0EE; --accent:#0C5C4A; --strong:#1F7A3D; --medium:#9A6B00;
  --low:#8A8F87; --cite:#9A6B00;}

*{box-sizing:border-box;}
::selection{background:var(--accent);color:var(--panel);}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--serif);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;}
a{color:inherit;text-decoration:none;}
.wrap{max-width:768px;margin:0 auto;padding:0 22px 72px;}
header{position:sticky;top:0;z-index:9;background:var(--ground);border-bottom:1px solid transparent;
  transition:box-shadow .25s ease,border-color .25s ease;}
header.scrolled{border-color:var(--line);box-shadow:0 6px 18px -12px rgba(0,0,0,.25);}
.mast{max-width:768px;margin:0 auto;padding:18px 22px 0;}
.brandrow{display:flex;align-items:baseline;justify-content:space-between;gap:14px;}
.brand{font-weight:600;font-size:26px;letter-spacing:-.01em;line-height:1;}
.brand .the{font-style:italic;font-weight:400;color:var(--muted);}
.tagline{font-family:var(--sans);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--faint);margin-top:6px;}
.toggle{font-family:var(--sans);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);background:none;border:1px solid var(--line);border-radius:20px;
  padding:5px 11px;cursor:pointer;transition:border-color .15s,color .15s,transform .15s;}
.toggle:hover{border-color:var(--accent);color:var(--accent);}
.toggle:active{transform:scale(.94);}
.doublerule{height:3px;border-top:1px solid var(--ink);border-bottom:1px solid var(--ink);margin:14px 0 0;}
.navtabs{display:flex;gap:6px;padding:12px 0 4px;flex-wrap:nowrap;overflow-x:auto;
  scrollbar-width:none;-ms-overflow-style:none;-webkit-mask:linear-gradient(90deg,#000 94%,transparent);}
.navtabs::-webkit-scrollbar{display:none;}
.navtabs button{font-family:var(--sans);font-size:12.5px;font-weight:600;letter-spacing:.01em;
  color:var(--muted);background:var(--panel);border:1px solid var(--line);border-radius:20px;
  padding:7px 14px;cursor:pointer;white-space:nowrap;flex:none;
  transition:background .15s ease,border-color .15s ease,color .15s ease,transform .1s ease;}
.navtabs button:hover:not(.on){border-color:var(--accent);color:var(--accent);}
.navtabs button:active{transform:scale(.96);}
.navtabs button.on{color:var(--panel);background:var(--ink);border-color:var(--ink);}
.navtools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:6px 0 12px;}
.navtools .sp{flex:1;}
.searchwrap{position:relative;display:inline-flex;align-items:center;}
.searchwrap:before{content:"";position:absolute;left:10px;width:12px;height:12px;pointer-events:none;
  background:var(--faint);
  -webkit-mask:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" fill="none" stroke="black" stroke-width="2"/><line x1="21" y1="21" x2="16.65" y2="16.65" stroke="black" stroke-width="2"/></svg>') center/contain no-repeat;
  mask:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" fill="none" stroke="black" stroke-width="2"/><line x1="21" y1="21" x2="16.65" y2="16.65" stroke="black" stroke-width="2"/></svg>') center/contain no-repeat;}
#q{font-family:var(--sans);font-size:13px;color:var(--ink);background:var(--panel);
  border:1px solid var(--line);border-radius:20px;padding:6px 10px 6px 28px;width:190px;max-width:42vw;
  transition:border-color .15s,box-shadow .15s;}
#q:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent);}
#month,#cat,#jsel,#topic{font-family:var(--sans);font-size:12.5px;font-weight:600;color:var(--muted);
  background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:6px 12px;
  cursor:pointer;max-width:44vw;transition:border-color .15s,color .15s;}
#cat:hover,#month:hover,#jsel:hover,#topic:hover{border-color:var(--accent);color:var(--accent);}
#cat:focus,#month:focus,#jsel:focus,#topic:focus{outline:0;border-color:var(--accent);}
.dateline{font-family:var(--sans);font-size:12px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);margin:26px 0 2px;display:flex;align-items:center;gap:10px;}
.dateline .n{font-variant-numeric:tabular-nums;color:var(--faint);}
/* a card with no score has no rail to show, so it spans the full width */
.entry.flat{grid-template-columns:1fr;}
.ntag{display:inline-block;font-family:var(--sans);font-size:10px;font-weight:600;
  letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  background:var(--line2);border-radius:3px;padding:2px 7px;margin-bottom:7px;}
.ntag.wait{color:var(--faint);background:transparent;border:1px dashed var(--line);}

/* ---- two-level navigation ---- */
.subtabs{display:flex;gap:5px;padding:0 0 10px;flex-wrap:wrap;}
.subtabs button{font-family:var(--sans);font-size:12px;font-weight:500;
  letter-spacing:.01em;color:var(--muted);background:transparent;
  border:1px solid transparent;border-radius:16px;padding:5px 12px;
  cursor:pointer;transition:color .15s,background .15s;}
.subtabs button:hover:not(.on){color:var(--accent);background:var(--line2);}
.subtabs button.on{color:var(--accent);background:var(--panel);
  border-color:var(--line);font-weight:600;}
.subtabs button[hidden]{display:none;}

/* ---- responsive: the portal previously had NO width breakpoints, so on a
   phone the score rail, sub-bars and nav all competed for a 360px column ---- */
@media (max-width:960px){
  .wrap{padding-left:16px;padding-right:16px;}
  .entry{grid-template-columns:44px 1fr;gap:12px;padding:14px;}
}
@media (max-width:640px){
  body{font-size:16px;}
  /* the score rail costs a third of the width on a phone; put the number
     inline above the title instead of beside it */
  .entry{grid-template-columns:1fr;gap:8px;padding:14px 15px;}
  .rail{display:flex;align-items:center;gap:10px;text-align:left;padding:0;}
  .gauge{width:38px;height:38px;flex:none;}
  .cap{margin:0;}
  .subs{grid-template-columns:1fr 1fr;}
  .navtabs button{padding:7px 13px;}            /* >=44px touch target */
  .subtabs button{padding:7px 12px;}
  .navtools{gap:6px;}
  .navtools select,.searchwrap input{max-width:100%;}
  h1,.brand{font-size:22px;}
  /* --- Ask: conversations, and the outside literature --- */
.chatbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:14px 0 4px;}
.newchat{font-family:var(--sans);font-size:11.5px;font-weight:700;letter-spacing:.02em;
  color:var(--accent);background:transparent;border:1px dashed var(--accent);
  border-radius:999px;padding:5px 12px;cursor:pointer;}
.newchat:hover{background:color-mix(in srgb,var(--accent) 10%,transparent);}
.chatpill{display:inline-flex;align-items:center;gap:7px;max-width:230px;
  font-family:var(--sans);font-size:11.5px;font-weight:600;color:var(--muted);
  background:var(--panel);border:1px solid var(--line);border-radius:999px;
  padding:5px 11px;cursor:pointer;transition:border-color .15s,color .15s;}
.chatpill>:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.chatpill:hover{border-color:var(--accent);color:var(--accent);}
.chatpill.on{background:var(--accent);border-color:var(--accent);color:var(--panel);}
.chatpill .x{opacity:0;font-size:10px;padding:0 2px;border-radius:3px;transition:opacity .12s;}
.chatpill:hover .x{opacity:.7;}
.chatpill .x:hover{opacity:1;background:color-mix(in srgb,currentColor 22%,transparent);}
.outtog{display:flex;align-items:center;gap:7px;margin:8px 2px 0;
  font-family:var(--sans);font-size:11.5px;color:var(--muted);cursor:pointer;}
.outtog input{accent-color:var(--accent);cursor:pointer;}
/* the outside block is deliberately not styled like a source: these are papers
   we do NOT hold, and the difference has to be visible before it is read */
.srch.osh{margin-top:22px;color:var(--cite);}
.osrc{display:flex;gap:12px;align-items:flex-start;justify-content:space-between;
  padding:11px 13px;margin:7px 0;border:1px dashed var(--line);border-radius:11px;
  background:color-mix(in srgb,var(--cite) 4%,transparent);}
.osb{min-width:0;}
.osb a{font-family:var(--serif);font-size:14.5px;color:var(--ink);text-decoration:none;
  line-height:1.35;}
.osb a:hover{color:var(--accent);text-decoration:underline;}
.oabs{font-size:12.5px;line-height:1.5;color:var(--muted);margin-top:5px;max-width:70ch;}
.oatag{color:var(--strong);font-weight:650;}
.addbtn{flex:0 0 auto;font-family:var(--sans);font-size:11px;font-weight:700;
  letter-spacing:.02em;color:var(--panel);background:var(--accent);border:0;
  border-radius:7px;padding:6px 11px;cursor:pointer;transition:opacity .15s;}
.addbtn:hover{opacity:.86;}
.addbtn.done,.addbtn:disabled{background:var(--line);color:var(--muted);cursor:default;}
.xtag{font-family:var(--sans);font-size:9.5px;font-weight:700;letter-spacing:.05em;
  text-transform:uppercase;margin-right:7px;padding:1px 5px;border-radius:4px;
  color:var(--cite);background:color-mix(in srgb,var(--cite) 14%,transparent);}
.askbox{flex-direction:column;align-items:stretch;}
  .askbox button{width:100%;padding:11px;}
  .src{grid-template-columns:22px 1fr;}
}

/* ---- Ask tab ---- */
.askbox{display:flex;gap:10px;align-items:flex-end;margin:14px 0 6px;padding:12px;
  background:var(--panel);border:1px solid var(--line);border-radius:14px;}
.askbox textarea{flex:1;resize:vertical;min-height:46px;font-family:var(--serif);font-size:16px;
  line-height:1.5;color:var(--ink);background:transparent;border:0;outline:0;padding:4px 2px;}
.askbox button{font-family:var(--sans);font-size:12.5px;font-weight:600;letter-spacing:.02em;
  padding:9px 20px;border-radius:20px;border:1px solid var(--ink);background:var(--ink);
  color:var(--panel);cursor:pointer;transition:opacity .15s;}
.askbox button:hover:not(:disabled){opacity:.82;}
.askbox button:disabled{opacity:.45;cursor:default;}
.askex{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 4px;}
.exq{font-family:var(--sans);font-size:12px;color:var(--muted);background:var(--panel);
  border:1px solid var(--line);border-radius:20px;padding:7px 13px;cursor:pointer;text-align:left;
  transition:border-color .15s,color .15s;}
.exq:hover{border-color:var(--accent);color:var(--accent);}
.ftag{font-family:var(--sans);font-size:10px;font-weight:600;letter-spacing:.06em;
  text-transform:uppercase;color:var(--panel);background:var(--ink);border-radius:20px;
  padding:2px 7px;margin-right:7px;}
.cachetag{font-family:var(--sans);font-size:10.5px;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted);background:var(--line);border-radius:20px;
  padding:3px 9px;margin-left:10px;vertical-align:middle;}
.reask{font-family:var(--sans);font-size:11px;color:var(--muted);background:none;
  border:0;cursor:pointer;text-decoration:underline;margin-left:6px;padding:0;}
.reask:hover{color:var(--accent);}
.qa{margin:22px 0 8px;padding-top:18px;border-top:1px solid var(--line);}
.qa:first-of-type{border-top:0;}
.qq{font-family:var(--serif);font-size:20px;font-weight:600;line-height:1.35;margin-bottom:12px;}
.answer{font-size:16.5px;line-height:1.62;}
.answer p{margin:0 0 12px;}
.answer ul{margin:0 0 12px;padding-left:22px;}
.answer li{margin:0 0 6px;}
.answer code{font-size:.9em;background:var(--line);padding:1px 5px;border-radius:4px;}
a.cite{font-family:var(--sans);font-size:11px;font-weight:600;vertical-align:super;
  text-decoration:none;color:var(--accent);padding:0 1px;}
.thinking{font-family:var(--sans);font-size:13px;color:var(--muted);padding:6px 0;}
.thinking::after{content:'';display:inline-block;width:1em;text-align:left;
  animation:dots 1.2s steps(4,end) infinite;}
@keyframes dots{0%{content:'';}25%{content:'.';}50%{content:'..';}75%{content:'...';}}
.askerr{font-family:var(--sans);font-size:13px;color:#b4442e;background:rgba(180,68,46,.07);
  border:1px solid rgba(180,68,46,.25);border-radius:10px;padding:10px 12px;}
.srch{font-family:var(--sans);font-size:11px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--faint);margin:20px 0 8px;}
.src{display:grid;grid-template-columns:26px 1fr;gap:10px;padding:9px 0;
  border-top:1px solid var(--line);font-size:14.5px;}
.src .sn{font-family:var(--sans);font-size:11px;font-weight:600;color:var(--faint);
  padding-top:3px;font-variant-numeric:tabular-nums;}
.src a{color:var(--ink);text-decoration:none;font-weight:600;}
.src a:hover{color:var(--accent);}
.src .meta{font-family:var(--sans);font-size:11.5px;color:var(--muted);margin-top:2px;}
.sechead{font-size:19px;font-weight:600;margin:30px 0 4px;padding-bottom:7px;
  border-bottom:1px solid var(--ink);display:flex;justify-content:space-between;align-items:baseline;}
.sechead .cnt{font-family:var(--sans);font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--faint);font-weight:400;}
.sechead.t2{color:var(--muted);border-bottom-color:var(--line);}
.entry{display:grid;grid-template-columns:52px 1fr;gap:16px;padding:16px 18px;margin:10px 0;
  background:var(--panel);border:1px solid var(--line);border-radius:14px;
  box-shadow:0 1px 3px rgba(20,25,23,.04);
  transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease;}
.entry:hover{transform:translateY(-2px);box-shadow:0 10px 24px -14px rgba(20,25,23,.28);border-color:var(--accent);}
.rail{text-align:center;padding-top:2px;}
.rail .rank{font-family:var(--serif);font-size:14px;font-weight:600;line-height:1;color:var(--accent);
  font-variant-numeric:tabular-nums;padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid var(--line);}
.gauge{width:38px;height:38px;border-radius:50%;margin:0 auto;display:flex;align-items:center;
  justify-content:center;background:conic-gradient(var(--gc,var(--accent)) calc(var(--pct,0)*1%),var(--line) 0);}
.gauge span{width:29px;height:29px;border-radius:50%;background:var(--panel);display:flex;
  align-items:center;justify-content:center;font-size:11px;font-weight:700;
  font-variant-numeric:tabular-nums;color:var(--ink);}
.rail .cap{font-family:var(--sans);font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);margin-top:5px;}
.title{font-size:17px;font-weight:600;line-height:1.28;text-wrap:balance;transition:color .12s;}
.title:hover{color:var(--accent);text-decoration:underline;text-underline-offset:2px;}
.meta{font-family:var(--sans);font-size:11.5px;letter-spacing:.02em;color:var(--muted);margin-top:4px;}
.meta .j{color:var(--ink);font-weight:500;}
#toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,14px);z-index:60;
  font-family:var(--sans);font-size:12.5px;font-weight:600;color:var(--panel);
  background:var(--ink);padding:9px 16px;border-radius:8px;pointer-events:none;
  opacity:0;transition:opacity .18s,transform .18s;
  box-shadow:0 8px 26px -12px rgba(0,0,0,.5);}
#toast.on{opacity:1;transform:translate(-50%,0);}
@media (prefers-reduced-motion:reduce){#toast{transition:none;}}
.tagbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:10px 0 0;}
.tag{display:inline-flex;align-items:center;gap:0;font-family:var(--sans);font-size:11.5px;
  font-weight:650;letter-spacing:.02em;color:var(--muted);background:var(--panel);
  border:1px solid var(--line);border-radius:999px;padding:4px 11px;cursor:pointer;
  transition:border-color .15s,color .15s,background .15s;}
.tag:hover{border-color:var(--accent);color:var(--accent);}
.tag.on{background:var(--accent);border-color:var(--accent);color:var(--panel);}
/* the pin is a second control inside the tag, so it gets its own hit area and
   must not inherit the tag's click */
.tag .pin{margin-left:7px;margin-right:-3px;opacity:0;font-size:13px;line-height:1;
  padding:0 2px;border-radius:4px;transition:opacity .12s;}
.tag:hover .pin,.tag.pinned .pin{opacity:.75;}
.tag .pin:hover{opacity:1;background:color-mix(in srgb,currentColor 18%,transparent);}
.tag.pinned{border-style:solid;border-color:color-mix(in srgb,var(--accent) 45%,var(--line));}
.sleeves{display:flex;flex-wrap:wrap;gap:5px;margin:7px 0 0;}
.sl{font-family:var(--sans);font-size:10.5px;font-weight:650;letter-spacing:.02em;
  padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--muted);
  white-space:nowrap;cursor:pointer;transition:border-color .15s,color .15s;}
.sl:hover{border-color:var(--accent);color:var(--accent);}
.sl.fitn{cursor:default;}
/* filled, not merely coloured: desk fit is the one signal here worth spotting
   from a distance, and an outline chip among outline chips does not carry */
.sl.fit{border-color:transparent;background:color-mix(in srgb,var(--accent) 14%,transparent);
  color:var(--accent);}
.sl.fitn{border-color:transparent;background:var(--accent);color:var(--panel);}
.summary{font-size:15px;line-height:1.5;color:var(--ink);margin-top:7px;max-width:63ch;}
.empty{color:var(--muted);font-style:italic;padding:56px 0;text-align:center;}
.entry.classic{grid-template-columns:1fr;}
.cwrap{display:flex;align-items:baseline;justify-content:space-between;gap:14px;}
.cites{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--cite);white-space:nowrap;}
.cites small{font-family:var(--sans);font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);font-weight:400;margin-left:3px;}
.bar{height:2px;background:var(--line);margin:8px 0 0;border-radius:2px;overflow:hidden;}
.bar i{display:block;height:100%;background:var(--cite);opacity:.55;}
.rail .yr{font-family:var(--serif);font-size:16px;font-weight:600;line-height:1;color:var(--muted);font-variant-numeric:tabular-nums;}
.tag{font-family:var(--sans);font-size:9px;letter-spacing:.09em;text-transform:uppercase;font-weight:600;
  padding:2px 7px;border-radius:20px;white-space:nowrap;border:1px solid;flex:none;}
.tag.theory{color:var(--accent);border-color:var(--accent);}
.tag.method{color:var(--medium);border-color:var(--medium);}
.tag.empirical{color:var(--strong);border-color:var(--strong);}
.tag.frontier{color:var(--panel);background:var(--accent);border-color:var(--accent);}
.tag.modern{color:var(--panel);background:var(--medium);border-color:var(--medium);}
.subs{display:flex;gap:16px;margin-top:10px;flex-wrap:wrap;}
.sub{font-family:var(--sans);display:flex;flex-direction:column;gap:3px;min-width:54px;}
.sub i{font-size:9px;letter-spacing:.09em;text-transform:uppercase;color:var(--faint);font-style:normal;}
.sub b{font-size:12px;font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums;line-height:1;}
.sub s{height:3px;width:100%;background:var(--line);border-radius:3px;display:block;text-decoration:none;overflow:hidden;}
.sub s u{display:block;height:100%;background:var(--accent);opacity:.7;border-radius:3px;
  transform-origin:left;animation:growBar .5s cubic-bezier(.2,.7,.2,1) both;}
.savebtn{font-family:var(--sans);background:none;border:0;cursor:pointer;font-size:15px;display:inline-block;
  line-height:1;color:var(--faint);padding:0;margin-left:8px;vertical-align:-1px;transition:color .15s,transform .15s;}
.savebtn:hover{color:var(--accent);transform:scale(1.18);}
.savebtn.on{color:var(--medium);}
.ftmark{font-family:var(--sans);font-size:10px;font-weight:700;letter-spacing:.04em;
  text-transform:uppercase;margin-left:8px;padding:1px 6px;border-radius:4px;
  color:var(--cite);background:color-mix(in srgb,var(--cite) 12%,transparent);
  white-space:nowrap;}
.pdfbtn{font-family:var(--sans);font-size:10px;font-weight:700;letter-spacing:.04em;
  color:var(--accent);border:1px solid var(--accent);border-radius:4px;padding:1px 5px;
  margin-left:8px;text-decoration:none;vertical-align:1px;display:inline-block;
  transition:background .15s,color .15s,transform .15s;}
.pdfbtn:hover{background:var(--accent);color:var(--panel);transform:translateY(-1px);}
.loadmore{display:block;width:100%;font-family:var(--sans);font-size:13px;font-weight:600;
  color:var(--muted);background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:12px;margin:14px 0 0;cursor:pointer;transition:border-color .15s,color .15s;}
.loadmore .n{color:var(--faint);font-weight:400;}
.loadmore:hover{border-color:var(--accent);color:var(--accent);}
.wtag{font-family:var(--sans);font-size:10px;font-weight:700;letter-spacing:.04em;
  color:var(--panel);background:var(--medium);border-radius:4px;padding:1px 6px;margin-right:7px;}
.fresh{font-family:var(--sans);font-size:11.5px;font-weight:600;color:var(--muted);
  margin-bottom:8px;letter-spacing:.02em;}
footer{font-family:var(--sans);font-size:11px;line-height:1.6;color:var(--faint);
  margin-top:40px;border-top:1px solid var(--line);padding-top:16px;}
@keyframes entryIn{from{opacity:0;transform:translateY(7px);}to{opacity:1;transform:translateY(0);}}
@keyframes growBar{from{transform:scaleX(0);}to{transform:scaleX(1);}}
.entry,.dateline,.sechead{animation:entryIn .3s cubic-bezier(.2,.7,.2,1) both;}
@media (prefers-reduced-motion:reduce){
  *{animation-duration:.001ms!important;animation-iteration-count:1!important;
    transition-duration:.001ms!important;scroll-behavior:auto!important;}
}
.katex{font-size:1.02em;}
.katex-display{overflow-x:auto;overflow-y:hidden;padding:2px 0;}
</style>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>
</head>
<body>
<header>
  <div class="mast">
    <div class="brandrow">
      <div>
        <div class="brand"><span class="the">The</span> Quant Research Digest</div>
        <div class="tagline">A private review of quantitative finance</div>
      </div>
      <button class="toggle" id="toggle">Dark</button>
    </div>
    <div class="doublerule"></div>
    <div class="navtabs" role="tablist" aria-label="Sections">
      <button id="g-papers" class="on">Papers</button>
      <button id="g-ask">Ask</button>
      <button id="g-shelf">Shelf</button>
      <button id="g-saved">Saved</button>
    </div>
    <div class="subtabs" id="subtabs">
      <button id="t-recent" class="on">Recent</button>
      <button id="t-foryou">For You</button>
      <button id="t-watched">Watched</button>
      <button id="t-nber">NBER</button>
      <button id="t-monthly">Monthly</button>
      <button id="t-practitioners">Practitioners</button>
      <button id="t-archive">Archive</button>
      <button id="t-classics">Classics</button>
      <button id="t-anchors">Anchors</button>
      <button id="t-ask" hidden></button>
      <button id="t-saved" hidden></button>
    </div>
    <div class="tagbar" id="tagbar" hidden></div>
    <div class="navtools">
      <select id="cat" title="Category" style="display:none">
        <option value="all">All categories</option>
        <option value="0">Academic · Tier 1</option>
        <option value="1">Academic · Tier 2</option>
        <option value="2">Preprints &amp; working papers</option>
      </select>
      <select id="topic" title="Topic" style="display:none"></select>
      <select id="psrc" title="Source" style="display:none"></select>
      <select id="jsel" title="Journal" style="display:none"></select>
      <select id="month" style="display:none"></select>
      <select id="nbermonth" title="NBER month" style="display:none"></select>
      <span class="sp"></span>
      <span class="searchwrap"><input id="q" type="search" placeholder="Search…" autocomplete="off"></span>
    </div>
  </div>
</header>
<main class="wrap"><div id="view"></div>
  <footer><div id="freshness" class="fresh"></div>
    Rubric scores are an ensemble consensus of multiple LLMs (Groq · Mistral ·
    OpenAI) — skim, don't trust blindly. Practitioner posts are listed as-is, not scored.
    Sources: NEP · NBER · arXiv · finance journals &amp; SSRN via Crossref · PM-Research ·
    OpenAlex · practitioner &amp; asset-manager research.
  </footer>
</main>
<script>
let DATA=[], CLASSICS=[], MONTHLY={}, NBER={}, VIEW="recent", MAXSEEN="";
let ARCHIVE_DATA=null, archiveLoading=false;
const TOPICS=__TOPICS_JSON__;
const SLEEVES=__SLEEVES_JSON__;
const SLEEVE_LABEL=Object.fromEntries(SLEEVES);
const PINS_KEY='qd_pins_v1', PIN_MAX=4;
let SLEEVE='all', PINS=[];
const BASE_PAPERS=['recent','foryou','watched','nber','monthly','practitioners','archive'];
let _toastT=null;
function toast(msg){
  let el=$('toast');
  if(!el){el=document.createElement('div');el.id='toast';document.body.appendChild(el);}
  el.textContent=msg;el.classList.add('on');
  clearTimeout(_toastT);_toastT=setTimeout(()=>el.classList.remove('on'),2600);
}
const isPrac=x=>String(x.section)==="4";   // practitioner blogs + house research
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtK=n=>n>=1000?(n/1000).toFixed(1)+'k':String(n);
const bandColor=s=>s>=70?'var(--strong)':s>=45?'var(--medium)':'var(--low)';
const jlabel=x=>String(x.source||'').replace(/^journal:/,'').replace(/^topic:/,'topic · ');

// "Saved" bucket: kept in this browser's localStorage (no backend) so a
// star click here works the same across every tab -- Recent/For You/
// Monthly/Classics/Archive all render entries through the same _saveBtn.
let SAVED={};
try{SAVED=JSON.parse(localStorage.getItem('qd_saved_v1')||'{}');}catch(e){SAVED={};}
let ITEM_BY_URL={};
function persistSaved(){localStorage.setItem('qd_saved_v1',JSON.stringify(SAVED));}
function pushSavedToServer(){
  fetch('/api/saved',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(SAVED)}).catch(()=>{});
}
async function syncSavedFromServer(){
  try{
    const r=await fetch('/api/saved');
    if(!r.ok)return;
    const remote=await r.json();
    SAVED={...SAVED,...remote};   // remote wins per-key; local-only additions survive the merge
    persistSaved();
    if(VIEW==='saved')renderSaved();
    pushSavedToServer();          // reconcile anything local-only back up to KV
  }catch(e){/* offline or KV not provisioned yet -- localStorage still works standalone */}
}
// Link straight to the paper's own publicly-hosted PDF instead of the
// abstract page -- we never fetch/store a copy ourselves, just resolve the
// URL pattern of sources that are unconditionally open (arXiv, NBER working
// papers) or already point at a PDF directly. SSRN/journal links have no
// reliable public-PDF pattern, so they fall through to the normal link.
function _pdfUrl(x){
  const u=x.url||'';
  let m=u.match(/arxiv\\.org\\/abs\\/([^\\/?#]+)/i);
  if(m)return 'https://arxiv.org/pdf/'+m[1];
  m=u.match(/nber\\.org\\/papers\\/w(\\d+)/i);
  if(m)return `https://www.nber.org/system/files/working_papers/w${m[1]}/w${m[1]}.pdf`;
  if(/\\.pdf(\\?|$)/i.test(u))return u;
  // The url is not the only thing that identifies a paper. Anything ingested
  // BY DOI -- the classics, most of the NBER block -- carries a doi: uid and a
  // doi.org link, so matching the url alone missed several hundred papers
  // whose PDF is perfectly derivable from the identifier itself.
  const id=x.uid||'';
  m=id.match(/^doi:10\\.3386\\/w(\\d+)$/i);
  if(m)return `https://www.nber.org/system/files/working_papers/w${m[1]}/w${m[1]}.pdf`;
  m=id.match(/^doi:10\\.48550\\/arxiv\\.(.+?)(?:v\\d+)?$/i);
  if(m)return 'https://arxiv.org/pdf/'+m[1];
  m=id.match(/^arxiv:(.+?)(?:v\\d+)?$/i);
  if(m)return 'https://arxiv.org/pdf/'+m[1];
  // Last: a url a resolver actually found and we actually fetched. Nothing
  // is derivable here -- it is known only because a full-text run went
  // looking, so it covers the papers the patterns above cannot reach.
  return FT_PDF[id]||null;
}
// We parsed this paper's PDF into passages, so Ask can quote it by section
// rather than paraphrase an abstract. That is worth knowing BEFORE you ask.
function _ftBtn(x){
  return (FT_SET&&x.uid&&FT_SET.has(x.uid))
    ?'<span class="ftmark" title="Full text parsed - Ask can quote this paper by section">full text</span>':'';
}
function _pdfBtn(x){
  const p=_pdfUrl(x);
  return p?`<a class="pdfbtn" href="${esc(p)}" target="_blank" rel="noopener" title="Open the publisher's own PDF — save from there">PDF</a>`:'';
}
function _saveBtn(x){
  if(!x||!x.url)return'';
  const on=!!SAVED[x.url];
  return `<button class="savebtn${on?' on':''}" data-url="${esc(x.url)}" title="${on?'Remove from Saved':'Save for later'}" onclick="toggleSave(event,this)">${on?'★':'☆'}</button>`;
}
function toggleSave(ev,btn){
  ev.preventDefault();ev.stopPropagation();
  const url=btn.dataset.url;
  if(SAVED[url]){delete SAVED[url];btn.classList.remove('on');btn.textContent='☆';btn.title='Save for later';}
  else{
    const item=ITEM_BY_URL[url]||{url};
    SAVED[url]={...item,_savedAt:Date.now()};
    btn.classList.add('on');btn.textContent='★';btn.title='Remove from Saved';
  }
  persistSaved();
  pushSavedToServer();
  if(VIEW==='saved')renderSaved();
}
function renderSaved(){
  const q=$('q').value.toLowerCase().trim();
  const rows=sleeveFilter(Object.values(SAVED))
    .filter(x=>!q||(x.title+' '+(x.authors||'')+' '+(x.source||x.journal||'')).toLowerCase().includes(q))
    .sort((a,b)=>(b._savedAt||0)-(a._savedAt||0));
  $('view').innerHTML=`<div class="dateline">Saved <span class="n">· ${rows.length} papers · this browser only</span></div>`+
    (rows.length?rows.map(x=>entry(x)).join(''):'<div class="empty">Nothing saved yet — click the ☆ on any paper.</div>');
}

const CATS=[
 {label:"Academic · Tier 1 — top journals",cls:""},
 {label:"Academic · Tier 2 — field &amp; practitioner journals",cls:"t2"},
 {label:"Preprints &amp; working papers",cls:"t2"},
 {label:"Practitioner &amp; blogs",cls:"t2"},
];
const catOf=x=>x.tier==="T1"?0:x.tier==="T2"?1:String(x.section)==="4"?3:2;
const byDate=(a,b)=>String(b.date||b.seen||'').localeCompare(String(a.date||a.seen||''));

function entry(x,rank){
  const rk=rank?`<div class="rank">${rank}</div>`:'';
  const dv=x._displayScore!=null?x._displayScore:x.score;
  const cap=x._displayLabel||'rating';
  // 55% of the archive is unscored, and every one of those cards reserved a
  // 52px rail to display nothing. A card with no score drops the column
  // entirely and marks WHY it has none, which is more informative than a gap:
  // a practitioner post is never scored, an unscored paper is merely waiting.
  const prac=isPrac(x);
  const cls=(dv!=null)?'entry':'entry flat';
  const badge=(dv!=null)?'':(prac
    ? '<span class="ntag">practitioner</span>'
    : '<span class="ntag wait">not yet scored</span>');
  const sc=(dv!=null)
    ?`<div class="rail">${rk}<div class="gauge" style="--pct:${dv};--gc:${bandColor(dv)}"><span>${dv}</span></div><div class="cap">${cap}</div></div>`
    :(rk?`<div class="rail">${rk}</div>`:'');
  const sm=x.summary?`<div class="summary">${esc(x.summary)}</div>`:'';
  const who=x.authors?' · '+esc(x.authors):'';
  const lvl=v=>v==null?'–':v+'/3';
  const prov=x.contribution_provisional?' <span style="opacity:.55">(prov.)</span>':'';
  const ctype=(x.novelty_type&&x.novelty_type!=='none')?' ('+esc(x.novelty_type)+')':'';
  const hasScores=(x.generality!=null||x.contribution!=null||x.testability!=null||x.novelty_posterior!=null);
  const subs=hasScores?'<div class="subs">'+[
    _subBar('Relevance (vs history)',x.relevance_posterior!=null?Math.round(x.relevance_posterior*100)+'%':'–',(x.relevance_posterior||0)*100),
    _subBar('Generality',lvl(x.generality),(x.generality||0)/3*100),
    _subBar('Contribution'+ctype,lvl(x.contribution)+prov,(x.contribution||0)/3*100),
    _subBar('Testability',lvl(x.testability),(x.testability||0)/3*100),
    _subBar('Novelty vs history',x.novelty_posterior!=null?Math.round(x.novelty_posterior*100)+'%':'–',(x.novelty_posterior||0)*100),
    (x.author_score!=null?_subBar('Author',Math.round(x.author_score),x.author_score):''),
  ].join('')+'</div>':'';
  const watch=x.watchlist?`<span class="wtag">★ ${esc(x.watchlist_author||'watched')}</span>`:'';
  // sleeve chips: 'other' is the classifier's null answer, so showing it adds
  // nothing. desk_fit 2+ means usable on the desk, and gets a filled chip.
  const sl=(x.sleeves||[]).filter(k=>k!=='other');
  const fit=(x.desk_fit||0)>=2;
  const chips=sl.length?'<div class="sleeves">'+sl.map(k=>
    `<span class="sl${fit?' fit':''}" data-sleeve="${k}" title="Filter to ${esc(SLEEVE_LABEL[k]||k)}">${esc(SLEEVE_LABEL[k]||k)}</span>`).join('')+
    (fit?`<span class="sl fitn" title="usable on the desk">desk ${x.desk_fit}/3</span>`:'')+'</div>':'';
  return `<div class="${cls}">${sc}<div class="body">${badge}
    <a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>
    <div class="meta">${watch}<span class="j">${esc(jlabel(x))}</span>${who} · ${esc(x.date||x.seen)}${x.topic?' · '+esc(x.topic):''}${x.consensus_n?' · '+x.consensus_n+'× '+(x.consensus_agree?'agree':'split'):''}${_ftBtn(x)}${_pdfBtn(x)}${_saveBtn(x)}</div>
    ${chips}${sm}${subs}</div></div>`;
}
// Desk sleeves are MULTI-LABEL -- a paper can be carry AND fx at once -- so this
// is a membership test, not equality. Papers with no labels yet are HIDDEN when
// a sleeve is chosen rather than shown: an unlabelled paper is not evidence of
// belonging, and the backfill is still filling them in.
function sleeveFilter(rows,force){
  const s=force||SLEEVE;
  if(s==='all')return rows;
  if(s==='fit')return rows.filter(x=>(x.desk_fit||0)>=2);
  return rows.filter(x=>(x.sleeves||[]).indexOf(s)>=0);
}
// A sleeve is worth a permanent tab once you read it daily, and worth nothing
// once you don't -- so which ones get promoted is the reader's call, not a
// build-time decision. Pins live in this browser, like Saved.
function loadPins(){
  try{return JSON.parse(localStorage.getItem(PINS_KEY)||'[]').filter(k=>SLEEVE_LABEL[k]);}
  catch(e){return[];}
}
function savePins(){try{localStorage.setItem(PINS_KEY,JSON.stringify(PINS));}catch(e){}}
function setSleeve(k){
  SLEEVE=(SLEEVE===k)?'all':k;
  // choosing a sleeve from a pinned tab's view would be two filters fighting;
  // step back to Archive, where the tag rail is the only thing filtering
  if(VIEW.slice(0,3)==='sl:'){setView('archive');return;}
  archivePage=0;renderTagbar();render();
}
function togglePin(k,ev){
  ev.stopPropagation();
  const i=PINS.indexOf(k);
  if(i>=0){
    PINS.splice(i,1);
    const b=$('t-sl:'+k); if(b)b.remove();
    if(VIEW==='sl:'+k){savePins();rebuildViews();setView('archive');renderTagbar();return;}
  } else {
    if(PINS.length>=PIN_MAX){toast('Up to '+PIN_MAX+' pinned sleeves — unpin one first.');return;}
    PINS.push(k);
  }
  savePins();rebuildViews();renderPinTabs();renderTagbar();
}
// GROUPS is what setGroup/setView iterate, so a pinned sleeve has to become a
// real view there, not a special case bolted onto the render switch
function rebuildViews(){
  GROUPS.papers=BASE_PAPERS.concat(PINS.map(k=>'sl:'+k));
  Object.keys(GROUP_OF).forEach(k=>delete GROUP_OF[k]);
  Object.entries(GROUPS).forEach(([g,vs])=>vs.forEach(v=>{GROUP_OF[v]=g;}));
  ALL_VIEWS.length=0;Object.values(GROUPS).flat().forEach(v=>ALL_VIEWS.push(v));
}
function renderPinTabs(){
  PINS.forEach(k=>{
    if($('t-sl:'+k))return;
    const b=document.createElement('button');
    b.id='t-sl:'+k;b.textContent=SLEEVE_LABEL[k]||k;
    b.onclick=()=>setView('sl:'+k);
    $('subtabs').insertBefore(b,$('t-ask'));
  });
}
function renderTagbar(){
  const on=VIEW.slice(0,3)==='sl:'?VIEW.slice(3):SLEEVE;
  const mk=(k,lab,pinnable)=>{
    const cls='tag'+(on===k?' on':'')+(PINS.indexOf(k)>=0?' pinned':'');
    const pin=pinnable?`<span class="pin" data-pin="${k}" title="${
      PINS.indexOf(k)>=0?'Unpin':'Pin as a tab'}">${PINS.indexOf(k)>=0?'✕':'+'}</span>`:'';
    return `<button class="${cls}" data-sleeve="${k}">${esc(lab)}${pin}</button>`;
  };
  $('tagbar').innerHTML=mk('all','All',false)+mk('fit','Desk fit 2+',false)+
    SLEEVES.filter(([k])=>k!=='other').map(([k,lab])=>mk(k,lab,true)).join('');
}
// one delegated listener rather than one per tag, since the rail is rebuilt on
// every click and per-node handlers would leak with it
$('tagbar').addEventListener('click',e=>{
  const pin=e.target.closest('[data-pin]');
  if(pin){togglePin(pin.dataset.pin,e);return;}
  const t=e.target.closest('[data-sleeve]');
  if(t)setSleeve(t.dataset.sleeve);
});
// the chips on a card are the same filter, reached from the paper that
// prompted the thought rather than from the rail
$('view').addEventListener('click',e=>{
  const c=e.target.closest('.sl[data-sleeve]');
  if(c)setSleeve(c.dataset.sleeve);
});
function grouped(rows){
  const q=$('q').value.toLowerCase().trim();
  const cf=$('cat').value;
  rows=sleeveFilter(rows).filter(x=>!q||(x.title+' '+x.authors+' '+x.source).toLowerCase().includes(q));
  const g=[[],[],[],[]]; rows.forEach(x=>g[catOf(x)].push(x));
  let h='';
  g.forEach((a,i)=>{ if(!a.length||(cf!=="all"&&String(i)!==cf))return; a.sort(byDate);
    h+=`<div class="sechead ${CATS[i].cls}">${CATS[i].label}<span class="cnt">${a.length}</span></div>`+a.map(x=>entry(x)).join('');
  });
  return h||'<div class="empty">No matches.</div>';
}
// like grouped(), but ignores the shared #cat select (For You has no
// category filter of its own, and that select's stale value from a
// previous Recent-tab visit would otherwise silently hide whole sections)
function byCategory(rows,sortFn){
  rows=sleeveFilter(rows);
  const g=[[],[],[],[]]; rows.forEach(x=>g[catOf(x)].push(x));
  let h='';
  g.forEach((a,i)=>{ if(!a.length)return; a.sort(sortFn||byDate);
    h+=`<div class="sechead ${CATS[i].cls}">${CATS[i].label}<span class="cnt">${a.length}</span></div>`+a.map(x=>entry(x)).join('');
  });
  return h||'<div class="empty">No matches.</div>';
}
function sinceDays(n){
  if(!MAXSEEN)return"";
  const c=new Date(MAXSEEN);c.setDate(c.getDate()-(n-1));
  return c.toISOString().slice(0,10);
}
// Recent's bar requires the Bayesian relevance posterior to clear the same
// confidence bar the pipeline uses for gating (config.RELEVANCE_CONFIDENCE,
// baked in at build time -- see portal.build), so every qualified item is a
// deliberate pass/fail against real evidence, not a flat rescaled level.
const RELEVANCE_CONFIDENCE_PCT=__RELEVANCE_CONFIDENCE_PCT__;
// Blend the dimensions that still vary among qualified items (generality,
// testability, novelty-vs-history) into a display-only strength score.
function _strengthScore(x){
  const g=x.generality||0,t=x.testability||0,np=x.novelty_posterior||0;
  return Math.max(0,Math.min(100,Math.round((g+t)/6*50+np*50)));
}
function renderRecent(){
  const cs=sinceDays(7);
  const pool=(cs?DATA.filter(x=>(x.seen||'')>=cs):DATA).filter(x=>x.score!=null&&!isPrac(x));
  // strict lane: relevance posterior at/above the bar AND a genuinely novel,
  // non-provisional contribution -- the "genuinely strong" set
  const strict=pool.filter(x=>x.score>=RELEVANCE_CONFIDENCE_PCT&&x.contribution===3&&!x.contribution_provisional);
  // trusted lane: a watched author's paper that's squarely relevant (core_fit)
  // earns a place even at contribution 2 -- you trust the person, so a solid
  // (not just groundbreaking) Kelly/Xiu/Feng paper shouldn't be gated out
  const watched=pool.filter(x=>x.watchlist&&x.relevance_category==='core_fit'
    &&!strict.includes(x));
  const seen=new Set();
  // author reputation nudges the ordering (bounded, same multiplier used in
  // the Monthly composite) -- a strong-author paper ranks a touch higher
  const _rk=x=>(x.novelty_posterior||0)*(x.reputation||1);
  const top=strict.slice().sort((a,b)=>
    _rk(b)-_rk(a)||(b.generality||0)-(a.generality||0)||byDate(a,b)
  ).slice(0,20);
  const rows=[...watched.sort(byDate),...top]
    .filter(x=>!seen.has(x.url)&&seen.add(x.url))
    .map(x=>({...x,_displayScore:_strengthScore(x),_displayLabel:'strength'}));
  const note=watched.length?` · incl. ${watched.length} from watched authors`:'';
  $('view').innerHTML=`<div class="dateline">Last 7 days · genuinely strong <span class="n">· ${top.length} of ${strict.length} that cleared the bar${note} — the rest are in Archive</span></div>`+grouped(rows);
}
// Watched authors -- everything your roster published, mirroring the email's
// "★ Watched authors" section: you trust the person, so anything they wrote
// that isn't off-topic is surfaced, whatever it scored. UNLIKE Recent's trusted
// lane this is NOT gated to the last 7 days or to core_fit -- it's the full
// standing feed of your roster (same rule as emailer._keep: watchlist item kept
// iff relevance_category !== 'off_topic', dropping name-match false positives).
function renderWatched(){
  const w=DATA.filter(x=>x.watchlist&&x.relevance_category&&x.relevance_category!=='off_topic'&&!isPrac(x));
  const seen=new Set();
  const rows=w.sort(byDate)
    .filter(x=>!seen.has(x.url)&&seen.add(x.url))
    // for a watched paper the score is a LABEL not a gate -- say "relevance N"
    .map(x=>({...x,_displayScore:x.score,_displayLabel:'relevance'}));
  const roster=new Set(rows.map(x=>x.watchlist_author).filter(Boolean));
  const note=roster.size?` from ${roster.size} watched author${roster.size===1?'':'s'}`:'';
  $('view').innerHTML=`<div class="dateline">Watched authors · everything they published <span class="n">· ${rows.length} paper${rows.length===1?'':'s'}${note} — off-topic name-matches filtered out</span></div>`+(rows.length?grouped(rows):'<div class="empty">No watched-author papers yet — they appear here as your roster publishes.</div>');
}
// Anchors -- a hand-curated shelf of foundational BOOKS for a systematic-macro
// / CTA desk (the QRT macro role). Static, not from the digest feed: these are
// the mental-model texts you read once and keep, while the feed keeps you
// current. Each has the publisher/author page; a free PDF/companion link is
// added ONLY where one is legitimately free (author-hosted or open access) --
// no pirated scans.
const ANCHORS=[
  {grp:'Orientation — how a macro book thinks',items:[
    {t:'Expected Returns: An Investor’s Guide to Harvesting Market Rewards',by:'Antti Ilmanen',yr:2011,
     why:'The single best orientation for a systematic-macro quant: every risk premium — value, carry, trend, volatility — across every asset class, framed the way the desk frames it. Read this first.',
     url:'https://www.aqr.com/Insights/Research/Book/Expected-Returns-An-Investors-Guide-to-Harvesting-Market-Rewards',ul:'AQR',
     pdf:'https://rpc.cfainstitute.org/sites/default/files/-/media/documents/book/rf-publication/2012/rf-v2012-n1-1-pdf.PDF',pl:'free CFA monograph (condensed)'},
    {t:'Investing Amid Low Expected Returns',by:'Antti Ilmanen',yr:2022,
     why:'The 2022 update to the above — same lens, recalibrated for a low-premium, post-ZIRP world. Keeps the mental model current.',
     url:'https://www.wiley.com/en-us/Investing+Amid+Low+Expected+Returns:+Making+the+Most+When+Markets+Offer+the+Least-p-9781119860198',ul:'Wiley'},
    {t:'Efficiently Inefficient: How Smart Money Invests and Market Prices Are Determined',by:'Lasse Heje Pedersen',yr:2015,
     why:'Strategy-by-strategy playbook — trend, carry, value/momentum, global macro, arbitrage — with interviews of the managers who run them. Pedersen is on your Watched roster.',
     url:'https://press.princeton.edu/books/hardcover/9780691166193/efficiently-inefficient',ul:'Princeton UP'},
  ]},
  {grp:'Systematic & CTA craft',items:[
    {t:'Trend Following with Managed Futures: The Search for Crisis Alpha',by:'Alex Greyserman & Kathryn Kaminski',yr:2014,
     why:'The book on trend/CTA: 800 years of trend evidence, the theoretical foundations, and the crisis-alpha convexity argument for why a macro book carries trend at all.',
     url:'https://www.wiley.com/en-us/Trend+Following+with+Managed+Futures:+The+Search+for+Crisis+Alpha-p-9781118890974',ul:'Wiley'},
    {t:'Advanced Futures Trading Strategies',by:'Robert Carver',yr:2023,
     why:'A modern systematic-futures cookbook — 30 tested strategies across 100+ instruments — from an ex-Man AHL PM. The most practical CTA implementation text there is.',
     url:'https://harriman-house.com/authors/robert-carver/advanced-futures-trading-strategies/9780857199683',ul:'Harriman House'},
    {t:'Active Portfolio Management: A Quantitative Approach',by:'Richard Grinold & Ronald Kahn',yr:2000,
     why:'The quant-craft bible: information ratio and the Fundamental Law of Active Management (IR ≈ IC·√breadth) — how to turn raw forecasts into sized positions.',
     url:'https://www.amazon.com/Active-Portfolio-Management-Quantitative-Controlling/dp/0070248826',ul:'McGraw-Hill'},
  ]},
  {grp:'Foundations — theory, econometrics, time series',items:[
    {t:'Asset Pricing (Revised Edition)',by:'John H. Cochrane',yr:2005,
     why:'The theory anchor: everything reduces to p = E[m·x]. The stochastic-discount-factor view that unifies bonds, equities and macro risk under one equation.',
     url:'https://press.princeton.edu/books/ebook/9781400845743/asset-pricing',ul:'Princeton UP',
     pdf:'https://www.johnhcochrane.com/asset-pricing-phd-class-stanford-edition',pl:'free PhD course (notes + video)'},
    {t:'Time Series Analysis',by:'James D. Hamilton',yr:1994,
     why:'The state-space bible: VARs, the Kalman filter, and Markov regime-switching — precisely the machinery for nowcasting the macro state and modelling regimes.',
     url:'https://press.princeton.edu/books/hardcover/9780691042893/time-series-analysis',ul:'Princeton UP'},
    {t:'Asset Management: A Systematic Approach to Factor Investing',by:'Andrew Ang',yr:2014,
     why:'Factors as the organising principle across every asset class — bridges academic factor theory and how a systematic desk actually allocates capital.',
     url:'https://global.oup.com/academic/product/asset-management-9780199959327',ul:'Oxford UP'},
  ]},
];
function renderAnchors(){
  const q=$('q').value.toLowerCase().trim();
  const card=x=>{
    const pdf=x.pdf?` · <a href="${x.pdf}" target="_blank" rel="noopener" style="color:var(--accent);font-weight:600">⭳ ${esc(x.pl||'PDF')}</a>`:'';
    // "flat": these cards carry no score rail, and plain .entry reserves a
    // 52px column for one -- which squeezed every anchor into a third of the card
    return `<div class="entry flat"><div class="body">
      <a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.t)}</a>
      <div class="meta"><span class="j">${esc(x.by)}</span> · ${x.yr} · <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.ul)}</a>${pdf}</div>
      <div class="summary">${esc(x.why)}</div></div></div>`;
  };
  const match=x=>!q||(x.t+' '+x.by+' '+x.why).toLowerCase().includes(q);
  const secs=ANCHORS.map(g=>{const it=g.items.filter(match);return it.length?
    `<div class="sechead t1">${esc(g.grp)}<span class="cnt">${it.length}</span></div>`+it.map(card).join(''):'';
  }).join('');
  $('view').innerHTML=`<div class="dateline">Anchors <span class="n">· foundational books for a systematic-macro / CTA desk — read once and keep, while the feed keeps you current · free PDF linked where legitimately available</span></div>`+(secs||'<div class="empty">No matches.</div>');
}
// ---------------------------------------------------------------- Ask
// A research agent over the whole archive. Retrieval happens HERE, in the
// browser: docs/vec.bin is an int8 matrix (one 256-dim unit vector per paper,
// built by tools/embed.py), so ranking 5.8k papers is ~1.5M integer multiply-
// adds -- a few milliseconds locally, and nothing but the question leaves the
// page until we have the shortlist. /api/ask (a Cloudflare Pages Function
// holding the API key) only embeds the question and writes the final synthesis.
let VEC=null,VEC_UIDS=null,VEC_DIM=0,VEC_SHARD=64,ITEM_BY_UID={},indexLoading=false;
// Ask is a CONVERSATION, not a series of one-shot queries. Turns are kept in
// order and replayed to the model, so "why?" or "what about costs?" resolve
// against what was actually said instead of starting cold every time.
const CHATS_KEY='qd_chats_v1', QUEUE_KEY='qd_queued_v1';
const CHAT_MAX=20;         // conversations kept in this browser
const CHAT_TURNS=12;       // turns persisted per conversation
const HIST_SEND=6;         // turns replayed to the model
const OUTSIDE_SHOW=8;      // outside hits listed under an answer
const OUTSIDE_CTX=4;       // outside hits the agent is allowed to see
let CHATS=[],CHAT_ID=null,asking=false,ASK_OUTSIDE=true,QUEUED={};
const curChat=()=>CHATS.find(c=>c.id===CHAT_ID)||CHATS[0];
const curTurns=()=>{const c=curChat();return c?(c.turns=c.turns||[]):[];};
function newChat(quiet){
  const c={id:'c'+Date.now().toString(36)+Math.random().toString(36).slice(2,6),
           title:'New conversation',ts:Date.now(),turns:[]};
  CHATS.unshift(c);CHAT_ID=c.id;
  if(CHATS.length>CHAT_MAX)CHATS.length=CHAT_MAX;
  if(!quiet){saveChats();renderAsk();}
  return c;
}
function loadChats(){
  try{CHATS=JSON.parse(localStorage.getItem(CHATS_KEY)||'[]')||[];}catch(e){CHATS=[];}
  if(!Array.isArray(CHATS)||!CHATS.length)CHATS=[];
  CHATS.forEach(c=>{c.turns=c.turns||[];});
  if(!CHATS.length)newChat(true); else CHAT_ID=CHATS[0].id;
  try{QUEUED=JSON.parse(localStorage.getItem(QUEUE_KEY)||'{}')||{};}catch(e){QUEUED={};}
}
function saveChats(){
  // Only what is needed to REDRAW and to REPLAY. A turn's passages and full
  // source records are re-derivable and are by far the biggest part of it,
  // and localStorage is a ~5MB budget already shared with Saved and the
  // answer cache -- persisting everything would evict the conversations.
  try{
    localStorage.setItem(CHATS_KEY,JSON.stringify(CHATS.slice(0,CHAT_MAX).map(c=>({
      id:c.id,title:c.title,ts:c.ts,
      turns:(c.turns||[]).filter(t=>t.state==='done').slice(-CHAT_TURNS).map(t=>({
        q:t.q,answer:t.answer,state:'done',ts:t.ts,cached:t.cached,
        sources:(t.sources||[]).map(x=>({title:x.title,url:x.url,authors:x.authors,
          date:x.date,seen:x.seen,source:x.source,score:x.score,uid:x.uid,
          _depth:x._depth,_sec:x._sec})),
        outside:(t.outside||[]).slice(0,OUTSIDE_SHOW)}))
    }))));
  }catch(e){}
}
function persistQueued(){try{localStorage.setItem(QUEUE_KEY,JSON.stringify(QUEUED));}catch(e){}}
// Everything the archive already holds, so an outside hit that is simply a
// paper we own is never offered as a discovery.
function knownUids(){
  const s=new Set(VEC_UIDS||[]);
  (ARCHIVE_DATA||[]).forEach(x=>{if(x.uid)s.add(String(x.uid).toLowerCase());});
  (VEC_UIDS||[]).forEach(u=>s.add(String(u).toLowerCase()));
  return s;
}
// Two-stage selection, both stages free and instant because they reuse work the
// pipeline already did. Stage 1: cosine recall over every paper. Stage 2: blend
// that similarity with the paper's OWN pipeline scores (strength, reputation)
// and a keyword hit-rate against the question, then keep the finalists. No
// extra LLM call to rank -- the model is spent only on the final synthesis.
const ASK_RECALL=200;                 // candidates pulled by embedding similarity
const ASK_SCAN=48;                    // papers actually examined for content
const ASK_DEEP=12;                    // papers read in full (top of the ranking)
const SCAN_BATCH=16;                  // papers per screening call; fanned out in parallel
// weights: relevance to the QUESTION dominates; the archive's own quality score
// breaks ties so a strong paper outranks a mediocre one on equal topical fit
const W_SIM=0.55,W_KW=0.30,W_QUALITY=0.15;
const ASK_STOP=new Set(('the a an of and or to in on for with is are be as by at from that this what which how does do did why '
 +'when we our their its it also than then these those between across over under more most less least there here into out '
 +'about after before during any some all can could would should may might will shall must have has had been being').split(' '));
function qTerms(q){
  return [...new Set(String(q||'').toLowerCase().replace(/[^a-z0-9 ]/g,' ').split(/\\s+/)
    .filter(t=>t.length>2&&!ASK_STOP.has(t)))];
}
function kwHit(terms,text){
  if(!terms.length||!text)return 0;
  const t=' '+String(text).toLowerCase()+' ';
  let n=0;terms.forEach(w=>{if(t.indexOf(w)>=0)n++;});
  return n/terms.length;
}
// the archive's own verdict on the paper, independent of the question
function askQuality(x){
  const scored=(x.generality!=null||x.testability!=null||x.novelty_posterior!=null);
  // unscored papers sit just below average rather than being buried -- ~42% of
  // the archive is still unscored, and some of it is genuinely relevant
  const q=scored?_strengthScore(x)/100:0.45;
  return Math.max(0,Math.min(1.2,q*(x.reputation||1)));
}
function askRank(x,terms){
  const sim=Math.max(0,Math.min(1,(x._sim||0)/(127*127)));
  const kw=0.6*kwHit(terms,x.title)+0.4*kwHit(terms,x.summary);
  return W_SIM*sim+W_KW*kw+W_QUALITY*askQuality(x);
}
const ASK_EXAMPLES=[
  "What does the archive say about the stock-bond correlation flip?",
  "Is there evidence trend following has decayed since 2010?",
  "What drives commodity carry beyond backwardation?",
  "Summarise recent work on term premium estimation.",
];
function loadIndex(cb){
  if(VEC&&ARCHIVE_DATA){cb();return;}
  if(indexLoading)return;
  indexLoading=true;
  Promise.all([
    fetch('vec.json').then(r=>r.json()),
    fetch('vec.bin').then(r=>r.arrayBuffer()),
    ARCHIVE_DATA?Promise.resolve(ARCHIVE_DATA):fetch('archive.json').then(r=>r.json()),
  ]).then(([meta,buf,arch])=>{
    VEC_UIDS=meta.uids;VEC=new Int8Array(buf);VEC_DIM=meta.dim||0;VEC_SHARD=meta.shard||64;
    ARCHIVE_DATA=arch;
    arch.forEach(x=>{if(x.uid)ITEM_BY_UID[x.uid]=x;if(x.url)ITEM_BY_URL[x.url]=x;});
    indexLoading=false;
    if(VIEW==='ask')cb();
  }).catch(()=>{
    indexLoading=false;
    if(VIEW==='ask')renderAsk('The semantic index has not been built yet — run the "Semantic Index" workflow once.');
  });
}
// cosine over unit vectors == dot product; int8 rounding is monotonic, so the
// 1/127 scale factor is irrelevant to the ranking and never applied
// Stage 1 -- recall by cosine over every indexed paper. Each candidate carries
// its raw dot product (_sim) and its vector row (_row, which addresses the
// abstract shard), so the blend stage has everything it needs without a
// second pass over the matrix.
function retrieve(qv,k){
  const dim=qv.length,n=VEC_UIDS.length,out=[];
  for(let i=0;i<n;i++){
    let s=0,off=i*dim;
    for(let j=0;j<dim;j++)s+=qv[j]*VEC[off+j];
    out.push([s,i]);
  }
  out.sort((a,b)=>b[0]-a[0]);
  const seen=new Set(),picks=[];
  for(const [sim,i] of out){
    const it=ITEM_BY_UID[VEC_UIDS[i]];
    if(!it||seen.has(it.title))continue;
    seen.add(it.title);
    picks.push(Object.assign({},it,{_row:i,_sim:sim}));
    if(picks.length>=k)break;
  }
  return picks;
}
// ---- Ask memory -------------------------------------------------------
// Once a paper has been read its text is kept, so later questions that surface
// the same paper cost nothing: no shard re-fetch, no re-reading. Answers are
// kept too, keyed by the question's content words, so repeating a question is
// instant. Both live in localStorage next to the Saved bucket, LRU-evicted to
// stay well inside the ~5 MB quota.
let READ={},ANS={};
try{READ=JSON.parse(localStorage.getItem('qd_read_v1')||'{}');}catch(e){READ={};}
try{ANS=JSON.parse(localStorage.getItem('qd_ans_v1')||'{}');}catch(e){ANS={};}
const READ_MAX=1200,ANS_MAX=60;
function _evict(o,max){
  const ks=Object.keys(o);
  if(ks.length>max){
    ks.sort((a,b)=>(o[a].t||0)-(o[b].t||0));
    ks.slice(0,ks.length-max).forEach(k=>{delete o[k];});
  }
  return o;
}
function persistRead(){try{localStorage.setItem('qd_read_v1',JSON.stringify(_evict(READ,READ_MAX)));}catch(e){}}
function persistAns(){try{localStorage.setItem('qd_ans_v1',JSON.stringify(_evict(ANS,ANS_MAX)));}catch(e){}}
// order-insensitive key: "trend decay evidence" == "evidence of trend decay"
function qKey(q){
  return qTerms(q).slice().sort().join(' ');
}
function readCount(){return Object.keys(READ).length;}
// Full abstracts live in row-indexed shards (docs/abs/N.json) so we can pull
// the real text for the handful of finalists without shipping ~12 MB of
// abstracts to every visitor. Papers already in READ skip the network entirely.
// ---- full text ---------------------------------------------------------
// Papers parsed by GROBID have their passages in docs/ft/<uid>.json. The
// manifest says which, so the client never probes for files that don't exist.
let FT_SET=null,FT_PDF={},ftLoading=false;
const FT_CACHE={};
const FT_PAPERS=8;                    // papers to open at passage depth
const FT_PASSAGES=10;                 // passages carried into the answer
function loadFtIndex(){
  if(FT_SET||ftLoading)return Promise.resolve();
  ftLoading=true;
  return fetch('ft/index.json').then(r=>r.json())
    .then(j=>{FT_SET=new Set(j.uids||[]);FT_PDF=j.pdfs||{};ftLoading=false;})
    .catch(()=>{FT_SET=new Set();ftLoading=false;});
}
async function loadPassages(picks){
  const want=picks.filter(p=>FT_SET&&FT_SET.has(p.uid)).slice(0,FT_PAPERS);
  await Promise.all(want.filter(p=>!(p.uid in FT_CACHE)).map(p=>
    fetch('ft/'+p.uid.replace(/[^A-Za-z0-9._-]/g,'_').slice(0,120)+'.json')
      .then(r=>r.json()).then(j=>{FT_CACHE[p.uid]=j.p||[];})
      .catch(()=>{FT_CACHE[p.uid]=[];})));
  // The same paper can sit in the archive twice (an arXiv id and a title hash
  // for the mailing-list copy), so identical passages arrive twice and would
  // burn two context slots and two citation numbers on one source. Dedupe on
  // the text itself, which catches it regardless of why the uids differ.
  const out=[],seenTxt=new Set();
  want.forEach(p=>{(FT_CACHE[p.uid]||[]).forEach((ps,k)=>{
    const t=ps.t||'';
    const key=t.slice(0,160);
    if(!t||seenTxt.has(key))return;
    seenTxt.add(key);
    out.push({paper:p,sec:ps.s||'',text:t,k:k});});});
  return out;
}
// BM25 over the candidate passages. Lexical scoring is the right tool at this
// level: within one paper every passage is topically identical, so what marks
// the relevant one is the presence of specific terms -- exactly what embeddings
// blur and BM25 sharpens. Length normalisation is well behaved here because
// passages are near-uniform (~220 words) rather than mixing abstracts with
// full papers, which is where the b parameter starts to matter.
function bm25(terms,docs,k1,b){
  k1=k1||1.5;b=b||0.75;
  const N=docs.length;
  if(!N||!terms.length)return [];
  const toks=docs.map(d=>String(d.text||'').toLowerCase().replace(/[^a-z0-9 ]/g,' ').split(/\\s+/).filter(Boolean));
  const avgdl=toks.reduce((s,t)=>s+t.length,0)/N;
  const df={};
  terms.forEach(t=>{df[t]=toks.reduce((n,tk)=>n+(tk.indexOf(t)>=0?1:0),0);});
  return docs.map((d,i)=>{
    const tk=toks[i],len=tk.length||1;
    let s=0;
    terms.forEach(t=>{
      const n=df[t];
      if(!n)return;
      let f=0;for(let j=0;j<tk.length;j++)if(tk[j]===t)f++;
      if(!f)return;
      const idf=Math.log(1+(N-n+0.5)/(n+0.5));
      s+=idf*(f*(k1+1))/(f+k1*(1-b+b*len/avgdl));
    });
    return Object.assign({},d,{_bm25:s});
  }).filter(d=>d._bm25>0).sort((a,b2)=>b2._bm25-a._bm25);
}
const ABS_SHARD={};
async function loadAbstracts(picks){
  const miss=picks.filter(p=>!(READ[p.uid]&&READ[p.uid].a));
  if(miss.length){
    const need=[...new Set(miss.map(p=>Math.floor(p._row/VEC_SHARD)))]
      .filter(s=>!(s in ABS_SHARD));
    await Promise.all(need.map(s=>
      fetch('abs/'+s+'.json').then(r=>r.json()).then(j=>{ABS_SHARD[s]=j;})
        .catch(()=>{ABS_SHARD[s]={};})));
    miss.forEach(p=>{
      const sh=ABS_SHARD[Math.floor(p._row/VEC_SHARD)]||{};
      const a=sh[String(p._row)]||'';
      if(a)READ[p.uid]={a:a,t:Date.now()};
    });
    persistRead();
  }
  const out={};
  picks.forEach(p=>{
    const r=READ[p.uid];
    if(r){out[p.uid]=r.a;r.t=Date.now();}else{out[p.uid]='';}
  });
  return {abs:out,fetched:miss.length,reused:picks.length-miss.length};
}
// The prompt now invites LaTeX, so it has to render -- otherwise a moment
// condition arrives on screen as raw backslashes. Guarded because the CDN
// scripts load with `defer` and a question can be answered before they land.
function typesetMath(el){
  if(!el||typeof renderMathInElement!=='function')return;
  try{
    renderMathInElement(el,{delimiters:[
      {left:'$$',right:'$$',display:true},
      {left:'\\\\[',right:'\\\\]',display:true},
      {left:'$',right:'$',display:false},
      {left:'\\\\(',right:'\\\\)',display:false},
    ],throwOnError:false,ignoredTags:['script','style','textarea','pre','code']});
  }catch(e){}
}
const _mdEsc=s=>esc(s);
function md(t,ti){
  const lines=String(t||'').split('\\n');let h='',ul=false;
  for(let raw of lines){
    let l=_mdEsc(raw.trim());
    l=l.replace(/\\*\\*([^*]+)\\*\\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
    // anchor citations to THIS answer's source list, not the newest one's
    l=l.replace(/\\[(\\d+)\\]/g,'<a class="cite" href="#src-'+ti+'-$1">[$1]</a>');
    if(/^[-*]\\s+/.test(raw.trim())){
      if(!ul){h+='<ul>';ul=true;}
      h+='<li>'+l.replace(/^[-*]\\s+/,'')+'</li>';continue;
    }
    if(ul){h+='</ul>';ul=false;}
    if(l)h+='<p>'+l+'</p>';
  }
  if(ul)h+='</ul>';
  return h;
}
async function doAsk(q,force){
  if(asking||!q.trim())return;
  if(!VEC||!VEC_UIDS){renderAsk('The semantic index has not been built yet — run the "Semantic Index" workflow once.');return;}
  const turns=curTurns();
  // The cache is keyed by question alone, so it is only safe on the FIRST turn.
  // Mid-conversation the same words mean something different -- "and in FX?"
  // depends entirely on what came before -- and replaying a stored answer there
  // would silently ignore the thread.
  const key=qKey(q),hit=(force||turns.length)?null:ANS[key];
  if(hit){
    hit.t=Date.now();persistAns();
    turns.push({q:q,state:'done',answer:hit.answer,cached:true,ts:Date.now(),
      sources:(hit.uids||[]).map(u=>ITEM_BY_UID[u]).filter(Boolean)});
    titleChat();saveChats();renderAsk();return;
  }
  asking=true;
  const turn={q:q,state:'retrieving',ts:Date.now()};
  turns.push(turn);titleChat();
  renderAsk();
  try{
    const er=await fetch('/api/ask',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({mode:'embed',q:q})});
    const ej=await er.json();
    if(!er.ok)throw new Error(ej.error||'embed failed');
    // a width mismatch means query and index are in different vector spaces --
    // retrieval would return confident nonsense, so refuse rather than guess
    if(VEC_DIM&&ej.vec.length!==VEC_DIM)
      throw new Error('the question embedded to '+ej.vec.length+' dimensions but the index is '+VEC_DIM+'-dimensional — rebuild the index (Semantic Index workflow) so both use the same model');
    // stage 1: broad recall by similarity
    const cands=retrieve(ej.vec,ASK_RECALL);
    // stage 2: re-order by question-relevance BLENDED with the archive's own
    // scores -- free, instant, and it reuses work the pipeline already did
    const terms=qTerms(q);
    cands.forEach(c=>{c._rank=askRank(c,terms);});
    cands.sort((a,b)=>b._rank-a._rank);
    const scanSet=cands.slice(0,ASK_SCAN);
    turn.scanned=cands.length;turn.state='reading';
    renderAsk();
    // pull the FULL abstracts for the whole examined set (archive.json only
    // carries a 400-char summary). Papers read for an earlier question come
    // straight from memory.
    let got={abs:{},fetched:0,reused:0};
    try{ got=await loadAbstracts(scanSet); }catch(e){ got={abs:{},fetched:0,reused:0}; }
    scanSet.forEach(p=>{p._full=got.abs[p.uid]||'';});
    turn.reused=got.reused;
    // Top of the ranking is read in full. Everything else is SCREENED: a paper
    // ranked 30th can still hold the one number that answers the question, and
    // a fixed top-N cutoff would never see it. Only papers that actually turn
    // out to hold something relevant get carried into the answer, so the
    // reading set is decided by content rather than by rank.
    const deep=scanSet.slice(0,ASK_DEEP);
    const rest=scanSet.slice(ASK_DEEP).filter(p=>p._full);
    let extra=[],foundBy={};
    if(rest.length){
      turn.state='scanning';turn.scanning=rest.length;
      renderAsk();
      const batches=[];
      for(let i=0;i<rest.length;i+=SCAN_BATCH)batches.push(rest.slice(i,i+SCAN_BATCH));
      const res=await Promise.all(batches.map(b=>
        fetch('/api/ask',{method:'POST',headers:{'content-type':'application/json'},
          body:JSON.stringify({mode:'scan',q:q,
            papers:b.map(p=>({i:p._row,title:p.title,text:p._full}))})})
        .then(r=>r.json()).then(j=>j.found||[]).catch(()=>[])));
      res.flat().forEach(f=>{if(f&&f.i!=null&&f.f)foundBy[f.i]=String(f.f);});
      extra=rest.filter(p=>foundBy[p._row]);
    }
    // citation order must match the context order exactly: full reads first,
    // then the screened papers that had something
    // Deep-read layer: for papers GROBID has parsed, pull the actual passages
    // and let BM25 pick which ones bear on the question. These are the only
    // sources that may support a specification-level claim, so they lead.
    let psg=[];
    try{
      await loadFtIndex();
      const cand=await loadPassages(scanSet);
      if(cand.length){
        turn.state='passages';turn.npsg=cand.length;
        renderAsk();
        psg=bm25(terms,cand).slice(0,FT_PASSAGES);
      }
    }catch(e){ psg=[]; }
    // abstracts still carry breadth, but yield room when real passages exist --
    // the synthesis context is finite and a passage outranks an abstract
    const nAbs=psg.length?6:ASK_DEEP;
    const picks=deep.slice(0,nAbs).concat(extra);
    // the displayed source list MUST mirror ctx order exactly, or [n] resolves
    // to the wrong paper -- passages lead there, so they lead here too
    turn.sources=psg.map(x=>Object.assign({},x.paper,
        {_sec:x.sec,_depth:'full'})).concat(picks);
    turn.extra=extra.length;
    turn.passages=psg;
    turn.state='thinking';
    renderAsk();
    // Passages lead the context so their citation numbers come first, and the
    // section label travels with them -- a specification claim has to be
    // attributable to "Section 4.2", not to the paper in general.
    const ctx=psg.map(x=>({title:x.paper.title+(x.sec?' — '+x.sec:''),
        authors:x.paper.authors,date:x.paper.date,source:x.paper.source,
        topic:x.paper.topic,summary:x.text,depth:'full'}))
      .concat(picks.map(p=>({title:p.title,authors:p.authors,
        date:p.date,source:p.source,topic:p.topic,
        summary:foundBy[p._row]||p._full||p.summary,
        // tell the agent HOW MUCH text is behind each source, so it can refuse a
        // specification question on an abstract instead of inventing one
        depth:p._full?'abstract':'summary_only'})));
    // The archive is a curated slice, not the literature. Look outside for what
    // it does not hold, and let the agent SEE the best few -- marked as not
    // held, so it can say "there is a 2024 paper you do not have that does
    // exactly this" instead of pretending the gap is not there.
    let outside=[];
    if(ASK_OUTSIDE){
      turn.state='outside';renderAsk();
      try{
        const or_=await fetch('/api/ask',{method:'POST',headers:{'content-type':'application/json'},
          body:JSON.stringify({mode:'outside',q:q})});
        const oj=await or_.json();
        if(or_.ok){
          const known=knownUids();
          outside=(oj.hits||[]).filter(h=>h.uid&&!known.has(String(h.uid).toLowerCase()))
                               .slice(0,OUTSIDE_SHOW);
        }
      }catch(e){ outside=[]; }
    }
    turn.outside=outside;
    const ctxAll=ctx.concat(outside.slice(0,OUTSIDE_CTX).map(h=>({
      title:h.title,authors:h.authors,date:h.year?String(h.year):'',
      source:h.venue||h.via,summary:h.abstract||'',depth:'abstract',external:true})));
    // the source list must mirror ctx order exactly or [n] resolves to the
    // wrong paper -- the outside hits are appended in both, in the same order
    turn.sources=(turn.sources||[]).concat(outside.slice(0,OUTSIDE_CTX).map(h=>({
      title:h.title,url:h.url,authors:h.authors,date:h.year?String(h.year):'',
      source:h.venue||'',uid:h.uid,_external:true})));
    turn.state='thinking';renderAsk();
    const ar=await fetch('/api/ask',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({mode:'answer',q:q,ctx:ctxAll,
        // prior turns, oldest first, so a follow-up resolves against the thread
        history:turns.slice(0,-1).filter(t=>t.state==='done')
                     .slice(-HIST_SEND).map(t=>({q:t.q,a:t.answer||''}))})});
    const aj=await ar.json();
    if(!ar.ok)throw new Error(aj.error||'answer failed');
    turn.answer=aj.answer;turn.state='done';
    // cache the FIRST turn only, for the same reason it is only read there
    if(!turns.slice(0,-1).length){
      ANS[key]={answer:aj.answer,uids:picks.map(p=>p.uid),t:Date.now()};
      persistAns();
    }
    saveChats();
  }catch(e){
    turn.error=String(e.message||e);turn.state='error';
  }
  asking=false;saveChats();renderAsk();
}
function askSubmit(){
  const el=$('askq');const q=el.value;el.value='';doAsk(q);
}
// The first question a conversation asks is what it is about, so it names it.
function titleChat(){
  const c=curChat();if(!c)return;
  const t=(c.turns||[])[0];
  if(t&&t.q&&(c.title==='New conversation'||!c.title))
    c.title=t.q.length>52?t.q.slice(0,52).replace(/\\s+\\S*$/,'')+'\u2026':t.q;
}
function switchChat(id){CHAT_ID=id;renderAsk();}
function deleteChat(id,ev){
  ev.stopPropagation();
  CHATS=CHATS.filter(c=>c.id!==id);
  if(!CHATS.length)newChat(true);
  if(!CHATS.some(c=>c.id===CHAT_ID))CHAT_ID=CHATS[0].id;
  saveChats();renderAsk();
}
// Adding a paper is a WRITE to the archive, so the browser does not do it: the
// Function dispatches the repo's ingest workflow, and the paper arrives through
// the same resolver, scorer and dedup as everything else.
async function addPaper(btn){
  const id=btn.dataset.add;
  btn.disabled=true;btn.textContent='adding\u2026';
  try{
    const r=await fetch('/api/ask',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({mode:'ingest',ids:[id]})});
    const j=await r.json();
    if(!r.ok)throw new Error(j.error||'ingest failed');
    QUEUED[id]=Date.now();persistQueued();
    btn.textContent='queued';btn.classList.add('done');
    toast('Queued \u2014 it enters the archive on the next ingest run.');
  }catch(e){
    btn.disabled=false;btn.textContent='+ Add';
    toast(String(e.message||e).slice(0,140));
  }
}
function outsideCard(h){
  const q=QUEUED[h.uid];
  const bits=[h.venue||h.via,h.year||'',h.cites?fmtK(h.cites)+' cites':''].filter(Boolean);
  return `<div class="osrc">
    <div class="osb"><a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.title)}</a>
      <div class="meta">${h.authors?esc(h.authors)+' \u00b7 ':''}${esc(bits.join(' \u00b7 '))}${h.oa?' \u00b7 <span class="oatag">open access</span>':''}</div>
      ${h.abstract?`<div class="oabs">${esc(h.abstract.slice(0,320))}${h.abstract.length>320?'\u2026':''}</div>`:''}
    </div>
    <button class="addbtn${q?' done':''}" data-add="${esc(h.uid)}" ${q?'disabled':''}
      title="Resolve it and add it to the archive">${q?'queued':'+ Add'}</button></div>`;
}
let _lastTurns=-1;
function renderAsk(notice){
  if(!VEC||!ARCHIVE_DATA){
    if(!notice){loadIndex(renderAsk);
      $('view').innerHTML='<div class="empty">Loading the semantic index\u2026</div>';return;}
  }
  if(!CHATS.length)newChat(true);
  const turns=curTurns();
  const note=notice?`<div class="askerr">${esc(notice)}</div>`:'';
  const chips=turns.length?'':'<div class="askex">'+ASK_EXAMPLES.map(e=>
    `<button class="exq" data-q="${esc(e)}">${esc(e)}</button>`).join('')+'</div>';
  const bar=`<div class="chatbar">
    <button class="newchat" id="newchat">+ New conversation</button>
    ${CHATS.slice(0,8).map(c=>`<button class="chatpill${c.id===CHAT_ID?' on':''}" data-chat="${esc(c.id)}">${esc(c.title||'Untitled')}<span class="x" data-del="${esc(c.id)}" title="Delete">\u2715</span></button>`).join('')}
  </div>`;
  const thread=turns.map((t,ti)=>{
    let body='';
    if(t.state==='retrieving')body='<div class="thinking">Searching '+(VEC_UIDS?VEC_UIDS.length.toLocaleString():'the')+' papers\u2026</div>';
    else if(t.state==='reading')body='<div class="thinking">Ranked '+(t.scanned||0)+' candidates \u00b7 pulling full abstracts for the top '+(t.sources||[]).length+'\u2026</div>';
    else if(t.state==='scanning')body='<div class="thinking">Screening '+(t.scanning||0)+' further papers for relevant content\u2026</div>';
    else if(t.state==='passages')body='<div class="thinking">Reading full text \u00b7 ranking '+(t.npsg||0)+' passages\u2026</div>';
    else if(t.state==='outside')body='<div class="thinking">Searching the outside literature (OpenAlex, arXiv)\u2026</div>';
    else if(t.state==='thinking')body='<div class="thinking">Synthesising from '+(t.sources||[]).length+' sources'+((t.passages||[]).length?' ('+t.passages.length+' full-text passages)':'')+(t.extra?' ('+t.extra+' surfaced by the wider screen)':'')+(t.reused?' \u00b7 '+t.reused+' from memory':'')+'\u2026</div>';
    else if(t.state==='error')body='<div class="askerr">'+esc(t.error)+'</div>';
    else body='<div class="answer">'+md(t.answer,ti)+'</div>';   // math typeset after insert
    const src=(t.sources&&t.state==='done')?'<div class="srch">Sources</div>'+t.sources.map((p,i)=>
      `<div class="src" id="src-${ti}-${i+1}"><span class="sn">${i+1}</span>
        <div><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a>
        <div class="meta">${p._depth==='full'?`<span class="ftag" title="full text \u2014 may support specification-level claims">full text${p._sec?' \u00b7 '+esc(p._sec):''}</span>`:''}${p._external?'<span class="xtag" title="found by live search \u2014 not held in the archive">not in archive</span>':''}<span class="j">${esc(jlabel(p))}</span>${p.authors?' \u00b7 '+esc(p.authors):''} \u00b7 ${esc(p.date||p.seen||'')}${p.score!=null?' \u00b7 rated '+p.score:''}</div></div></div>`
    ).join(''):'';
    const out=(t.state==='done'&&(t.outside||[]).length)
      ?`<div class="srch osh">Not in your archive <span class="n">\u00b7 ${t.outside.length} from a live search \u00b7 add anything worth keeping</span></div>`
        +t.outside.map(outsideCard).join('')
      :'';
    const badge=t.cached?`<span class="cachetag" title="answered from this browser's memory \u2014 nothing was re-read">from memory</span>
      <button class="reask" data-q="${esc(t.q)}" title="Ignore the stored answer and ask again">ask again</button>`:'';
    return `<div class="qa"><div class="qq">${esc(t.q)}${badge}</div>${body}${src}${out}</div>`;
  }).join('');
  const follow=turns.length?' \u00b7 follow-ups keep the thread':'';
  $('view').innerHTML=`<div class="dateline">Ask the archive <span class="n">\u00b7 ${VEC_UIDS?VEC_UIDS.length.toLocaleString():'\u2014'} papers indexed \u00b7 searched in your browser, ranked by similarity + your own paper scores + keyword fit; top ${ASK_DEEP} read in full and the next ${ASK_SCAN-ASK_DEEP} screened${follow}</span></div>
    ${bar}${note}${chips}${thread}
    <div class="askbox"><textarea id="askq" rows="2" placeholder="${turns.length?'Follow up \u2014 it remembers this conversation':'Ask anything about the archive \u2014 e.g. what does the evidence say about trend decay?'}"></textarea>
      <button id="asksend" ${asking?'disabled':''}>${asking?'\u2026':'Ask'}</button></div>
    <label class="outtog"><input type="checkbox" id="outtog" ${ASK_OUTSIDE?'checked':''}> Also search outside the archive (OpenAlex, arXiv)</label>`;
  const send=$('asksend');if(send)send.onclick=askSubmit;
  const ta=$('askq');
  if(ta){ta.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();askSubmit();}};ta.focus();}
  const tog=$('outtog');if(tog)tog.onchange=()=>{ASK_OUTSIDE=tog.checked;};
  const nc=$('newchat');if(nc)nc.onclick=()=>newChat();
  document.querySelectorAll('[data-chat]').forEach(b=>b.onclick=()=>switchChat(b.dataset.chat));
  document.querySelectorAll('[data-del]').forEach(b=>b.onclick=e=>deleteChat(b.dataset.del,e));
  document.querySelectorAll('.exq').forEach(b=>b.onclick=()=>doAsk(b.dataset.q));
  document.querySelectorAll('.reask').forEach(b=>b.onclick=()=>doAsk(b.dataset.q,true));
  document.querySelectorAll('.addbtn:not(.done)').forEach(b=>b.onclick=()=>addPaper(b));
  typesetMath($('view'));
  // a new turn now appears at the BOTTOM, so bring it into view rather than
  // leaving the reader looking at the top of a conversation they have read
  if(turns.length!==_lastTurns){
    _lastTurns=turns.length;
    const last=$('view').querySelector('.qa:last-of-type');
    if(last)last.scrollIntoView({block:'start',behavior:'smooth'});
  }
}
// NBER finance-program working papers, browsable by month (docs/nber.json,
// built by tools/backfill_nber.py back to 2010; kept fresh for the current
// month by the daily digest). A raw listing -- not scored -- like a direct
// window onto NBER's Asset Pricing / Corporate Finance / Monetary programs.
// a working paper is a "potential classic" if it accrued citations fast for
// its vintage (cites/yr) or a large absolute count -- the dominant signal for
// seminal work, from real OpenAlex citation data
const isClassic=x=>(x.cites_per_year||0)>=15||(x.cites||0)>=400;
function nberEntry(x){
  const sm=x.abstract?`<div class="summary">${esc(x.abstract)}</div>`:'';
  const cls=isClassic(x)?`<span class="wtag" title="high citation velocity for its vintage">★ potential classic</span>`:'';
  const cites=(x.cites!=null)?`<div class="cites">${fmtK(x.cites)}<small>cites</small></div>${x.cites_per_year!=null?`<div class="cap">${x.cites_per_year}/yr</div>`:''}`:`<div class="yr">${esc(x.wp||'')}</div>`;
  return `<div class="entry"><div class="rail">${cites}</div>
    <div class="body">
    <div class="cwrap"><a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>${cls}</div>
    <div class="meta">${x.authors?esc(x.authors):''} · ${esc(x.wp||'')}${_pdfBtn({url:x.url})}${_saveBtn({url:x.url,title:x.title,authors:x.authors})}</div>
    ${sm}</div></div>`;
}
let NBER_LOADED=false, nberLoading=false;
// nber.json is ~3MB (16y of papers); fetch it lazily the first time the NBER
// tab is opened, not on every page load -- same pattern as archive.json
function loadNBER(cb){
  if(NBER_LOADED){cb();return;}
  if(nberLoading)return;
  nberLoading=true;
  $('view').innerHTML='<div class="empty">Loading NBER working papers…</div>';
  fetch('nber.json').then(r=>r.json()).then(nb=>{
    NBER=nb||{};NBER_LOADED=true;nberLoading=false;
    $('nbermonth').innerHTML='';
    Object.keys(NBER).filter(k=>/^\\d{4}-\\d{2}$/.test(k)).sort().reverse()
      .forEach(m=>$('nbermonth').add(new Option(new Date(m+"-01").toLocaleString('en',{month:'long',year:'numeric'}),m)));
    if(VIEW==='nber')cb();
  }).catch(()=>{nberLoading=false;if(VIEW==='nber')$('view').innerHTML='<div class="empty">Could not load NBER data.</div>';});
}
function renderNBER(){
  if(!NBER_LOADED){loadNBER(renderNBER);return;}
  const keys=Object.keys(NBER).filter(k=>/^\\d{4}-\\d{2}$/.test(k)).sort().reverse();
  if(!keys.length){$('view').innerHTML='<div class="empty">No NBER data yet — run tools/backfill_nber.py.</div>';return;}
  const m=$('nbermonth').value||keys[0];
  const q=$('q').value.toLowerCase().trim();
  // sort by citations desc within the month so potential classics rise to top
  const rows=(NBER[m]||[]).filter(x=>!q||(x.title+' '+(x.authors||'')).toLowerCase().includes(q))
    .slice().sort((a,b)=>(b.cites||0)-(a.cites||0));
  const nClassic=rows.filter(isClassic).length;
  const label=new Date(m+"-01").toLocaleString('en',{month:'long',year:'numeric'});
  $('view').innerHTML=`<div class="dateline">NBER · Asset Pricing · ${esc(label)} <span class="n">· ${rows.length} working papers · by citations${nClassic?' · '+nClassic+' potential classic'+(nClassic>1?'s':''):''}</span></div>`+
    (rows.length?rows.map(nberEntry).join(''):'<div class="empty">No matches this month.</div>');
}
// Tuned for a SYSTEMATIC-MACRO / CTA desk (QRT macro): the identity themes are
// trend, carry, FX, rates/term premium, commodities, and macro regime/nowcasting
// -- weighted highest. Cross-asset methods the desk actually uses (regime-
// switching, state-space, nowcasting) stay strong. The equity cross-sectional
// factor zoo is kept only lightly (it's adjacent, not the job).
const FORYOU_KEYWORDS=[
  // --- Trend / CTA / managed futures (core identity) ---
  ['trend following',3],['time series momentum',3],['managed futures',3],
  [' cta ',2.5],['trend premi',2.5],['momentum',1.5],['ewma',1.5],
  ['volatility targeting',2.5],['target volatility',2.5],['risk parity',2.5],
  ['drawdown',1.5],['crisis alpha',3],['divergent',0.6],
  // --- Systematic / global macro, cross-asset ---
  ['systematic macro',3],['global macro',3],['cross asset',2.5],['multi asset',2.5],
  ['macro',1.2],['macroeconomic',1.5],['risk premia',2],['risk premium',1.5],
  ['value and momentum',2.5],['everywhere',0.8],
  // --- Carry (FX / rates / commodity) ---
  ['carry trade',3],['carry factor',2.5],['currency carry',3],['fx carry',3],
  // --- FX ---
  ['currency',2.5],['exchange rate',2],['dollar factor',2.5],
  ['uncovered interest',2.5],['purchasing power parity',1.5],['covered interest',1.5],
  // --- Rates / fixed income ---
  ['term premium',3],['bond risk premi',3],['yield curve',2.5],['term structure',2.5],
  ['treasury',1.5],['duration',1],['inflation',2.5],['real rate',2],['breakeven',2],
  // --- Commodities ---
  ['commodit',3],['backwardation',2.5],['contango',2.5],['roll yield',2.5],
  ['hedging pressure',2.5],['convenience yield',2],['basis momentum',2.5],
  ['oil price',1.5],['oil return',1.5],['gold',1],['futures',1],
  // --- Macro regime / cycle / policy / nowcasting ---
  ['regime',2.5],['regime switching',3],['markov switching',3],['business cycle',2.5],
  ['recession',1.5],['nowcast',3],['monetary policy',2.5],['central bank',2],
  ['fomc',2],['quantitative tightening',2],['fiscal',1.5],['positioning',2],
  ['commitment of traders',2.5],['cftc',2],
  // --- Methods the desk uses (Bayesian state-space, forecasting) ---
  ['bayesian',2],['kalman filter',2.5],['state space',2.5],['dynamic factor model',2.5],
  ['mixed frequency',2.5],['midas',2],['dynamic model averaging',2.5],
  ['forecast combination',2],['time series forecasting',1.5],['vector autoregression',1.5],
  ['gibbs sampling',1.5],['hedge fund replication',2],
  // --- Cross-asset correlation / diversification ---
  ['stock bond correlation',2.5],['bond equity correlation',2.5],
  ['equity bond correlation',2.5],['diversification',1],
  // --- Portfolio / execution (support, lower) ---
  ['portfolio construction',1.2],['transaction cost',1],['turnover',0.5],
  ['systematic trading',1.5],['systematic alpha',1.5],['tactical asset allocation',2],
  // --- Equity cross-section: adjacent only, deliberately light ---
  ['factor timing',1.2],['factor investing',1],['equity factor',0.6],
  ['cross section of',0.8],['asset pricing',0.6],['econometrics',0.5],
];
// hyphens normalized to spaces before matching so "factor-timing" and
// "factor timing" (or any other hyphen/space title variant) both hit
const _norm=s=>String(s||'').toLowerCase().replace(/-/g,' ');
function forYouScore(x){
  const text=' '+_norm((x.title||'')+' '+(x.summary||'')+' '+(x.topic||''))+' ';
  let s=0;
  FORYOU_KEYWORDS.forEach(([term,w])=>{if(text.includes(term))s+=w;});
  return s;
}
function renderForYou(){
  const q=$('q').value.toLowerCase().trim();
  const cs=sinceDays(21);
  const pool=(cs?DATA.filter(x=>(x.seen||'')>=cs):DATA)
    .filter(x=>!q||(x.title+' '+x.authors+' '+x.source).toLowerCase().includes(q));
  // keyword-fit nudged by author reputation (same bounded multiplier as
  // everywhere else), so a strong-author match ranks a touch higher
  const matched=pool.map(x=>({...x,_fy:forYouScore(x)})).filter(x=>x._fy>=2.5)
    .sort((a,b)=>b._fy*(b.reputation||1)-a._fy*(a.reputation||1)||byDate(a,b));
  // a raw keyword-sum ranking structurally favours long academic abstracts
  // over short practitioner posts (more text = more hits), so a flat top-10
  // would rarely surface a blog even when it's a genuinely strong match --
  // reserve slots across both buckets instead of letting them compete directly
  const prac=matched.filter(isPrac),acad=matched.filter(x=>!isPrac(x));
  let nPrac=Math.min(3,prac.length),nAcad=Math.min(10-nPrac,acad.length);
  nPrac=Math.min(10-nAcad,prac.length);
  const rows=[...acad.slice(0,nAcad),...prac.slice(0,nPrac)]
    // show personal-fit match strength here, not the generic (and often
    // unrelated) triage relevance score -- the two can legitimately
    // disagree, and showing the wrong one reads as a contradiction
    .map(x=>({...x,_displayScore:Math.min(100,Math.round(x._fy*10)),_displayLabel:'match'}));
  $('view').innerHTML=`<div class="dateline">For you <span class="n">· systematic macro / CTA — trend, carry, FX, rates &amp; term premium, commodities, macro regime &amp; nowcasting, Bayesian state-space · top ${rows.length}, across journals/preprints/practitioner</span></div>`+
    (rows.length?byCategory(rows,(a,b)=>(b._fy||0)-(a._fy||0)):'<div class="empty">Nothing matched strongly in the last 3 weeks.</div>');
}
const _psource=x=>String(x.source||'').replace(/^journal:/,'').replace(/^topic:/,'').trim()||'Other';
function pracEntry(x){
  const sm=x.summary?`<div class="summary">${esc(x.summary)}</div>`:'';
  return `<div class="entry"><div class="rail"></div><div class="body">
    <a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>
    <div class="meta">${x.authors?esc(x.authors)+' · ':''}${esc(x.date||x.seen)}${_ftBtn(x)}${_pdfBtn(x)}${_saveBtn(x)}</div>
    ${sm}</div></div>`;
}
function renderPractitioners(){
  const q=$('q').value.toLowerCase().trim();
  const src=$('psrc').value||'all';
  const rows=DATA.filter(isPrac)
    .filter(x=>src==='all'||_psource(x)===src)
    .filter(x=>!q||(x.title+' '+x.authors+' '+x.source+' '+(x.summary||'')).toLowerCase().includes(q))
    .slice().sort(byDate);
  const groups={}; rows.forEach(x=>{const s=_psource(x);(groups[s]=groups[s]||[]).push(x);});
  let h=`<div class="dateline">Practitioner &amp; house research <span class="n">· ${rows.length} posts · by source · latest first</span></div>`;
  if(!rows.length){$('view').innerHTML=h+'<div class="empty">No matches.</div>';return;}
  Object.keys(groups).sort().forEach(s=>{const a=groups[s];
    h+=`<div class="sechead t2">${esc(s)}<span class="cnt">${a.length}</span></div>`+a.map(pracEntry).join('');});
  $('view').innerHTML=h;
}
// archive.json carries the FULL history (data.json is a bounded recent
// window -- see portal.build) and only grows; fetch it once, lazily, the
// first time someone actually opens Archive, not on every page load.
function loadArchive(cb){
  if(ARCHIVE_DATA){cb();return;}
  if(archiveLoading)return;
  archiveLoading=true;
  $('view').innerHTML='<div class="empty">Loading the full archive…</div>';
  fetch('archive.json').then(r=>r.json()).then(d=>{
    ARCHIVE_DATA=d;
    PINS=loadPins();rebuildViews();renderPinTabs();renderTagbar();
  loadChats();
  $('tagbar').hidden=!(SLEEVE_VIEWS.indexOf(VIEW)>=0||VIEW.slice(0,3)==='sl:');
  d.forEach(x=>{if(x.url)ITEM_BY_URL[x.url]=x;});
    archiveLoading=false;
    if(VIEW==='archive')cb();
  }).catch(()=>{
    archiveLoading=false;
    if(VIEW==='archive')$('view').innerHTML='<div class="empty">Could not load the archive.</div>';
  });
}
let archivePage=0;
const ARCHIVE_PAGE_SIZE=100;
// A pinned sleeve is the archive narrowed to one sleeve -- same records, same
// sort. It exists as its own tab so it survives a reload and a tab switch,
// which a transient filter does not.
function renderSleeve(key){
  if(!ARCHIVE_DATA){loadArchive(()=>renderSleeve(key));return;}
  const q=$('q').value.toLowerCase().trim();
  const rows=sleeveFilter(ARCHIVE_DATA,key).filter(x=>!isPrac(x))
    .filter(x=>!q||(x.title+' '+x.authors+' '+x.source).toLowerCase().includes(q))
    .slice().sort(byDate);
  const shownCount=Math.min(rows.length,(archivePage+1)*ARCHIVE_PAGE_SIZE);
  const remaining=rows.length-shownCount;
  const more=remaining>0?`<button class="loadmore" id="slmore">Show ${Math.min(ARCHIVE_PAGE_SIZE,remaining)} more <span class="n">(${remaining} left)</span></button>`:'';
  const nfit=rows.filter(x=>(x.desk_fit||0)>=2).length;
  $('view').innerHTML=`<div class="dateline">${esc(SLEEVE_LABEL[key]||key)} <span class="n">· ${rows.length} paper${rows.length===1?'':'s'} · ${nfit} at desk fit 2+ · date-wise</span></div>`+
    (rows.length?rows.slice(0,shownCount).map(x=>entry(x)).join('')
      :'<div class="empty">Nothing labelled with this sleeve yet — the backfill is still running.</div>')+more;
  if(remaining>0)$('slmore').onclick=()=>{archivePage++;renderSleeve(key);};
}
function renderArchive(){
  if(!ARCHIVE_DATA){loadArchive(renderArchive);return;}
  const q=$('q').value.toLowerCase().trim();
  const t=$('topic').value||'all';
  let rows=sleeveFilter(ARCHIVE_DATA).filter(x=>!isPrac(x)&&(t==='all'||((x.topic||'Other')===t)))
    .filter(x=>!q||(x.title+' '+x.authors+' '+x.source+' '+(x.topic||'')).toLowerCase().includes(q))
    .slice().sort(byDate);
  const label=t==='all'?'All topics':t;
  // paginated -- rendering thousands of animated cards at once is what
  // actually made Archive itself feel slow, independent of fetch time
  const shownCount=Math.min(rows.length,(archivePage+1)*ARCHIVE_PAGE_SIZE);
  const shown=rows.slice(0,shownCount);
  const remaining=rows.length-shownCount;
  const more=remaining>0?`<button class="loadmore" id="archmore">Show ${Math.min(ARCHIVE_PAGE_SIZE,remaining)} more <span class="n">(${remaining} left)</span></button>`:'';
  $('view').innerHTML=`<div class="dateline">Archive · ${esc(label)} <span class="n">· ${rows.length} papers · date-wise</span></div>`+
    (rows.length?shown.map(x=>entry(x)).join(''):'<div class="empty">No matches.</div>')+more;
  if(remaining>0)$('archmore').onclick=()=>{archivePage++;renderArchive();};
}
function _subBar(label,valueHtml,pct){
  const w=Math.max(0,Math.min(100,pct||0));
  return `<span class="sub"><i>${label}</i><b>${valueHtml}</b><s><u style="width:${w}%"></u></s></span>`;
}
function monthlyEntry(x,rank){
  const band=bandColor(x.composite);
  const lvl=v=>v==null?'–':`${v}/3`;
  const prov=x.contribution_provisional?' <span style="opacity:.55">(prov.)</span>':'';
  const subs=[
    _subBar('Generality',lvl(x.generality),(x.generality||0)/3*100),
    _subBar('Contribution'+(x.novelty_type&&x.novelty_type!=='none'?' ('+esc(x.novelty_type)+')':''),lvl(x.contribution)+prov,(x.contribution||0)/3*100),
    _subBar('Novelty vs history',x.novelty_posterior!=null?Math.round(x.novelty_posterior*100)+'%':'–',(x.novelty_posterior||0)*100),
    _subBar('Testability',lvl(x.testability),(x.testability||0)/3*100),
    _subBar('Citations',x.cites_norm!=null?Math.round(x.cites_norm):'–',x.cites_norm||0),
    _subBar('Velocity',x.velocity_norm!=null?Math.round(x.velocity_norm):'–',x.velocity_norm||0),
    _subBar('Robustness',x.robustness!=null?Math.round(x.robustness*100)+'%':'–',(x.robustness||0)*100),
    _subBar('Reputation',x.reputation!=null?x.reputation.toFixed(2)+'×':'–',
      x.reputation!=null?(x.reputation-0.85)/0.30*100:0),
    _subBar('Author',x.author_score!=null?Math.round(x.author_score):'–',x.author_score||0),
  ].join('');
  const meta=`<span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''}${x.date?' · '+esc(x.date):''}${x.cites!=null?' · '+fmtK(x.cites)+' cites':''}${x.consensus_n?' · '+x.consensus_n+'× '+(x.consensus_agree?'agree':'split'):''}${_ftBtn(x)}${_pdfBtn(x)}${_saveBtn(x)}`;
  const cval=Math.round(x.composite);
  return `<div class="entry"><div class="rail">
      <div class="rank">${rank}</div><div class="gauge" style="--pct:${cval};--gc:${band}"><span>${cval}</span></div><div class="cap">composite</div></div>
    <div class="body">
      <a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>
      <div class="meta">${meta}</div>
      ${x.summary?`<div class="summary">${esc(x.summary)}</div>`:''}
      <div class="subs">${subs}</div>
    </div></div>`;
}
function renderMonthly(){
  const keys=Object.keys(MONTHLY).filter(k=>/^\\d{4}-\\d{2}$/.test(k)).sort().reverse();
  if(!keys.length){$('view').innerHTML='<div class="empty">No monthly picks yet — the next run fills this.</div>';return;}
  const m=$('month').value||keys[0];
  const q=$('q').value.toLowerCase().trim();
  const rows=(MONTHLY[m]||[]).filter(x=>!q||(x.title+' '+x.authors+' '+(x.journal||'')).toLowerCase().includes(q));
  const label=new Date(m+"-01").toLocaleString('en',{month:'long',year:'numeric'});
  $('view').innerHTML=`<div class="dateline">${esc(label)} <span class="n">· top ${rows.length} · 5-factor composite</span></div>`+
    (rows.length?rows.map((x,i)=>monthlyEntry(x,i+1)).join(''):'<div class="empty">No matches.</div>');
}
function classicsGroups(){
  if(Array.isArray(CLASSICS))return{overall:CLASSICS,journals:{},topics:{},modern:[]};
  return{overall:(CLASSICS&&CLASSICS.overall)||[],journals:(CLASSICS&&CLASSICS.journals)||{},
    topics:(CLASSICS&&CLASSICS.topics)||{},modern:(CLASSICS&&CLASSICS.modern)||[]};
}
function renderModern(g){
  const q=$('q').value.toLowerCase().trim();
  const rows=(g.modern||[]).filter(x=>!q||(x.title+' '+x.authors+' '+(x.journal||'')).toLowerCase().includes(q))
    .slice().sort((a,b)=>(b.composite||0)-(a.composite||0));
  $('view').innerHTML=`<div class="dateline">Modern · emerging — LLM-flagged <span class="n">· ${rows.length} papers</span></div>`+
    (rows.length?rows.map(canonEntry).join(''):'<div class="empty">Nothing flagged yet — high-innovation papers land here as runs proceed.</div>');
}
function canonEntry(x){
  const tag=x.type?`<span class="tag ${esc(String(x.type).toLowerCase())}">${esc(x.type)}</span>`:'';
  const cites=x.cites!=null?` · ${fmtK(x.cites)} cites`:'';
  const why=x.why||x.summary||'';
  return `<div class="entry"><div class="rail"><div class="yr">${esc(x.year||'')}</div></div>
    <div class="body">
      <div class="cwrap"><a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>${tag}</div>
      ${why?`<div class="summary">${esc(why)}</div>`:''}
      <div class="meta"><span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''}${cites}${_ftBtn(x)}${_pdfBtn(x)}${_saveBtn(x)}</div>
    </div></div>`;
}
function renderCanon(g,topic){
  const q=$('q').value.toLowerCase().trim();
  const rows=((g.topics&&g.topics[topic])||[])
    .filter(x=>!q||(x.title+' '+x.authors+' '+(x.journal||'')+' '+(x.why||'')).toLowerCase().includes(q))
    .slice().sort((a,b)=>(a.year||0)-(b.year||0));
  $('view').innerHTML=`<div class="dateline">${esc(topic)} <span class="n">· seminal · chronological · ${rows.length} papers · cites shown for context, not ranking</span></div>`+
    (rows.length?rows.map(canonEntry).join(''):'<div class="empty">No papers.</div>');
}
function renderClassics(){
  const g=classicsGroups(),sel=$('jsel').value||'__overall';
  if(sel.slice(0,6)==='topic:'){renderCanon(g,sel.slice(6));return;}
  if(sel==='__modern'){renderModern(g);return;}
  const src=sel==='__overall'?g.overall:(g.journals[sel]||[]);
  const q=$('q').value.toLowerCase().trim();
  let rows=src.filter(x=>!q||(x.title+' '+x.authors+' '+(x.journal||'')).toLowerCase().includes(q))
    .slice().sort((a,b)=>(b.cites||0)-(a.cites||0));
  const max=Math.max(1,...rows.map(x=>x.cites||0));
  const head=sel==='__overall'?'Most-cited · finance · all-time':esc(sel);
  $('view').innerHTML=`<div class="dateline">${head} <span class="n">· ${rows.length} papers · by citations</span></div>`+
    (rows.length?rows.map(x=>`<div class="entry classic"><div class="body">
      <div class="cwrap"><a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a><span class="cites">${fmtK(x.cites||0)}<small>cites</small></span></div>
      <div class="bar"><i style="width:${((x.cites||0)/max*100).toFixed(1)}%"></i></div>
      <div class="meta"><span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''} · ${esc(x.year||'')}${_ftBtn(x)}${_pdfBtn(x)}${_saveBtn(x)}</div>
      ${x.summary?`<div class="summary">${esc(x.summary)}</div>`:''}</div></div>`).join('')
      :'<div class="empty">No history generated yet — run backfill.py.</div>');
}
function render(){if(VIEW.slice(0,3)==='sl:'){renderSleeve(VIEW.slice(3));return;}
  VIEW==="monthly"?renderMonthly():VIEW==="ask"?renderAsk():VIEW==="foryou"?renderForYou():VIEW==="watched"?renderWatched():VIEW==="anchors"?renderAnchors():VIEW==="nber"?renderNBER():VIEW==="recent"?renderRecent():VIEW==="practitioners"?renderPractitioners():VIEW==="archive"?renderArchive():VIEW==="saved"?renderSaved():renderClassics();}
// Eleven flat tabs gave every destination the same weight and scrolled half of
// them off screen. They group by INTENT: read what's new, question the corpus,
// consult the standing reference, revisit your own picks. Ask and Saved are
// groups of one -- they are modes, not lists, so they get no sub-row.
const GROUPS={
  papers:['recent','foryou','watched','nber','monthly','practitioners','archive'],
  ask:['ask'],
  shelf:['classics','anchors'],
  saved:['saved'],
};
const GROUP_OF={};
Object.entries(GROUPS).forEach(([g,vs])=>vs.forEach(v=>{GROUP_OF[v]=g;}));
const ALL_VIEWS=Object.values(GROUPS).flat();

function setGroup(g){
  Object.keys(GROUPS).forEach(k=>$('g-'+k).classList.toggle('on',k===g));
  // a one-view group has nothing to choose between, so hide the sub-row
  const multi=GROUPS[g].length>1;
  $('subtabs').style.display=multi?'':'none';
  ALL_VIEWS.forEach(v=>{
    const b=$('t-'+v);
    if(b)b.hidden=!(multi&&GROUPS[g].indexOf(v)>=0);
  });
  if(GROUPS[g].indexOf(VIEW)<0)setView(GROUPS[g][0]);
}

// Only the views whose records actually carry the labels. nber.json and
// monthly.json are written by separate exporters that don't include sleeves,
// so offering the facet there would be a control that silently does nothing.
const SLEEVE_VIEWS=['recent','foryou','watched','archive','saved'];
function setView(v){
  VIEW=v;ALL_VIEWS.forEach(k=>$('t-'+k).classList.toggle('on',k===v));
  const g=GROUP_OF[v];
  Object.keys(GROUPS).forEach(k=>$('g-'+k).classList.toggle('on',k===g));
  const multi=GROUPS[g].length>1;
  $('subtabs').style.display=multi?'':'none';
  ALL_VIEWS.forEach(k=>{
    const b=$('t-'+k);
    if(b)b.hidden=!(multi&&GROUPS[g].indexOf(k)>=0);
  });
  $('month').style.display=v==="monthly"?'':'none';
  $('nbermonth').style.display=v==="nber"?'':'none';
  $('jsel').style.display=v==="classics"?'':'none';
  $('cat').style.display=v==="recent"?'':'none';
  $('topic').style.display=v==="archive"?'':'none';
  // unlike the other facets this one is not view-specific: sleeves are a
  // property of the paper, so it applies anywhere papers are listed. On a
  // pinned tab the sleeve is already decided, so the rail only shows which.
  $('tagbar').hidden=!(SLEEVE_VIEWS.indexOf(v)>=0||v.slice(0,3)==='sl:');
  if(!$('tagbar').hidden)renderTagbar();
  $('psrc').style.display=v==="practitioners"?'':'none';
  if(v==="archive"||v.slice(0,3)==='sl:')archivePage=0;
  render();
}
Object.keys(GROUPS).forEach(g=>{$('g-'+g).onclick=()=>setGroup(g);});
$('t-recent').onclick=()=>setView('recent');
$('t-ask').onclick=()=>setView('ask');
$('t-foryou').onclick=()=>setView('foryou');
$('t-watched').onclick=()=>setView('watched');
$('t-nber').onclick=()=>setView('nber');
$('t-monthly').onclick=()=>setView('monthly');
$('t-classics').onclick=()=>setView('classics');
$('t-anchors').onclick=()=>setView('anchors');
$('t-practitioners').onclick=()=>setView('practitioners');
$('t-archive').onclick=()=>setView('archive');
$('t-saved').onclick=()=>setView('saved');
$('q').addEventListener('input',()=>{archivePage=0;render();});
$('month').addEventListener('change',render);
$('nbermonth').addEventListener('change',render);
$('cat').addEventListener('change',render);
$('topic').addEventListener('change',()=>{archivePage=0;render();});
$('psrc').addEventListener('change',render);
$('jsel').addEventListener('change',render);
const root=document.documentElement;
$('toggle').onclick=()=>{
  const dark=!(root.getAttribute('data-theme')==='dark'||
    (!root.getAttribute('data-theme')&&matchMedia('(prefers-color-scheme:dark)').matches));
  root.setAttribute('data-theme',dark?'dark':'light');$('toggle').textContent=dark?'Light':'Dark';
};
const header=document.querySelector('header');
addEventListener('scroll',()=>header.classList.toggle('scrolled',scrollY>4),{passive:true});
syncSavedFromServer();
Promise.all([
  fetch('data.json').then(r=>r.json()).catch(()=>[]),
  fetch('classics.json').then(r=>r.json()).catch(()=>[]),
  fetch('monthly.json').then(r=>r.json()).catch(()=>({})),
  loadFtIndex(),
]).then(([d,c,mo])=>{
  DATA=d;CLASSICS=c;MONTHLY=mo||{};
  MAXSEEN=d.reduce((m,x)=>(x.seen||'')>m?(x.seen||''):m,"");
  if(MAXSEEN){
    const days=Math.round((Date.now()-new Date(MAXSEEN))/864e5);
    const ago=days<=0?'today':days===1?'yesterday':days+' days ago';
    const dot=days<=2?'var(--strong)':days<=5?'var(--medium)':'var(--low)';
    $('freshness').innerHTML=`<span style="color:${dot}">●</span> Data as of ${esc(MAXSEEN)} (${ago})`;
  }
  Object.keys(MONTHLY).filter(k=>/^\\d{4}-\\d{2}$/.test(k)).sort().reverse()
    .forEach(m=>$('month').add(new Option(new Date(m+"-01").toLocaleString('en',{month:'long',year:'numeric'}),m)));
  $('topic').add(new Option('All topics','all'));
  TOPICS.forEach(t=>$('topic').add(new Option(t,t)));
  $('psrc').add(new Option('All sources','all'));
  [...new Set(d.filter(isPrac).map(_psource))].sort()
    .forEach(s=>$('psrc').add(new Option(s,s)));
  const g=classicsGroups();
  if(Object.keys(g.topics).length){
    const og=document.createElement('optgroup');og.label='Seminal — by topic';
    Object.keys(g.topics).forEach(t=>og.appendChild(new Option(t,'topic:'+t)));
    $('jsel').appendChild(og);
  }
  const og2=document.createElement('optgroup');og2.label='Most cited — by journal';
  og2.appendChild(new Option('Overall (all finance)','__overall'));
  Object.keys(g.journals).forEach(name=>og2.appendChild(new Option(name,name)));
  $('jsel').appendChild(og2);
  if((g.modern||[]).length){
    const og3=document.createElement('optgroup');og3.label='Emerging';
    og3.appendChild(new Option('Modern — flagged','__modern'));
    $('jsel').appendChild(og3);
  }
  // registry so a star click on any tab can find the full record by URL,
  // no matter which of the three JSON files it actually came from
  d.forEach(x=>{if(x.url)ITEM_BY_URL[x.url]=x;});
  Object.values(MONTHLY).forEach(arr=>(arr||[]).forEach(x=>{if(x.url)ITEM_BY_URL[x.url]=x;}));
  (g.overall||[]).forEach(x=>{if(x.url)ITEM_BY_URL[x.url]=x;});
  Object.values(g.journals||{}).forEach(arr=>(arr||[]).forEach(x=>{if(x.url)ITEM_BY_URL[x.url]=x;}));
  Object.values(g.topics||{}).forEach(arr=>(arr||[]).forEach(x=>{if(x.url)ITEM_BY_URL[x.url]=x;}));
  (g.modern||[]).forEach(x=>{if(x.url)ITEM_BY_URL[x.url]=x;});
  render();
});
</script>
</body>
</html>
"""
