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
        # retired by tools/prune.py: the row stays in state.db so cross-run
        # dedup still remembers the paper, but it leaves everything the reader
        # sees -- and the embedding index the browser downloads
        if m.get("retired"):
            continue
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
            # mailed in by the Claude digest, and the host would neither confirm
            # nor deny the link (Macrosynergy answers 403 to everything). Shown
            # with a marker rather than presented as established -- every other
            # record here came from an API or a feed
            "unverified": bool(m.get("unverified")),
            "topic": m.get("topic", ""),
            # Subject tags (tools/tags.py) -- a finer layer beneath sleeves.
            # Capped on export, not in the matcher: a paper may legitimately
            # carry a dozen, and the cap is about how many fit on a card.
            "tags": (m.get("tags") or [])[:config.TAGS_MAX],
            # A free PDF a resolver already found -- OpenAlex's
            # best_oa_location, mostly. _pdfUrl derives arXiv and NBER from
            # the identifier, but everything else is only knowable because
            # something went looking, and it was not being shipped at all.
            "pdf_url": m.get("pdf_url") or "",
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
    if not (docs / "cot.json").exists():            # placeholder until tools/cot.py runs
        (docs / "cot.json").write_text('{"groups":[]}', encoding="utf-8")

    # Typed artifacts (tools/artifacts.py) ship SEPARATELY, keyed by uid, and
    # are fetched only when the Build tab is opened. Inlining them into
    # data.json would put a kilobyte or two of methods, datasets and pitfalls
    # onto every page load for a tab most visits never open.
    arts = {}
    for uid, meta in con.execute("SELECT uid, meta FROM items"):
        try:
            m = json.loads(meta or "{}")
        except Exception:                          # noqa: BLE001
            continue
        if m.get("retired") or not m.get("artifacts"):
            continue
        arts[uid] = m["artifacts"]
    (docs / "artifacts.json").write_text(
        json.dumps(arts, separators=(",", ":"), default=str), encoding="utf-8")
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
  --rail:#EFF2F0; --navfg:#33403A; --nil:#C9D0CB; --bar:#D8DEDA; --long:#1F7A3D; --short:#A63A2B; --rowhover:#FBFCFB;
  --serif:Newsreader,ui-serif,Georgia,'Iowan Old Style',Palatino,serif;
  --sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#0F1311; --panel:#141917; --ink:#E9ECE8; --muted:#94A099; --faint:#6E7A73;
  --line:#232A26; --line2:#1C221F; --accent:#43B08F; --strong:#3FAE63; --medium:#C79A3A;
  --low:#7D857D; --cite:#C79A3A;
  --rail:#111614; --navfg:#C4CCC6; --nil:#3A443E; --bar:#2A322D; --long:#3FAE63; --short:#D4705F; --rowhover:#181E1B;}}
:root[data-theme="dark"]{
  --ground:#0F1311; --panel:#141917; --ink:#E9ECE8; --muted:#94A099; --faint:#6E7A73;
  --line:#232A26; --line2:#1C221F; --accent:#43B08F; --strong:#3FAE63; --medium:#C79A3A;
  --low:#7D857D; --cite:#C79A3A;
  --rail:#111614; --navfg:#C4CCC6; --nil:#3A443E; --bar:#2A322D; --long:#3FAE63; --short:#D4705F; --rowhover:#181E1B;}
:root[data-theme="light"]{
  --ground:#F6F8F7; --panel:#FFFFFF; --ink:#171C1A; --muted:#5E6B66; --faint:#8A968F;
  --line:#E4E8E5; --line2:#EDF0EE; --accent:#0C5C4A; --strong:#1F7A3D; --medium:#9A6B00;
  --low:#8A8F87; --cite:#9A6B00;
  --rail:#EFF2F0; --navfg:#33403A; --nil:#C9D0CB; --bar:#D8DEDA; --long:#1F7A3D; --short:#A63A2B; --rowhover:#FBFCFB;}

*{box-sizing:border-box;}
::selection{background:var(--accent);color:var(--panel);}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--serif);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;}
a{color:inherit;text-decoration:none;}
#app{display:grid;grid-template-columns:244px minmax(0,1.25fr) minmax(0,1fr);
  grid-template-rows:minmax(0,1fr);height:100vh;overflow:hidden;}
#app.wide{grid-template-columns:244px minmax(0,1fr);}
#app.wide #detail{display:none;}
/* The detail rail is hidden by VIEW (map/ask/pmap set .wide), never by width,
   so on a 1280px laptop three columns split into a reading column barely wider
   than the sidebar. The rail is a companion to the content, not the content,
   so below this it is the one that goes. */
@media (max-width:1200px){
  #app{grid-template-columns:244px minmax(0,1fr);}
  #detail{display:none;}
}
#rail{background:var(--rail);border-right:1px solid var(--line);display:flex;flex-direction:column;
  overflow-y:auto;overflow-x:hidden;min-width:0;min-height:0;}
#listcol{display:flex;flex-direction:column;border-right:1px solid var(--line);background:var(--ground);
  overflow-y:auto;overflow-x:hidden;min-width:0;min-height:0;padding-bottom:40px;}
#detail{background:var(--panel);overflow-y:auto;overflow-x:hidden;min-width:0;min-height:0;}
header{position:sticky;top:0;z-index:9;background:var(--ground);border-bottom:1px solid transparent;
  transition:box-shadow .25s ease,border-color .25s ease;}
header.scrolled{border-color:var(--line);box-shadow:0 6px 18px -12px rgba(0,0,0,.25);}
.mast{max-width:none;margin:0;padding:20px 16px 0;}
.brandrow{display:flex;align-items:baseline;justify-content:space-between;gap:14px;}
.brand{font-weight:600;font-size:19px;letter-spacing:-.012em;line-height:1.16;}
.brand .the{font-style:italic;font-weight:400;color:var(--muted);}
.tagline{font-family:var(--sans);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--faint);margin-top:6px;}
.toggle{font-family:var(--sans);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);background:none;border:1px solid var(--line);border-radius:20px;
  padding:5px 11px;cursor:pointer;transition:border-color .15s,color .15s,transform .15s;}
.toggle:hover{border-color:var(--accent);color:var(--accent);}
.toggle:active{transform:scale(.94);}
.doublerule{height:3px;border-top:1px solid var(--ink);border-bottom:1px solid var(--ink);margin:14px 0 0;}
.navtabs{display:flex;gap:5px;padding:14px 0 6px;flex-wrap:wrap;overflow-x:visible;
  scrollbar-width:none;-ms-overflow-style:none;-webkit-mask:linear-gradient(90deg,#000 94%,transparent);}
.navtabs::-webkit-scrollbar{display:none;}
.navtabs button{font-family:var(--sans);font-size:11px;font-weight:700;letter-spacing:.02em;
  color:var(--muted);background:var(--panel);border:1px solid var(--line);border-radius:20px;
  padding:5px 10px;cursor:pointer;white-space:nowrap;flex:none;
  transition:background .15s ease,border-color .15s ease,color .15s ease,transform .1s ease;}
.navtabs button:hover:not(.on){border-color:var(--accent);color:var(--accent);}
.navtabs button:active{transform:scale(.96);}
.navtabs button.on{color:var(--panel);background:var(--ink);border-color:var(--ink);}
.navtools{display:flex;flex-direction:column;align-items:stretch;gap:7px;padding:6px 0 14px;}
.navtools .sp{display:none;}
#q{width:100%!important;max-width:none!important;}
.searchwrap{display:flex;}
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
.dateline{font-family:var(--sans);font-size:11px;letter-spacing:.11em;text-transform:uppercase;
  color:var(--muted);margin:0;padding:18px 18px 13px;border-bottom:1px solid var(--line);
  display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;line-height:1.5;}
.dateline .n{font-variant-numeric:tabular-nums;color:var(--faint);}
/* a card with no score has no rail to show, so it spans the full width */
.entry.flat{grid-template-columns:1fr;}
.ntag{display:inline-block;font-family:var(--sans);font-size:10px;font-weight:600;
  letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  background:var(--line2);border-radius:3px;padding:2px 7px;margin-bottom:7px;}
.ntag.wait{color:var(--faint);background:transparent;border:1px dashed var(--line);}

/* ---- two-level navigation ---- */
.subtabs{display:flex;flex-direction:column;align-items:stretch;gap:1px;padding:4px 0 8px;}
.subtabs button{font-family:var(--sans);font-size:12px;font-weight:500;
  letter-spacing:.01em;color:var(--muted);background:transparent;
  border:0;border-left:2px solid transparent;border-radius:0 5px 5px 0;padding:7px 9px;text-align:left;font-size:13px;
  cursor:pointer;transition:color .15s,background .15s;}
.subtabs button:hover:not(.on){color:var(--accent);background:var(--line2);}
.subtabs button.on{color:var(--accent);background:var(--panel);
  border-left-color:var(--accent);font-weight:600;}
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
.askbox{flex-direction:column;align-items:stretch;}
  .askbox button{width:100%;padding:11px;}
  .src{grid-template-columns:22px 1fr;}
}

/* ---- Ask tab ---- */
/* These lived INSIDE @media (max-width:640px), so every one of them --
   the conversation pills, + New conversation, the outside-result cards,
   + Add, the not-in-archive tag -- was unstyled on desktop, which is
   where this is actually read. Only the genuinely responsive rules stay
   behind the breakpoint. */
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

.mapbtn{font-family:var(--sans);font-size:10.5px;font-weight:700;letter-spacing:.03em;
  margin-left:8px;padding:1px 7px;border-radius:5px;cursor:pointer;
  color:var(--accent);background:transparent;border:1px solid var(--line);}
.mapbtn:hover{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 10%,transparent);}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px;}
.tg{font-family:var(--sans);font-size:10.5px;font-weight:500;letter-spacing:.01em;
  color:var(--muted);background:transparent;border:1px solid var(--line);
  border-radius:11px;padding:1.5px 8px;cursor:pointer;
  transition:color .12s,border-color .12s,background .12s;}
.tg:hover{color:var(--accent);border-color:var(--accent);}
.tg.on{color:var(--panel);background:var(--ink);border-color:var(--ink);}
.tagnote{font-family:var(--sans);font-size:10.5px;font-weight:600;letter-spacing:.02em;
  text-transform:none;color:var(--panel);background:var(--ink);border-radius:11px;
  padding:2px 9px;cursor:pointer;}
.tagnote:hover{opacity:.82;}
.mapbtn.off{opacity:.42;cursor:help;}
.navtoggle{font-family:var(--sans);font-size:11.5px;font-weight:600;letter-spacing:.02em;
  color:var(--muted);background:var(--panel);border:1px solid var(--line);
  border-radius:16px;padding:5px 12px;cursor:pointer;transition:all .13s;}
.navtoggle:hover{border-color:var(--accent);color:var(--accent);}
.navtoggle.on{color:var(--panel);background:var(--ink);border-color:var(--ink);}
.mapbtn.addpdf{opacity:.72;border-style:dashed;}
.mapbtn.addpdf:hover{opacity:1;border-style:solid;border-color:var(--accent);
  background:color-mix(in srgb,var(--accent) 10%,transparent);}
.mapbtn.off:hover{border-color:var(--line);background:none;}
.pmapbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;
  justify-content:space-between;margin:8px 2px 2px;}
/* the list and the canvas are one control surface: a row highlights its node
   and a node highlights its row, so neither has to be read alone */
.pmaprow{display:flex;gap:9px;align-items:flex-start;border-radius:12px;
  transition:background .12s;}
.pmaprow.on{background:color-mix(in srgb,var(--accent) 7%,transparent);}
.pmaprowbody{flex:1;min-width:0;position:relative;}
.expandbtn{flex:0 0 auto;margin-top:22px;width:24px;height:24px;border-radius:7px;
  font-family:var(--sans);font-size:13px;font-weight:700;line-height:1;
  color:var(--panel);background:var(--accent);border:0;cursor:pointer;}
.expandbtn:hover{opacity:.85;}
.expandbtn.done,.expandbtn:disabled{background:var(--line);color:var(--muted);cursor:default;}
.citetag,.simtag{position:absolute;top:15px;right:16px;z-index:1;
  font-family:var(--sans);font-size:9.5px;font-weight:700;letter-spacing:.05em;
  text-transform:uppercase;padding:1px 6px;border-radius:4px;}
.citetag{color:var(--panel);background:var(--accent);}
.simtag{color:var(--muted);background:color-mix(in srgb,var(--muted) 12%,transparent);
  font-variant-numeric:tabular-nums;}
#pmapwrap{position:relative;margin:12px 0 6px;border:1px solid var(--line);
  border-radius:14px;background:var(--panel);overflow:hidden;}
#pmapcv{display:block;width:100%;height:min(66vh,600px);cursor:pointer;}
.pmapnote{font-family:var(--sans);font-size:11.5px;color:var(--muted);margin:8px 2px 0;}
#mapwrap{position:relative;margin:14px 0 6px;border:1px solid var(--line);
  border-radius:14px;background:var(--panel);overflow:hidden;}
#mapcv{display:block;width:100%;height:min(68vh,640px);cursor:crosshair;}
#maptip{position:absolute;pointer-events:none;max-width:340px;padding:8px 11px;
  border-radius:9px;background:var(--ink);color:var(--panel);font-family:var(--sans);
  font-size:12px;line-height:1.4;opacity:0;transition:opacity .12s;z-index:5;}
#maptip.on{opacity:1;}
.maplegend{display:flex;flex-wrap:wrap;gap:5px;margin:9px 0 0;}
.mapkey{display:inline-flex;align-items:center;gap:6px;font-family:var(--sans);
  font-size:11px;font-weight:600;color:var(--muted);background:var(--panel);
  border:1px solid var(--line);border-radius:999px;padding:4px 10px;cursor:pointer;}
.mapkey:hover{border-color:var(--accent);color:var(--accent);}
.mapkey.on{background:var(--accent);border-color:var(--accent);color:var(--panel);}
.mapkey i{width:9px;height:9px;border-radius:50%;display:inline-block;}
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

.band{margin:22px 0 6px;padding:7px 0 5px;border-bottom:1px solid var(--line);
  display:flex;align-items:baseline;gap:10px;font-weight:600;font-size:14px}
.band .sub{font-weight:400;font-size:12px;color:var(--faint)}
.cotnote{font-size:12px;color:var(--muted);margin:8px 0 2px;padding:7px 10px;
  border-left:2px solid var(--line);background:var(--line2);border-radius:0 3px 3px 0}
.cotgrp{margin:14px 0 4px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--faint);display:flex;align-items:baseline;gap:8px}
table.cot{width:100%;border-collapse:collapse;font-size:13px;
  font-variant-numeric:tabular-nums}
table.cot td{padding:4px 6px;border-bottom:1px solid var(--line2);vertical-align:middle}
table.cot td.c{width:44%;color:var(--ink)}
table.cot td.n{text-align:right;width:15%;white-space:nowrap}
table.cot td.w{text-align:right;width:15%;white-space:nowrap;color:var(--muted)}
table.cot td.p{width:26%}
.pbar{display:flex;align-items:center;gap:7px}
.pbar .t{width:78px;height:5px;background:var(--bar);border-radius:3px;position:relative;
  flex:none;overflow:hidden}
.pbar .t i{position:absolute;top:0;bottom:0;width:3px;background:var(--ink);border-radius:2px}
.pbar .v{font-size:11px;color:var(--faint);width:30px;flex:none}
.lg{color:var(--long)}.sh{color:var(--short)}
.cotmore{font-size:12px;color:var(--accent);cursor:pointer;padding:6px 6px 2px;
  display:inline-block;background:none;border:0;font-family:inherit}
.cotmore:hover{text-decoration:underline}
.cotstale{font-size:12px;color:var(--medium);margin:6px 0}
.council{margin:14px 0 0;border-top:1px solid var(--line);padding-top:10px}
.chead{font-family:var(--sans);font-size:10px;font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--faint);margin:0 0 7px}
.csolo{font-family:var(--sans);font-size:11.5px;color:var(--medium);
  border-left:2px solid var(--medium);padding:6px 9px;margin:0 0 8px;line-height:1.5}
.cdet{border:1px solid var(--line);border-radius:6px;margin:0 0 6px;background:var(--panel)}
.cdet summary{font-family:var(--sans);font-size:12px;font-weight:600;padding:7px 11px;
  cursor:pointer;color:var(--muted);list-style:none}
.cdet summary::-webkit-details-marker{display:none}
.cdet summary:before{content:'\u25b8';display:inline-block;margin-right:7px;
  color:var(--faint);transition:transform .15s}
.cdet[open] summary:before{transform:rotate(90deg)}
.cdet summary:hover{color:var(--accent)}
.cdet .cm{font-weight:400;color:var(--faint);font-size:10.5px}
.cbody{padding:2px 13px 10px;font-size:13.5px;line-height:1.6}
.cbody p{margin:0 0 8px}
.qok{font-family:var(--sans);font-size:11px;color:var(--strong);margin:8px 0 0;
  letter-spacing:.01em}
.qbad{font-family:var(--sans);font-size:12px;color:var(--medium);margin:10px 0 0;
  border-left:2px solid var(--medium);background:var(--line2);
  padding:8px 11px;border-radius:0 4px 4px 0;line-height:1.5}
.qbad-q{font-family:var(--serif);font-size:12.5px;color:var(--ink);
  margin:6px 0 0;padding-left:8px;border-left:1px solid var(--line)}
.qbad-n{margin-top:7px;color:var(--muted);font-size:11px}
.bywhom{font-family:var(--sans);font-size:10.5px;color:var(--faint);
  margin:6px 0 0;letter-spacing:.02em}
.askmode{display:flex;gap:0;margin:0 0 7px;align-self:flex-start;
  border:1px solid var(--line);border-radius:7px;overflow:hidden;width:max-content}
.askmode button{font-family:var(--sans);font-size:11.5px;font-weight:600;
  letter-spacing:.02em;padding:5px 13px;border:0;background:var(--panel);
  color:var(--muted);cursor:pointer;transition:background .12s,color .12s}
.askmode button+button{border-left:1px solid var(--line)}
.askmode button:hover:not(.on){color:var(--accent)}
.askmode button.on{background:var(--ink);color:var(--panel)}
.bwrap{max-width:none;padding:22px 20px 60px}
.bhead{font-family:var(--sans);font-size:12px;color:var(--muted);margin:0 0 12px;line-height:1.55}
.bform{display:flex;gap:8px;margin:0 0 6px}
.bform textarea{flex:1;font-family:var(--sans);font-size:13.5px;line-height:1.5;
  padding:10px 12px;border:1px solid var(--line);border-radius:7px;background:var(--panel);
  color:var(--ink);resize:vertical;min-height:62px}
.bform textarea:focus{outline:none;border-color:var(--accent)}
.bform button{font-family:var(--sans);font-size:12.5px;font-weight:600;padding:0 16px;
  border:0;border-radius:7px;background:var(--ink);color:var(--panel);cursor:pointer;align-self:stretch}
.bform button:disabled{opacity:.5;cursor:default}
.bex{font-family:var(--sans);font-size:11.5px;color:var(--faint);margin:0 0 20px}
.bex b{font-weight:500;color:var(--accent);cursor:pointer;border-bottom:1px dotted var(--accent)}
.bsec{margin:26px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line);
  display:flex;align-items:baseline;gap:9px;font-weight:600;font-size:14px}
.bsec .n{font-weight:400;font-size:11.5px;color:var(--faint)}
.bcard{border:1px solid var(--line);border-radius:8px;padding:11px 13px;margin:0 0 9px;
  background:var(--panel)}
.bcard h4{margin:0 0 4px;font-size:13.5px;font-weight:600;line-height:1.35}
.bcard .fam{display:inline-block;font-family:var(--sans);font-size:10px;font-weight:700;
  letter-spacing:.06em;text-transform:uppercase;color:var(--accent);
  border:1px solid var(--accent);border-radius:3px;padding:1px 5px;margin-right:7px}
.bcard .row{font-size:12.5px;color:var(--muted);margin:5px 0 0;line-height:1.5}
.bcard .row b{color:var(--ink);font-weight:600}
.bcard .src{font-family:var(--sans);font-size:11px;color:var(--faint);margin-top:7px}
.bcard .src a{color:var(--faint);text-decoration:none;border-bottom:1px solid var(--line)}
.bcard .src a:hover{color:var(--accent)}
.bpit{border-left:2px solid var(--medium);background:var(--line2);border-radius:0 5px 5px 0;
  padding:8px 11px;margin:0 0 8px;font-size:12.5px;line-height:1.5}
.bdgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:8px}
.bd{border:1px solid var(--line);border-radius:7px;padding:9px 11px;background:var(--panel);font-size:12.5px}
.bd .nm{font-weight:600;margin-bottom:3px}
.bd .mt{font-family:var(--sans);font-size:11px;color:var(--faint)}
.acc{display:inline-block;font-family:var(--sans);font-size:9.5px;font-weight:700;
  letter-spacing:.05em;text-transform:uppercase;border-radius:3px;padding:1px 5px;margin-left:5px}
.acc.public{color:var(--strong);border:1px solid var(--strong)}
.acc.licensed,.acc.proprietary{color:var(--medium);border:1px solid var(--medium)}
.acc.unclear,.acc.synthetic{color:var(--faint);border:1px solid var(--line)}
.bnote{font-family:var(--sans);font-size:11.5px;color:var(--faint);margin:6px 0 0;line-height:1.5}
.unver{display:inline-block;font-family:var(--sans);font-size:10px;font-weight:700;
  letter-spacing:.06em;text-transform:uppercase;color:var(--medium);
  border:1px solid var(--medium);border-radius:3px;padding:1px 5px;margin-right:6px;
  vertical-align:1px;cursor:help}
.sechead{font-family:var(--sans);font-size:10px;font-weight:700;letter-spacing:.13em;
  text-transform:uppercase;color:var(--faint);margin:0;padding:20px 18px 6px;
  border-bottom:1px solid var(--ink);display:flex;justify-content:space-between;align-items:baseline;}
.sechead .cnt{font-family:var(--sans);font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--faint);font-weight:400;}
.sechead.t2{color:var(--muted);border-bottom-color:var(--line);}
.entry{display:grid;grid-template-columns:40px 1fr;gap:16px;padding:18px 22px;margin:0;cursor:pointer;
  background:transparent;border:0;border-bottom:1px solid var(--line2);border-radius:0;box-shadow:none;
  transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease;}
.entry:hover{background:var(--panel);}
.entry.on{background:var(--panel);box-shadow:inset 2px 0 0 var(--accent);}
.entry.on .title{color:var(--accent);}
.entry .summary,.entry .subs{display:none;}
.entry .title{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.rail{text-align:center;padding-top:2px;}
.rail .rank{font-family:var(--sans);font-size:15px;font-weight:700;line-height:1;color:var(--accent);
  font-variant-numeric:tabular-nums;padding:0;margin:0 0 4px;border:0;text-align:right;}
.gauge{display:block;width:100%;height:auto;border-radius:0;margin:0;padding-bottom:7px;
  background:linear-gradient(90deg,var(--gc,var(--accent)) 0 calc(var(--pct,0)*1%),
    var(--line) calc(var(--pct,0)*1%) 100%) no-repeat bottom/100% 2px;}
.gauge span{display:block;width:auto;height:auto;border-radius:0;background:none;
  font-family:var(--sans);font-size:13.5px;font-weight:700;line-height:1.15;text-align:right;
  font-variant-numeric:tabular-nums;color:var(--gc,var(--accent));}
.rail .cap{display:none;}
.title{font-size:17px;font-weight:600;line-height:1.35;text-wrap:pretty;transition:color .12s;}
.title:hover{color:var(--accent);text-decoration:underline;text-underline-offset:2px;}
.meta{font-family:var(--sans);font-size:12.5px;letter-spacing:.02em;color:var(--muted);margin-top:6px;line-height:1.55;}
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
.summary{font-size:16px;line-height:1.65;color:var(--ink);margin-top:10px;max-width:72ch;}
.empty{color:var(--muted);font-style:italic;padding:56px 0;text-align:center;}
.entry.classic{grid-template-columns:1fr;}
.cwrap{display:flex;align-items:baseline;justify-content:space-between;gap:14px;}
.cites{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--cite);white-space:nowrap;}
.cites small{font-family:var(--sans);font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);font-weight:400;margin-left:3px;}
.bar{height:2px;background:var(--line);margin:8px 0 0;border-radius:2px;overflow:hidden;}
.bar i{display:block;height:100%;background:var(--cite);opacity:.55;}
.rail .yr{font-family:var(--sans);font-size:15px;font-weight:700;line-height:1.15;color:var(--muted);font-variant-numeric:tabular-nums;text-align:right;}
/* renamed from .tag: it collided with the sleeve rail's .tag at equal
   specificity and, being later, restyled every sleeve chip to a 9px
   uppercase canon badge with a currentColor border */
.ctag{font-family:var(--sans);font-size:9px;letter-spacing:.09em;text-transform:uppercase;font-weight:600;
  padding:2px 7px;border-radius:20px;white-space:nowrap;border:1px solid;flex:none;}
.ctag.theory{color:var(--accent);border-color:var(--accent);}
.ctag.method{color:var(--medium);border-color:var(--medium);}
.ctag.empirical{color:var(--strong);border-color:var(--strong);}
.ctag.frontier{color:var(--panel);background:var(--accent);border-color:var(--accent);}
.ctag.modern{color:var(--panel);background:var(--medium);border-color:var(--medium);}
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
.pdfpages{display:flex;flex-direction:column;gap:14px;align-items:center;padding:12px 0;}
.pdfpage{max-width:100%;height:auto;background:#fff;border:1px solid var(--line);
  box-shadow:0 2px 10px rgba(0,0,0,.16);border-radius:2px;}
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
/* ---- detail pane: what used to be crammed onto every card ---- */
.dwrap{padding:30px 32px 60px;max-width:640px;}
.dempty{color:var(--faint);font-style:italic;padding:56px 24px;text-align:center;}
.dkick{font-family:var(--sans);font-size:10.5px;font-weight:700;letter-spacing:.13em;
  text-transform:uppercase;color:var(--faint);line-height:1.5;}
.dtitle{font-size:27px;font-weight:600;line-height:1.22;letter-spacing:-.013em;margin:11px 0 0;text-wrap:pretty;}
.dauth{font-style:italic;font-size:15.5px;color:var(--muted);margin-top:9px;line-height:1.4;}
.dacts{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin:20px 0 0;
  padding-bottom:24px;border-bottom:1px solid var(--line);}
.dbtn{font-family:var(--sans);font-size:11.5px;font-weight:600;letter-spacing:.02em;
  color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:5px;
  padding:7px 13px;cursor:pointer;transition:border-color .15s,color .15s;}
.dbtn:hover{border-color:var(--accent);color:var(--accent);}
.dbtn.prim{color:var(--panel);background:var(--ink);border-color:var(--ink);}
.dbtn.prim:hover{color:var(--panel);opacity:.85;}
.dsum{font-size:17px;line-height:1.58;margin:22px 0 0;max-width:62ch;text-wrap:pretty;}
.drubh{display:flex;align-items:baseline;justify-content:space-between;gap:14px;margin-top:34px;
  padding-bottom:9px;border-bottom:1px solid var(--ink);font-family:var(--sans);font-size:9.5px;
  font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);}
.drubh em{font-style:normal;font-size:11px;letter-spacing:.03em;text-transform:none;color:var(--faint);}
.drub{display:flex;flex-direction:column;gap:0;margin:0;}
.drub .sub{flex-direction:row;align-items:center;gap:12px;min-width:0;padding:9px 0;
  border-bottom:1px solid var(--line2);}
.drub .sub i{flex:1;font-size:11.5px;letter-spacing:0;text-transform:none;color:var(--muted);}
.drub .sub b{width:54px;text-align:right;flex:none;}
.drub .sub s{width:100px;flex:none;}
.dnote{font-family:var(--sans);font-size:11px;color:var(--faint);margin-top:11px;}

/* ---- keyboard help ---- */
#helpwrap{position:fixed;inset:0;z-index:70;display:flex;align-items:center;justify-content:center;
  padding:40px;background:color-mix(in srgb,var(--ink) 45%,transparent);}
#helpwrap[hidden]{display:none;}
.helpcard{width:420px;max-width:100%;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:24px 26px 20px;box-shadow:0 30px 70px -30px rgba(0,0,0,.5);}
.helphead{display:flex;align-items:baseline;justify-content:space-between;gap:14px;
  padding-bottom:10px;border-bottom:1px solid var(--ink);}
.helphead b{font-family:var(--serif);font-size:17px;font-weight:600;letter-spacing:-.01em;}
.helphead span{font-family:var(--sans);font-size:10px;font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--faint);}
.helprow{display:grid;grid-template-columns:78px 1fr;gap:14px;align-items:baseline;
  padding:9px 0;border-bottom:1px solid var(--line2);}
.helprow kbd{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;font-weight:600;
  color:var(--ink);background:var(--line2);border-radius:4px;padding:2px 7px;justify-self:start;}
.helprow span{font-family:var(--sans);font-size:12.5px;color:var(--muted);}
.helplink{font-family:var(--sans);font-size:10.5px;font-weight:600;color:var(--muted);
  background:none;border:0;padding:0;margin-top:9px;cursor:pointer;text-decoration:underline;
  text-underline-offset:2px;display:block;}

/* ---- the shell owns the breakpoints now; these win over the older rules ---- */
/* Below this the detail pane is dropped, so the row has to carry the summary
   again -- otherwise it is in the DOM and shown by nothing. */
@media (max-width:940px){
  #app{grid-template-columns:224px minmax(0,1fr);}
  #detail{display:none;}
  .entry .summary{display:block;}
  .entry .title{-webkit-line-clamp:3;}
}
@media (max-width:820px){
  /* The rail becomes a top bar here; it used to be display:none, which left a
     narrow window with NO navigation at all -- no menu, no fallback, every tab
     unreachable, and nothing on screen to say they existed. A three-pane
     layout has to collapse to fewer panes, not to fewer features. */
  #app{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr);}
  #rail{display:flex;border-right:0;border-bottom:1px solid var(--line);
    max-height:46vh;overflow-y:auto;}
  .mast{padding:12px 14px 0;}
  /* vertical list -> wrapping pills, so a dozen tabs fit above the content
     instead of pushing it off the screen */
  .subtabs{flex-direction:row;flex-wrap:wrap;gap:5px;padding:6px 0 10px;}
  .subtabs button{border-left:0;border:1px solid var(--line);border-radius:20px;
    padding:5px 11px;font-size:12px;text-align:center;}
  .subtabs button.on{border-color:var(--accent);background:var(--panel);}
  .navtools{flex-direction:row;flex-wrap:wrap;align-items:center;padding:0 0 10px;}
  .navtools select,.searchwrap{flex:1 1 140px;}
}
.entry{grid-template-columns:40px 1fr;}
.entry.flat,.entry.classic{grid-template-columns:1fr;}
.rail{display:block;text-align:right;padding:2px 0 0;}
footer{padding:0 18px 8px;margin-top:32px;}
.empty{padding:56px 22px;}
.tagbar{padding:0 0 4px;}
.katex{font-size:1.02em;}
.katex-display{overflow-x:auto;overflow-y:hidden;padding:2px 0;}
</style>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>
</head>
<body>
<div id="app">
<aside id="rail">
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
      <button id="t-map">Map</button>
      <button id="t-pmap" hidden></button>
      <button id="t-pdf" hidden></button>
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
      <select id="asrc" title="Journal or source" style="display:none"></select>
      <button id="pdfonly" class="navtoggle" title="Only papers with a PDF we can open" style="display:none">PDF only</button>
      <select id="jsel" title="Journal" style="display:none"></select>
      <select id="month" style="display:none"></select>
      <select id="nbermonth" title="NBER month" style="display:none"></select>
      <span class="sp"></span>
      <span class="searchwrap"><input id="q" type="search" placeholder="Search…" autocomplete="off"></span>
    </div>
  </div>
</aside>
<main id="listcol"><div id="view"></div>
  <footer><div id="freshness" class="fresh"></div>
    Rubric scores are an ensemble consensus of multiple LLMs (Groq · Mistral ·
    OpenAI) — skim, don't trust blindly. Practitioner posts are listed as-is, not scored.
    Sources: NEP · NBER · arXiv · finance journals &amp; SSRN via Crossref · PM-Research ·
    OpenAlex · practitioner &amp; asset-manager research.
  <button class="helplink" onclick="toggleHelp()">Keyboard shortcuts (?)</button>
  </footer>
</main>
<aside id="detail"><div class="dempty">Select a paper.</div></aside>
</div>
<div id="helpwrap" hidden></div>
<script>
let DATA=[], CLASSICS=[], MONTHLY={}, NBER={}, VIEW="recent", MAXSEEN="";
let SEL=null, ROWMAP={};
// The record a row was actually rendered from. ITEM_BY_URL is a merged registry
// -- monthly/classics records overwrite the exported ones and carry different
// fields -- so the detail pane reads the row's own object instead.
const _rk=x=>{if(x&&x.url)ROWMAP[x.url]=x;return (x&&x.url)?' data-key="'+esc(x.url)+'"':'';};
let ARCHIVE_DATA=null, archiveLoading=false;
const TOPICS=__TOPICS_JSON__;
const SLEEVES=__SLEEVES_JSON__;
const SLEEVE_LABEL=Object.fromEntries(SLEEVES);
const PINS_KEY='qd_pins_v1', PIN_MAX=4;
let SLEEVE='all', PINS=[];
const BASE_PAPERS=['recent','foryou','watched','nber','monthly','practitioners','archive','map','pmap','pdf'];
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
  // A resolver's answer wins over any pattern: it is a URL something actually
  // fetched, where the patterns below are derivations that can go stale.
  if(x.pdf_url)return x.pdf_url;
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
// docs/nber.json records carried no uid until now, and both _mapBtn and
// _implBtn key off one -- so NBER cards rendered with NEITHER button, which
// reads as the feature not existing rather than as not applying here. The
// writers now emit uid, but the file already on disk holds 16 years of records
// without one, so resolve by URL against whatever is loaded until it is
// regenerated. Same for any other renderer whose records are slimmed copies.
let UID_BY_URL=null;
function _uidIndex(){
  if(UID_BY_URL)return UID_BY_URL;
  UID_BY_URL={};
  const add=r=>{if(r&&r.url&&r.uid)UID_BY_URL[r.url]=r.uid;};
  (ARCHIVE_DATA||[]).forEach(add);
  (DATA||[]).forEach(add);
  return UID_BY_URL;
}
function _uid(x){
  if(!x)return '';
  if(x.uid)return x.uid;
  return (x.url&&_uidIndex()[x.url])||'';
}
function _ftBtn(x){
  const uid=_uid(x);
  return (FT_SET&&uid&&FT_SET.has(uid))
    ?'<span class="ftmark" title="Full text parsed - Ask can quote this paper by section">full text</span>':'';
}
// Turn ONE paper's method into a specification: notation, equations,
// pseudocode with the timing written on every line, the traps, and an explicit
// list of what the paper never says.
//
// Only offered when the full text is parsed, and the gate is enforced again
// server-side in functions/api/ask.js -- a disabled button is a courtesy, not
// a control, and the reason it exists here is so the limit is legible BEFORE
// someone spends a call rather than after. An abstract cannot support a lag
// structure or a coefficient, and pseudocode is exactly those things.
function _implBtn(x){
  const uid=_uid(x);
  if(!uid)return'';
  const full=FT_SET&&FT_SET.has(uid);
  if(!full){
    // Not disabled -- an INVITATION. The gate is real (an abstract cannot
    // support a lag structure) but it is a missing input, not a permanent no,
    // and a greyed control that only explains why is a dead end. If you have
    // the PDF, the paper can be read here and now.
    return `<button class="mapbtn addpdf" data-addpdf="${esc(uid)}" title="Only the abstract is held, and pseudocode needs a specification, a lag structure and an exact sample. Add the PDF and it is parsed in your browser — the file itself is never uploaded.">▸ implement — add PDF</button>`;
  }
  return `<button class="mapbtn" data-impl="${esc(uid)}" title="Method, notation, pseudocode with timing, traps, and what the paper leaves unspecified">▸ implement</button>`;
}
function _pdfBtn(x){
  const p=_pdfUrl(x);
  if(!p)return '';
  // href is kept so middle-click and "open in new tab" still work; the click
  // handler opens it in the reading pane instead, because leaving the portal
  // to read a paper loses the tab, the filter and the place in the list.
  return `<a class="pdfbtn" href="${esc(p)}" data-pdf="${esc(p)}" data-pdftitle="${esc(x.title||'')}" target="_blank" rel="noopener" title="Open the PDF here">PDF</a>`;
}
// Every card gets a way into its own neighbourhood. This is the map that was
// actually wanted: not 11.5k points, but what sits around ONE paper.
function _mapBtn(x){
  const uid=_uid(x);
  if(!uid)return'';
  return `<button class="mapbtn" data-pmap="${esc(uid)}" title="Papers around this one">\u25c8 near</button>`;
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
  // Subject tags sit BELOW the sleeve chips and are visually quieter, because
  // the hierarchy is real: a sleeve says which book this belongs to, a tag says
  // what it is about. Same delegated-click pattern as the sleeve chips.
  const tg=tagChips(x);
  return `<div class="${cls}${x.url&&x.url===SEL?' on':''}"${_rk(x)}>${sc}<div class="body">${badge}
    <a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>
    <div class="meta">${watch}${_unver(x)}<span class="j">${esc(jlabel(x))}</span>${who} · ${esc(x.date||x.seen)}${x.topic?' · '+esc(x.topic):''}${x.consensus_n?' · '+x.consensus_n+'× '+(x.consensus_agree?'agree':'split'):''}${_ftBtn(x)}${_pdfBtn(x)}${_implBtn(x)}${_mapBtn(x)}${_saveBtn(x)}</div>
    ${chips}${tg}${sm}${subs}</div></div>`;
}
// Desk sleeves are MULTI-LABEL -- a paper can be carry AND fx at once -- so this
// is a membership test, not equality. Papers with no labels yet are HIDDEN when
// a sleeve is chosen rather than shown: an unlabelled paper is not evidence of
// belonging, and the backfill is still filling them in.
// Subject tags. TAG_SEL is a single active tag, not a set: two tags ANDed
// together on a 10k archive almost always returns nothing, and an empty result
// reads as a broken filter rather than as a narrow one.
let TAG_SEL='';
function tagChips(x){
  const t=(x.tags||[]);
  if(!t.length)return '';
  return '<div class="tags">'+t.map(k=>
    `<span class="tg${k===TAG_SEL?' on':''}" data-tag="${esc(k)}" title="Filter to ${esc(k)}">${esc(k)}</span>`
  ).join('')+'</div>';
}
function tagNote(){
  if(!TAG_SEL)return '';
  return `<span class="tagnote" data-tag="${esc(TAG_SEL)}" title="Clear this filter">tag: ${esc(TAG_SEL)} ×</span>`;
}
function tagFilter(rows){
  if(!TAG_SEL)return rows;
  return rows.filter(x=>(x.tags||[]).indexOf(TAG_SEL)>=0);
}
function setTag(k){
  TAG_SEL=(TAG_SEL===k)?'':k;      // clicking the active tag clears it
  archivePage=0;pracPage=0;render();
}
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
function savePins(){if(!STATE_LOADED)return;try{localStorage.setItem(PINS_KEY,JSON.stringify(PINS));}catch(e){}}
function setSleeve(k){
  SLEEVE=(SLEEVE===k)?'all':k;
  // choosing a sleeve from a pinned tab's view would be two filters fighting;
  // step back to Archive, where the tag rail is the only thing filtering
  if(VIEW.slice(0,3)==='sl:'){setView('archive');return;}
  archivePage=0;pracPage=0;renderTagbar();render();
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
  const row=e.target.closest('.entry[data-key]');
  if(row&&!e.target.closest('a,button')){
    SEL=row.dataset.key;
    document.querySelectorAll('.entry.on').forEach(el=>el.classList.remove('on'));
    row.classList.add('on');
    showDetail(SEL);
    return;
  }
  const c=e.target.closest('.sl[data-sleeve]');
  if(c){setSleeve(c.dataset.sleeve);return;}
  // delegated: cards are re-rendered on every filter change, so per-node
  // handlers would be rebound (and leak) constantly
  const m=e.target.closest('[data-pmap]');
  if(m){e.preventDefault();openPaperMap(m.dataset.pmap);return;}
  const im=e.target.closest('[data-impl]');
  if(im){e.preventDefault();openImplement(im.dataset.impl);}
  const ap=e.target.closest('[data-addpdf]');
  if(ap){e.preventDefault();e.stopPropagation();addPdf(ap.dataset.addpdf);return;}
  const pv=e.target.closest('[data-pdf]');
  // A modified click is a deliberate "open this elsewhere" and stays the
  // browser's to handle; a plain click reads it here.
  if(pv&&!(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)){
    e.preventDefault();e.stopPropagation();
    openPdf(pv.dataset.pdf,pv.dataset.pdftitle);return;
  }
  const tg=e.target.closest('[data-tag]');
  if(tg){e.preventDefault();e.stopPropagation();setTag(tg.dataset.tag);}
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
    return `<div class="entry flat${x.url===SEL?' on':''}"${_rk(x)}><div class="body">
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
// -------------------------------------------------------- Paper neighbourhood
// One paper at the centre and the papers the graph puts around it, which GROWS
// as you open nodes rather than jumping to a new centre. The first version
// re-seeded on click, so the paper you were studying vanished the moment you
// looked at anything near it -- the opposite of what a neighbourhood view is
// for. The seed is pinned; clicking pulls another node's neighbours in beside
// what is already there.
//
// Similarity edges draw faint and citations solid: a citation is a stated
// relationship and similarity an inferred one, and the picture should not
// present them as the same claim.
let PMAP_SEED=null;          // row of the paper under consideration -- never moves
let PMAP_OPEN=new Set();     // rows whose neighbours have been pulled in
let PMAP_NODES=null, pmapHover=-1, _pmapPos={};
const PMAP_N=24;             // neighbours per expansion
const PMAP_MAX=120;          // total nodes before we stop growing
function openPaperMap(uid){
  const r=_rowOf(uid);
  PMAP_SEED=(r>=0?r:null);PMAP_OPEN=new Set(r>=0?[r]:[]);
  _pmapPos={};pmapHover=-1;PMAP_NODES=null;
  setView('pmap');
}
function _rowOf(uid){
  if(!VEC_UIDS)return -1;
  if(!_rowIndex){_rowIndex={};VEC_UIDS.forEach((u,i)=>{_rowIndex[u]=i;});}
  const r=_rowIndex[uid];return r===undefined?-1:r;
}
let _rowIndex=null;
const _ptitle=u=>((ITEM_BY_UID[u]||{}).title||u||'')
  .toLowerCase().replace(/[^a-z0-9]+/g,' ').trim().slice(0,80);

// Top-N distinct neighbours of one row. Deduplicated by normalised title:
// the archive holds ~620 papers under two uids (an arXiv id and a title hash,
// an NBER paper and its DOI) and those twins sit at cosine 1.00, so without
// this a neighbourhood opens with the seed listed twice as its own neighbour.
function _neighboursOf(row, taken){
  const cand=[];
  eachEdge(row,(nb,kind,w)=>cand.push([nb,kind,w]));
  cand.sort((a,b)=>(b[1]===EDGE_CITE?1:b[2])-(a[1]===EDGE_CITE?1:a[2]));
  const out=[];
  for(const e of cand){
    const t=_ptitle(VEC_UIDS[e[0]]);
    if(taken.has(t))continue;
    taken.add(t);out.push(e);
    if(out.length>=PMAP_N)break;
  }
  return out;
}
function buildNeighbourhood(){
  if(PMAP_SEED===null||!E_OFF)return null;
  const taken=new Set([_ptitle(VEC_UIDS[PMAP_SEED])]);
  const rows=[PMAP_SEED], from={};
  let capped=false;
  for(const open of PMAP_OPEN){
    if(open!==PMAP_SEED&&rows.indexOf(open)<0){taken.add(_ptitle(VEC_UIDS[open]));rows.push(open);}
  }
  for(const open of PMAP_OPEN){
    for(const [nb] of _neighboursOf(open,taken)){
      if(rows.length>=PMAP_MAX){capped=true;break;}
      rows.push(nb);from[nb]=open;
    }
    if(capped)break;
  }
  const set=new Set(rows), index={};
  rows.forEach((r,i)=>{index[r]=i;});
  const links=[];
  for(const r of rows){
    eachEdge(r,(nb,kind,w)=>{
      if(!set.has(nb)||nb<=r)return;
      links.push([index[r],index[nb],kind,w]);
    });
  }
  const nodes=rows.map((r,i)=>{
    const u=VEC_UIDS[r], it=ITEM_BY_UID[u]||{};
    // Warm start: a node already on screen keeps its position, and a new one
    // appears NEXT TO whatever introduced it. Without this every expansion
    // relaxes from scratch and the whole picture jumps, which reads as "it
    // changed" -- the complaint this rewrite exists to fix.
    const prev=_pmapPos[r];
    const anchor=_pmapPos[from[r]];
    const jitter=()=>(Math.random()-0.5)*0.16;
    return {row:r,uid:u,it:it,seed:r===PMAP_SEED,open:PMAP_OPEN.has(r),
            x:prev?prev.x:(anchor?anchor.x+jitter():Math.cos(i)*0.7),
            y:prev?prev.y:(anchor?anchor.y+jitter():Math.sin(i)*0.7),
            vx:0,vy:0,warm:!!prev};
  });
  return {nodes:nodes,links:links,capped:capped};
}
// A small spring layout: inverse-square repulsion between every pair, springs
// along the edges with a rest length set by how strong the relationship is.
// Runs once per rebuild rather than animating -- no rAF loop to leak. Fewer
// ticks when most nodes are warm, since they only need to make room.
function layoutPmap(g){
  const N=g.nodes.length;
  const warm=g.nodes.filter(n=>n.warm).length;
  const ticks=warm>N*0.5?90:220;
  for(let t=0;t<ticks;t++){
    for(let i=0;i<N;i++){
      for(let j=i+1;j<N;j++){
        const a=g.nodes[i],b=g.nodes[j];
        const dx=b.x-a.x,dy=b.y-a.y,d2=dx*dx+dy*dy+0.0001;
        const rep=0.0016/d2, d=Math.sqrt(d2);
        a.vx-=rep*dx/d;a.vy-=rep*dy/d;b.vx+=rep*dx/d;b.vy+=rep*dy/d;
      }
    }
    for(const [i,j,kind,w] of g.links){
      const a=g.nodes[i],b=g.nodes[j];
      const dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)+0.0001;
      const rest=kind===EDGE_CITE?0.30:(0.75-Math.min(0.45,w*0.6));
      const f=(d-rest)*0.05;
      a.vx+=f*dx/d;a.vy+=f*dy/d;b.vx-=f*dx/d;b.vy-=f*dy/d;
    }
    for(const nd of g.nodes){
      if(nd.seed){nd.x=nd.y=0;nd.vx=nd.vy=0;continue;}
      nd.x+=nd.vx;nd.y+=nd.vy;nd.vx*=0.82;nd.vy*=0.82;
      const r=Math.hypot(nd.x,nd.y);
      if(r>1.0){nd.x/=r;nd.y/=r;}
    }
  }
  g.nodes.forEach(n=>{_pmapPos[n.row]={x:n.x,y:n.y};});
}
let _pmapTheme=null;
function drawPmap(){
  const cv=$('pmapcv');if(!cv||!PMAP_NODES)return;
  const g=PMAP_NODES, th=_pmapTheme||{ink:'#111',line:'#ddd',acc:'#0C5C4A'};
  const dpr=devicePixelRatio||1,w=cv.clientWidth,h=cv.clientHeight;
  cv.width=w*dpr;cv.height=h*dpr;
  const c=cv.getContext('2d');c.setTransform(dpr,0,0,dpr,0,0);
  c.clearRect(0,0,w,h);
  const R=Math.min(w,h)/2-46, cx=w/2, cy=h/2;
  const px=v=>cx+v*R, py=v=>cy+v*R;
  for(const [i,j,kind] of g.links){
    const a=g.nodes[i],b=g.nodes[j];
    c.beginPath();c.moveTo(px(a.x),py(a.y));c.lineTo(px(b.x),py(b.y));
    c.strokeStyle=kind===EDGE_CITE?th.acc:th.line;
    c.lineWidth=kind===EDGE_CITE?1.5:0.8;
    c.globalAlpha=kind===EDGE_CITE?0.75:0.45;
    c.stroke();
  }
  c.globalAlpha=1;
  g.nodes.forEach((nd,i)=>{
    const sc=nd.it.score, r=nd.seed?9:(sc!=null?4+sc/22:4.5);
    c.beginPath();c.arc(px(nd.x),py(nd.y),r,0,6.2832);
    c.fillStyle=nd.seed?th.acc:mapColorOf({c:0,s:nd.it.sleeves||[],
      p:nd.it.sleeves_prop||[],f:nd.it.desk_fit||0});
    c.fill();
    // an opened node keeps a ring, so it is obvious what has been expanded
    if(nd.seed||nd.open||i===pmapHover){
      c.strokeStyle=th.ink;c.lineWidth=nd.seed?1.9:1.3;
      c.globalAlpha=(nd.open&&!nd.seed&&i!==pmapHover)?0.45:1;
      c.stroke();c.globalAlpha=1;
    }
  });
  c.fillStyle=th.ink;c.font='600 12px ui-sans-serif,system-ui,sans-serif';
  c.textAlign='center';
  c.fillText((g.nodes[0].it.title||'').slice(0,74),cx,cy-18);
}
// Distance from the seed, for the list. Citation-linked papers lead: a stated
// relationship outranks an inferred one however high the cosine.
function _pmapRanked(g){
  const cite=new Set(), sim={};
  eachEdge(PMAP_SEED,(nb,kind,w)=>{
    if(kind===EDGE_CITE)cite.add(nb);
    sim[nb]=Math.max(sim[nb]||0,w);
  });
  return g.nodes.slice(1).map((nd,i)=>({nd:nd,i:i+1,
      cite:cite.has(nd.row),w:sim[nd.row]||0}))
    .sort((a,b)=>(b.cite-a.cite)||(b.w-a.w));
}
let _pmapResize=null;
function renderPaperMap(){
  if(!VEC||!ARCHIVE_DATA){loadIndex(renderPaperMap);
    $('view').innerHTML='<div class="empty">Loading the archive\u2026</div>';return;}
  if(EDGES===null){
    $('view').innerHTML='<div class="empty">Loading the graph\u2026</div>';
    loadEdges().then(renderPaperMap);return;
  }
  const seedIt=PMAP_SEED!==null?(ITEM_BY_UID[VEC_UIDS[PMAP_SEED]]||{}):{};
  const g=buildNeighbourhood();
  if(!g){
    $('view').innerHTML=`<div class="dateline">Neighbourhood</div>
      <div class="empty">This paper is not in the graph \u2014 it has no embedding yet.</div>`;
    return;
  }
  layoutPmap(g);PMAP_NODES=g;
  const ranked=_pmapRanked(g);
  const nc=g.links.filter(l=>l[2]===EDGE_CITE).length;
  const grown=PMAP_OPEN.size>1;
  $('view').innerHTML=`<div class="dateline">Around this paper <span class="n">\u00b7 ${g.nodes.length-1} papers \u00b7 ${g.links.length} links${nc?', '+nc+' citations':''}${grown?' \u00b7 '+PMAP_OPEN.size+' expanded':''}${g.capped?' \u00b7 at the '+PMAP_MAX+'-paper limit':''}</span></div>
    <div class="entry flat"><div class="body">
      <a class="title" href="${esc(seedIt.url||'#')}" target="_blank" rel="noopener">${esc(seedIt.title||'(unknown)')}</a>
      <div class="meta">${esc(seedIt.authors||'')}${seedIt.date?' \u00b7 '+esc(seedIt.date):''}</div></div></div>
    <div id="pmapwrap"><canvas id="pmapcv"></canvas><div id="maptip"></div></div>
    <div class="pmapbar">
      <span class="pmapnote">Solid lines are citations, faint lines similarity. Click a paper to pull its neighbours in \u2014 the centre stays put. \u25c8 near on a row re-centres the map on that paper instead.</span>
      ${grown?'<button class="mapkey" id="pmapreset">Reset to this paper</button>':''}
    </div>
    <div class="srch">Closest papers <span class="n">\u00b7 ${ranked.length} \u00b7 citations first, then similarity</span></div>
    ${ranked.map(r=>`<div class="pmaprow" data-node="${r.i}">
      <button class="expandbtn${r.nd.open?' done':''}" data-open="${r.nd.row}" ${r.nd.open?'disabled':''}
        title="${r.nd.open?'Already expanded':'Pull this paper\u2019s neighbours in'}">${r.nd.open?'\u2713':'+'}</button>
      <div class="pmaprowbody">${r.cite?'<span class="citetag">cites</span>':
        '<span class="simtag">'+Math.round(r.w*100)+'%</span>'}${entry(r.nd.it)}</div>
    </div>`).join('')}`;
  // hoisted: this was read three times per frame from getComputedStyle
  const cs=getComputedStyle(document.documentElement);
  _pmapTheme={ink:cs.getPropertyValue('--ink').trim()||'#111',
              line:cs.getPropertyValue('--line').trim()||'#ddd',
              acc:cs.getPropertyValue('--accent').trim()||'#0C5C4A'};
  const cv=$('pmapcv'),tip=$('maptip');
  drawPmap();
  // one listener, not one per visit -- this used to accumulate a handler every
  // time the view was opened
  if(_pmapResize)removeEventListener('resize',_pmapResize);
  _pmapResize=()=>{if(VIEW==='pmap')drawPmap();};
  addEventListener('resize',_pmapResize,{passive:true});
  const hit=e=>{
    const r=cv.getBoundingClientRect();
    const R=Math.min(r.width,r.height)/2-46,cx=r.width/2,cy=r.height/2;
    const mx=e.clientX-r.left,my=e.clientY-r.top;
    let best=-1,bd=14;
    PMAP_NODES.nodes.forEach((nd,i)=>{
      const d=Math.hypot(cx+nd.x*R-mx,cy+nd.y*R-my);
      if(d<bd){bd=d;best=i;}
    });
    return[best,mx,my,r];
  };
  cv.onmousemove=e=>{
    const [best,mx,my,r]=hit(e);
    if(best!==pmapHover){pmapHover=best;drawPmap();_pmapSyncRow(best);}
    if(best<0){tip.classList.remove('on');return;}
    const nd=PMAP_NODES.nodes[best];
    tip.innerHTML=esc(nd.it.title||nd.uid)+
      (nd.it.authors?'<br><span style="opacity:.7">'+esc(nd.it.authors)+'</span>':'')+
      (nd.seed?'':'<br><span style="opacity:.55">click to pull in its neighbours</span>');
    tip.style.left=Math.min(mx+14,r.width-350)+'px';tip.style.top=(my+14)+'px';
    tip.classList.add('on');
  };
  cv.onmouseleave=()=>{tip.classList.remove('on');pmapHover=-1;drawPmap();_pmapSyncRow(-1);};
  cv.onclick=e=>{
    const [best]=hit(e);
    if(best<0)return;
    const nd=PMAP_NODES.nodes[best];
    // the seed, and anything already expanded, opens the paper instead
    if(nd.seed||nd.open){if(nd.it.url)open(nd.it.url,'_blank','noopener');return;}
    expandNode(nd.row);
  };
  const rst=$('pmapreset');
  if(rst)rst.onclick=()=>{PMAP_OPEN=new Set([PMAP_SEED]);_pmapPos={};renderPaperMap();};
  document.querySelectorAll('[data-open]').forEach(b=>b.onclick=e=>{
    e.preventDefault();e.stopPropagation();expandNode(Number(b.dataset.open));});
  document.querySelectorAll('.pmaprow').forEach(el=>{
    el.onmouseenter=()=>{pmapHover=Number(el.dataset.node);drawPmap();};
    el.onmouseleave=()=>{pmapHover=-1;drawPmap();};
  });
}
function expandNode(row){
  if(PMAP_NODES&&PMAP_NODES.nodes.length>=PMAP_MAX){
    toast('At the '+PMAP_MAX+'-paper limit \u2014 reset to explore elsewhere.');return;
  }
  PMAP_OPEN.add(row);pmapHover=-1;renderPaperMap();
}
// keep the list and the canvas pointing at the same paper
function _pmapSyncRow(i){
  document.querySelectorAll('.pmaprow.on').forEach(el=>el.classList.remove('on'));
  if(i<0)return;
  const el=document.querySelector('.pmaprow[data-node="'+i+'"]');
  if(el)el.classList.add('on');
}

// ------------------------------------------------------------ Knowledge map
// docs/map.json, built offline by tools/map.py (PCA + k-means over the same
// centred vectors the graph uses). Rendered on canvas rather than as DOM
// nodes because 11.5k absolutely-positioned divs is a scroll-janking layout,
// and none of them need to be individually interactive -- one hit test on
// mousemove does the whole job.
let MAP=null, mapLoading=false, MAP_COLOR='cluster', mapHover=-1;
const MAP_PALETTE=['#4E79A7','#F28E2B','#E15759','#76B7B2','#59A14F','#EDC948',
  '#B07AA1','#FF9DA7','#9C755F','#BAB0AC','#86BCB6','#D37295','#A0CBE8',
  '#FFBE7D','#8CD17D','#F1CE63','#D4A6C8','#79706E','#499894','#E39802',
  '#B6992D','#FABFD2','#D7B5A6','#6B9AC4'];
function loadMap(cb){
  if(MAP){cb();return;}
  if(mapLoading)return;
  mapLoading=true;
  $('view').innerHTML='<div class="empty">Loading the map\u2026</div>';
  fetch('map.json').then(r=>r.json()).then(j=>{MAP=j;mapLoading=false;cb();})
    .catch(()=>{mapLoading=false;
      $('view').innerHTML='<div class="empty">No map yet \u2014 it is built on deploy by tools/map.py.</div>';});
}
// Colour tells you what you came to find out. By cluster it is a picture of
// what the archive contains; by sleeve it is the diagnostic -- if the sleeves
// describe something real they occupy regions, and if carry scatters that is
// evidence about the taxonomy, not the classifier.
function mapColorOf(pt){
  if(MAP_COLOR==='cluster')return MAP_PALETTE[pt.c%MAP_PALETTE.length];
  if(MAP_COLOR==='fit'){
    const f=pt.f||0;
    return f>=3?'#1F7A3D':f===2?'#59A14F':f===1?'#BAB0AC':'#E4E8E5';
  }
  const sl=(pt.p&&pt.p.length?pt.p:pt.s)||[];
  if(!sl.length)return '#E4E8E5';
  const i=SLEEVES.findIndex(([k])=>k===sl[0]);
  return i<0?'#E4E8E5':MAP_PALETTE[i%MAP_PALETTE.length];
}
function drawMap(){
  const cv=$('mapcv');if(!cv||!MAP)return;
  const dpr=devicePixelRatio||1, w=cv.clientWidth, h=cv.clientHeight;
  cv.width=w*dpr;cv.height=h*dpr;
  const g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);
  g.clearRect(0,0,w,h);
  const pad=18, sx=v=>pad+(v+1)/2*(w-2*pad), sy=v=>pad+(1-(v+1)/2)*(h-2*pad);
  for(const pt of MAP.p){
    g.beginPath();
    g.arc(sx(pt.x),sy(pt.y),1.7,0,6.2832);
    g.fillStyle=mapColorOf(pt);
    g.globalAlpha=0.72;
    g.fill();
  }
  g.globalAlpha=1;
  if(mapHover>=0){
    const pt=MAP.p[mapHover];
    g.beginPath();g.arc(sx(pt.x),sy(pt.y),5,0,6.2832);
    g.strokeStyle=getComputedStyle(document.documentElement)
      .getPropertyValue('--ink').trim()||'#000';
    g.lineWidth=1.6;g.stroke();
  }
}
function renderMap(){
  if(!MAP){loadMap(renderMap);return;}
  const modes=[['cluster','Clusters'],['sleeve','Desk sleeve'],['fit','Desk fit']];
  $('view').innerHTML=`<div class="dateline">Map <span class="n">\u00b7 ${MAP.n.toLocaleString()} papers \u00b7 ${MAP.clusters.length} clusters \u00b7 laid out by what they are about, not when they arrived</span></div>
    <div class="maplegend">${modes.map(([k,lab])=>
      `<button class="mapkey${MAP_COLOR===k?' on':''}" data-mode="${k}">${lab}</button>`).join('')}</div>
    <div id="mapwrap"><canvas id="mapcv"></canvas><div id="maptip"></div></div>
    <div class="maplegend">${MAP.clusters.slice().sort((a,b)=>b.n-a.n).slice(0,12).map(c=>
      `<span class="mapkey"><i style="background:${MAP_PALETTE[c.c%MAP_PALETTE.length]}"></i>${esc(c.label)} <span class="n">${c.n}</span></span>`).join('')}</div>`;
  document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{
    MAP_COLOR=b.dataset.mode;renderMap();});
  const cv=$('mapcv'), tip=$('maptip');
  drawMap();
  addEventListener('resize',drawMap,{passive:true});
  cv.onmousemove=e=>{
    const r=cv.getBoundingClientRect(), pad=18;
    const mx=e.clientX-r.left, my=e.clientY-r.top;
    const sx=v=>pad+(v+1)/2*(r.width-2*pad), sy=v=>pad+(1-(v+1)/2)*(r.height-2*pad);
    let best=-1,bd=100;
    for(let i=0;i<MAP.p.length;i++){
      const d=Math.hypot(sx(MAP.p[i].x)-mx,sy(MAP.p[i].y)-my);
      if(d<bd){bd=d;best=i;}
    }
    if(bd>9)best=-1;
    if(best!==mapHover){mapHover=best;drawMap();}
    if(best<0){tip.classList.remove('on');return;}
    const pt=MAP.p[best], sl=(pt.p&&pt.p.length?pt.p:pt.s)||[];
    tip.innerHTML=esc(pt.t)+(sl.length?'<br><span style="opacity:.7">'+
      esc(sl.map(k=>SLEEVE_LABEL[k]||k).join(' \u00b7 '))+'</span>':'');
    tip.style.left=Math.min(mx+14,r.width-350)+'px';
    tip.style.top=(my+14)+'px';
    tip.classList.add('on');
  };
  cv.onmouseleave=()=>{tip.classList.remove('on');mapHover=-1;drawMap();};
  cv.onclick=()=>{
    if(mapHover<0)return;
    const it=ITEM_BY_UID[MAP.p[mapHover].u];
    if(it&&it.url)open(it.url,'_blank','noopener');
  };
}

// ---------------------------------------------------------------- Ask
// A research agent over the whole archive. Retrieval happens HERE, in the
// browser: docs/vec.bin is an int8 matrix (one 256-dim unit vector per paper,
// built by tools/embed.py), so ranking 5.8k papers is ~1.5M integer multiply-
// adds -- a few milliseconds locally, and nothing but the question leaves the
// page until we have the shortlist. /api/ask (a Cloudflare Pages Function
// holding the API key) only embeds the question and writes the final synthesis.
let VEC=null,VEC_UIDS=null,VEC_DIM=0,VEC_SHARD=64,ITEM_BY_UID={},indexLoading=false;
// The model the index was BUILT with, read from vec.json and sent back with
// every query. The question has to be embedded by the same model and width
// as the index or it lands in a different vector space -- which returns
// confident nonsense, not an error. Naming it in two files and hoping they
// agree is how that happens; this makes the index describe itself.
let VEC_MODEL='',VEC_BUILD='';
let indexWarning='';
// The paper graph (docs/edges.bin, built by tools/graph.py). Packed triples of
// <srcRow uint32, dstRow uint32, kind uint8, weight float32> against
// vec.json's uid order, so traversal needs no lookup table and stays in the
// browser like the rest of retrieval.
// Adjacency as CSR (compressed sparse row), not a Map of Arrays. 166k edges
// became 332k directed entries, and a Map with 11.5k keys each holding a
// growable Array of tuples allocates ~500k objects -- which is what made the
// graph feel slow to arrive, not the 1 MB download. Three flat typed arrays
// allocate four times total and the parse is two linear passes.
let EDGES=null, edgesLoading=false, edgesPromise=null;
let E_OFF=null, E_DST=null, E_KIND=null, E_W=null;   // CSR
const EDGE_SIM=0, EDGE_CITE=1;
function loadEdges(){
  if(edgesPromise)return edgesPromise;
  edgesPromise=fetch('edges.bin').then(r=>r.arrayBuffer()).then(buf=>{
    const dv=new DataView(buf);
    if(buf.byteLength<12||dv.getUint32(0,false)!==0x51444731){  // "QDG1"
      EDGES=false;return;                                       // absent or stale
    }
    const nodes=dv.getUint32(4,true), m=dv.getUint32(8,true), width=dv.getUint8(12);
    const stride=width*2+2, base=16;
    const rd=width===2?(o)=>dv.getUint16(o,true):(o)=>dv.getUint32(o,true);
    // pass 1: how many entries each node needs (each edge counts both ways)
    const deg=new Int32Array(nodes+1);
    for(let i=0;i<m;i++){
      const o=base+i*stride;
      deg[rd(o)]++;deg[rd(o+width)]++;
    }
    E_OFF=new Int32Array(nodes+1);
    for(let i=0,run=0;i<=nodes;i++){E_OFF[i]=run;run+=deg[i]||0;}
    const fill=E_OFF.slice();
    E_DST=new Int32Array(m*2);E_KIND=new Uint8Array(m*2);E_W=new Uint8Array(m*2);
    // pass 2: place them
    for(let i=0;i<m;i++){
      const o=base+i*stride, a=rd(o), b=rd(o+width);
      const k=dv.getUint8(o+width*2), w=dv.getUint8(o+width*2+1);
      let q=fill[a]++;E_DST[q]=b;E_KIND[q]=k;E_W[q]=w;
      q=fill[b]++;E_DST[q]=a;E_KIND[q]=k;E_W[q]=w;
    }
    EDGES=true;
  }).catch(()=>{EDGES=false;});
  return edgesPromise;
}
const edgeCount=r=>(E_OFF&&r>=0&&r+1<E_OFF.length)?E_OFF[r+1]-E_OFF[r]:0;
// iterate a node's neighbours without allocating anything
function eachEdge(r,fn){
  if(!E_OFF||r<0||r+1>=E_OFF.length)return;
  for(let q=E_OFF[r];q<E_OFF[r+1];q++)fn(E_DST[q],E_KIND[q],E_W[q]/255);
}
// One hop out from the papers similarity already chose. This is the whole
// point of the graph: a paper that never uses the question's vocabulary but
// sits next to three papers that do is exactly what a cosine ranking misses.
// Citation neighbours count for more than similarity ones -- a reference is a
// stated relationship, not an inferred one.
function expandGraph(seedRows, limit){
  if(!E_OFF)return [];
  const seed=new Set(seedRows), mass=new Map();
  seedRows.forEach((r,i)=>{
    const decay=1/(1+i*0.15);            // earlier picks pull harder
    eachEdge(r,(nb,kind,w)=>{
      if(seed.has(nb))return;
      mass.set(nb,(mass.get(nb)||0)+(kind===EDGE_CITE?1.0:Math.max(0,w))*decay);
    });
  });
  return [...mass.entries()].sort((a,b)=>b[1]-a[1]).slice(0,limit);
}
// Ask is a CONVERSATION, not a series of one-shot queries. Turns are kept in
// order and replayed to the model, so "why?" or "what about costs?" resolve
// against what was actually said instead of starting cold every time.
const CHATS_KEY='qd_chats_v1', QUEUE_KEY='qd_queued_v1';
const CHAT_MAX=20;         // conversations kept in this browser
const CHAT_TURNS=12;       // turns persisted per conversation
const HIST_SEND=6;         // turns replayed to the model
const CTX_MAX=120;         // must match MAX_CTX in functions/api/ask.js
const OUTSIDE_SHOW=14;     // outside hits listed under an answer
const OUTSIDE_CTX=8;       // outside hits the agent is allowed to see
let CHATS=[],CHAT_ID=null,asking=false,ASK_OUTSIDE=true,QUEUED={};
// Analyse vs Build is a property of the CONVERSATION, not of the app: a
// follow-up ("now do it for FX instead") has to inherit the shape of the answer
// it is following up on, or the thread changes character halfway through.
function askMode(){const c=curChat();return (c&&c.mode)||'analyse';}
function setAskMode(m){const c=curChat();if(!c)return;c.mode=m;saveChats();renderAsk();}
// guards every browser-state writer: persisting before the load has run
// would overwrite the stored copy with an empty one
let STATE_LOADED=false;
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
  if(!STATE_LOADED)return;
  // Only what is needed to REDRAW and to REPLAY. A turn's passages and full
  // source records are re-derivable and are by far the biggest part of it,
  // and localStorage is a ~5MB budget already shared with Saved and the
  // answer cache -- persisting everything would evict the conversations.
  try{
    localStorage.setItem(CHATS_KEY,JSON.stringify(CHATS.slice(0,CHAT_MAX).map(c=>({
      id:c.id,title:c.title,ts:c.ts,
      turns:(c.turns||[]).filter(t=>t.state==='done').slice(-CHAT_TURNS).map(t=>({
        q:t.q,answer:t.answer,model:t.model,quotes:t.quotes,
        council:t.council,councilSolo:t.councilSolo,
        state:'done',ts:t.ts,cached:t.cached,
        sources:(t.sources||[]).map(x=>({title:x.title,url:x.url,authors:x.authors,
          date:x.date,seen:x.seen,source:x.source,score:x.score,uid:x.uid,
          _depth:x._depth,_sec:x._sec})),
        outside:(t.outside||[]).slice(0,OUTSIDE_SHOW)}))
    }))));
  }catch(e){}
}
function persistQueued(){if(!STATE_LOADED)return;try{localStorage.setItem(QUEUE_KEY,JSON.stringify(QUEUED));}catch(e){}}
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
// Widths. Only ASK_SCAN costs money -- it fans out to /api/ask in batches of
// SCAN_BATCH, so 96/16 = 6 calls a question against the old 3. Recall and the
// BM25 passage ranking are integer arithmetic in the browser and are free, so
// they are widened much further than the paid stage.
const GRAPH_SEED=25;                  // strongest candidates whose neighbours we follow
const GRAPH_EXPAND=60;                // neighbours pulled in at most
const GRAPH_W=0.12;                   // how much graph mass can lift a paper's rank
const ASK_RECALL=500;                 // candidates pulled by embedding similarity
const ASK_SCAN=96;                    // papers actually examined for content
const ASK_DEEP=18;                    // papers read in full (top of the ranking)
const SCAN_BATCH=16;                  // papers per screening call; fanned out in parallel
// weights: relevance to the QUESTION dominates; the archive's own quality score
// breaks ties so a strong paper outranks a mediocre one on equal topical fit
const W_SIM=0.55,W_KW=0.30,W_QUALITY=0.15;
// RANK FUSION, which replaced the weighted sum above for the FINAL ordering.
// W_SIM/W_KW/W_QUALITY still decide which papers seed the graph hop, and the
// eval seeds it the same way, so they are not vestigial.
//
// The weighted sum broke as the archive grew. scaleSims min-max rescales
// cosine across the candidate set; in a denser corpus the top 500 are packed
// into a tighter cosine range, so rescaling amplifies ever smaller true
// differences while kw still uses the whole of [0,1]. The weights said
// similarity dominates and the arithmetic increasingly said keyword overlap
// did. RRF fuses RANKS, so the shape of the value distribution cannot touch
// it. Measured over eval/golden.json at 20,999 rows, identical candidate set:
//
//     weighted sum      hit@20 0.53  MRR 0.440  fulltext 0.38  vocab 0.10
//     RRF K=5           hit@20 0.67  MRR 0.424  fulltext 0.62  vocab 0.30
//     RRF K=5 + lexical hit@20 0.70  MRR 0.530  fulltext 0.75  vocab 0.30
//
// K was swept, not inherited. The conventional 60 is tuned for fusing many
// long lists and is wrong here: MRR climbs monotonically as K falls (0.292 at
// 60, 0.363 at 20, 0.424 at 5) while hit@20 stays pinned. Below 3 it starts
// trading top-10 coverage for MRR, so 5 is the point that holds both.
const RRF_K=5;
// How deep the lexical channel reaches. It only covers the papers with parsed
// full text -- 2,381 of 20,999 -- so a deep list here costs nothing like a
// deep list over the whole archive would.
const BM25_RECALL=200;
const BM25_K1=1.2;
const BM25_B=0.75;
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
// Rescale similarity ACROSS THE CANDIDATE SET, so the three terms in askRank
// actually span comparable ranges. Call before ranking; sets _simN on each.
//
// WHY. eval/run.py measured the old ranking and found it was discarding papers
// the embedding had already found: cosine rank 1 came out 512th, rank 2 came
// out 356th, and 8 of 14 misses were inside the recall set before this
// function got to them. Ordering by cosine alone scored BETTER than the
// re-rank (hit@20 0.57 against 0.53), which is the clearest possible statement
// that the re-rank was subtracting value.
//
// The cause was a range mismatch rather than a wrong weight. kw uses the whole
// of [0,1] -- a question whose every word appears scores exactly 1 -- and
// quality reaches 1.2. But sim is a cosine, and cosines on this corpus sit
// between roughly 0.2 and 0.7 and never approach 1. A 0.55 weight on a term
// that moves through half its range is worth less than a 0.30 weight on a term
// that uses all of its own, so the weights said similarity dominates while the
// arithmetic said keyword overlap did. Fixing /127^2 to /127 removed one
// instance of this; this is the rest of it.
//
// Measured over eval/golden.json, same candidate set, re-rank alone:
//     hit@20  0.53 -> 0.63     vocab tier 0.10 -> 0.30
//     abstract tier 0.92 -> 1.00, MRR 0.508 -> 0.486
// RRF at k=20 reached hit@20 0.67 but dropped MRR to 0.355; one question of
// coverage is not worth that, and this keeps the weights tunable.
// ---- the lexical channel over parsed full text (docs/bm25.bin) --------
// WHY A SECOND INDEX. The semantic one embeds title and abstract, and an
// abstract does not name its data vendor. "Which cross-asset futures study
// uses Barchart end-of-day data" is answerable from the paper's body and from
// nowhere else; cosine put that paper 71st and the re-rank finished it at
// 33rd. Measured alone this channel takes the fulltext tier from 0.38 to 1.00
// and moves the vocab tier not at all -- exact-term matching cannot help when
// the question and the paper share no vocabulary. That asymmetry is the whole
// argument for fusing the two rather than choosing between them.
//
// Built by tools/bm25.py; format documented there. Loaded lazily on the first
// question, like vec.bin, and the portal works without it.
let BM=null,bmPromise=null;
function loadBm25(){
  if(BM)return Promise.resolve(BM);
  if(bmPromise)return bmPromise;
  bmPromise=Promise.all([
    fetch('bm25.bin').then(r=>{if(!r.ok)throw new Error('bm25.bin '+r.status);return r.arrayBuffer();}),
    fetch('bm25.json').then(r=>{if(!r.ok)throw new Error('bm25.json '+r.status);return r.json();})
  ]).then(([buf,meta])=>{BM=parseBm25(buf,meta);return BM;})
    .catch(e=>{
      // Degrade to the semantic channel alone, which is what retrieval did
      // before this existed. A missing lexical index must not break Ask.
      console.warn('[ask] lexical channel unavailable:',(e&&e.message)||e);
      return null;
    });
  return bmPromise;
}
function parseBm25(buf,meta){
  const dv=new DataView(buf),u8=new Uint8Array(buf);
  if(String.fromCharCode(u8[0],u8[1],u8[2],u8[3])!=='QBM1')
    throw new Error('bm25.bin has the wrong magic');
  const n=dv.getUint32(4,true),nterms=dv.getUint32(8,true),
        avgdl=dv.getFloat32(12,true),w=u8[16],
        tbLen=dv.getUint32(20,true),nrec=dv.getUint32(24,true);
  let o=28;
  const termblob=new Uint8Array(buf,o,tbLen);o+=tbLen;
  // .slice(), not a view: the offset arrays follow a variable-length term blob
  // and are therefore not 4-byte aligned, which a Uint32Array over the raw
  // buffer requires. Copying 0.9 MB once beats a DataView call per comparison.
  const toff=new Uint32Array(buf.slice(o,o+4*(nterms+1)));o+=4*(nterms+1);
  const poff=new Uint32Array(buf.slice(o,o+4*(nterms+1)));o+=4*(nterms+1);
  const post=new Uint8Array(buf,o,nrec*(w+1));o+=nrec*(w+1);
  const dlen=new Uint16Array(buf.slice(o,o+2*n));
  return {n:n,nterms:nterms,avgdl:avgdl,w:w,termblob:termblob,
          toff:toff,poff:poff,post:post,dlen:dlen,uids:meta.uids||[]};
}
// Bytewise binary search over the sorted term blob. Terms are [a-z0-9]+, so
// byte order and string order agree, and nothing has to decode 118,404 terms
// to find one.
function bmFind(term){
  const key=[];for(let i=0;i<term.length;i++)key.push(term.charCodeAt(i));
  let lo=0,hi=BM.nterms-1;
  while(lo<=hi){
    const mid=(lo+hi)>>1,a=BM.toff[mid],b=BM.toff[mid+1];
    let cmp=0;const len=Math.min(b-a,key.length);
    for(let i=0;i<len;i++){const d=BM.termblob[a+i]-key[i];if(d){cmp=d;break;}}
    if(!cmp)cmp=(b-a)-key.length;
    if(!cmp)return mid;
    if(cmp<0)lo=mid+1;else hi=mid-1;
  }
  return -1;
}
// Okapi BM25, the same arithmetic as tools/bm25.py's reader. Returns
// [[uid,score],...] descending.
function bmSearch(terms,limit){
  if(!BM)return[];
  const score=new Map(),done=new Set();
  for(const t of terms){
    if(done.has(t))continue;
    done.add(t);
    const i=bmFind(t);
    if(i<0)continue;
    const s=BM.poff[i],e=BM.poff[i+1],df=e-s;
    if(!df)continue;
    const idf=Math.log(1+(BM.n-df+0.5)/(df+0.5));
    for(let r=s;r<e;r++){
      const off=r*(BM.w+1);
      const doc=BM.w===2
        ?(BM.post[off]|(BM.post[off+1]<<8))
        :(BM.post[off]|(BM.post[off+1]<<8)|(BM.post[off+2]<<16)|(BM.post[off+3]<<24));
      const tf=BM.post[off+BM.w],dl=BM.dlen[doc]||1;
      const denom=tf+BM25_K1*(1-BM25_B+BM25_B*dl/BM.avgdl);
      score.set(doc,(score.get(doc)||0)+idf*(tf*(BM25_K1+1))/denom);
    }
  }
  return [...score.entries()].sort((a,b)=>b[1]-a[1]).slice(0,limit)
    .map(e=>[BM.uids[e[0]],e[1]]);
}

// Reciprocal Rank Fusion over five lists. Ports eval/run.py's _rescore rrf
// branch exactly -- 0-based positions, and a list a candidate is absent from
// contributes nothing rather than a last place. That last part matters: 89% of
// the archive has no parsed full text, and charging those papers a last place
// on the lexical list would punish them for a fact about our coverage.
function _ranksBy(cands,keyfn){
  const idx=cands.map((c,i)=>i);
  idx.sort((a,b)=>keyfn(cands[a])-keyfn(cands[b]));
  const out=new Array(cands.length);
  idx.forEach((i,pos)=>{out[i]=pos;});
  return out;
}
function fuseRank(cands,terms){
  if(!cands.length)return;
  const k=RRF_K;
  const rs=_ranksBy(cands,c=>-(c._sim||0));
  const rk=_ranksBy(cands,c=>-(0.6*kwHit(terms,c.title)+0.4*kwHit(terms,c.summary)));
  const rq=_ranksBy(cands,c=>-askQuality(c));
  const rm=_ranksBy(cands,c=>-(c._mass||0));
  const rb=_ranksBy(cands,c=>-(c._bm25||0));
  cands.forEach((c,i)=>{
    c._rank=1/(k+rs[i])+1/(k+rk[i])+1/(k+rq[i])
      +((c._mass||0)>0?1/(k+rm[i]):0)
      +((c._bm25||0)>0?1/(k+rb[i]):0);
  });
  cands.sort((a,b)=>b._rank-a._rank);
}
// Now used ONLY to choose which papers seed the graph hop; fuseRank decides
// the order anyone sees. eval/run.py seeds from this same function on the same
// raw cosine, and that correspondence is what makes its numbers apply here.
function askRank(x,terms){
  // /127, not /127^2: tools/embed.py stores unit vectors scaled by 127 and the
  // QUERY arrives as raw floats, so the dot product peaks near 127, not 16129.
  // Dividing by the square capped sim at ~0.008 against keyword's 0.30 and
  // made the embedding decide only which candidates got here before being
  // ignored.
  const sim=Math.max(0,Math.min(1,(x._sim||0)/127));
  const kw=0.6*kwHit(terms,x.title)+0.4*kwHit(terms,x.summary);
  return W_SIM*sim+W_KW*kw+W_QUALITY*askQuality(x);
}
const ASK_EXAMPLES=[
  "What does the archive say about the stock-bond correlation flip?",
  "Is there evidence trend following has decayed since 2010?",
  "What drives commodity carry beyond backwardation?",
  "Summarise recent work on term premium estimation.",
];
// Callbacks waiting on the index, each remembering WHICH VIEW asked. Two
// separate bugs lived in the old shape: the completion fired `if(VIEW==='ask')`
// so any other tab waited forever (Build showed "Loading the semantic index"
// permanently, and the neighbourhood map had the same defect), and
// `if(indexLoading)return` dropped a second caller's callback on the floor
// rather than queueing it. The view check belongs per-caller -- it exists so a
// tab the reader has since navigated away from does not repaint over them.
let _idxWaiters=[];
function loadIndex(cb){
  if(VEC&&ARCHIVE_DATA){cb();return;}
  _idxWaiters.push([VIEW,cb]);
  if(indexLoading)return;
  indexLoading=true;
  Promise.all([
    fetch('vec.json').then(r=>r.json()),
    fetch('vec.bin').then(r=>r.arrayBuffer()),
    ARCHIVE_DATA?Promise.resolve(ARCHIVE_DATA):fetch('archive.json').then(r=>r.json()),
  ]).then(([meta,buf,arch])=>{
    VEC_UIDS=meta.uids;VEC=new Int8Array(buf);VEC_DIM=meta.dim||0;VEC_SHARD=meta.shard||64;
    VEC_MODEL=meta.model||'';VEC_BUILD=meta.build||'';
    // vec.json is the MANIFEST for vec.bin -- row i of the buffer is uids[i].
    // If the two disagree, retrieve() reads past the end of an Int8Array,
    // which yields undefined rather than throwing: every score becomes NaN and
    // the comparator goes inconsistent, so the "top 200 by similarity" is not
    // sorted by anything. A 404 on vec.bin does not even reject -- .arrayBuffer()
    // happily returns the error body -- so this is the only place it can be caught.
    const rows=VEC_DIM?Math.floor(VEC.length/VEC_DIM):0;
    if(rows<VEC_UIDS.length){
      VEC_UIDS=VEC_UIDS.slice(0,rows);
      indexWarning=rows
        ? 'The search index is truncated: '+rows.toLocaleString()+' of '+
          (meta.n||0).toLocaleString()+' papers have vectors. Run the "Semantic Index" workflow.'
        : 'The search index is empty — run the "Semantic Index" workflow.';
    }else if(rows>VEC_UIDS.length){
      // The SYMMETRIC case, which used to pass in silence. A .bin with MORE
      // rows than the manifest names is just as much a mismatch: the surplus
      // papers are unaddressable, retrieve() never sees them, and nothing
      // anywhere says so. A half-written manifest looks exactly like a smaller
      // archive.
      indexWarning='The search index and its manifest disagree: vec.bin holds '+
        rows.toLocaleString()+' vectors but vec.json names only '+
        VEC_UIDS.length.toLocaleString()+
        '. The extra rows are unreachable — rebuild with the "Semantic Index" workflow.';
    }
    ARCHIVE_DATA=arch;
    arch.forEach(x=>{if(x.uid)ITEM_BY_UID[x.uid]=x;if(x.url)ITEM_BY_URL[x.url]=x;});
    indexLoading=false;
    // The graph is a bonus for Ask (it widens a result set) and required only
    // by the neighbourhood view, which awaits it itself. Holding the index
    // load on it put another megabyte in front of the first answer.
    loadEdges();
    const waiting=_idxWaiters;_idxWaiters=[];
    waiting.forEach(([view,fn])=>{if(VIEW===view)fn();});
  }).catch(()=>{
    indexLoading=false;
    const waiting=_idxWaiters;_idxWaiters=[];
    const msg='The semantic index has not been built yet — run the "Semantic Index" workflow once.';
    // Every waiter must be told, or a tab that asked sits on its loading
    // message forever with no indication anything went wrong.
    waiting.forEach(([view])=>{
      if(VIEW!==view)return;
      if(view==='ask')renderAsk(msg);
      else $('view').innerHTML='<div class="empty">'+esc(msg)+'</div>';
    });
  });
}
// cosine over unit vectors == dot product; int8 rounding is monotonic, so the
// 1/127 scale factor is irrelevant to the ranking and never applied
// Stage 1 -- recall by cosine over every indexed paper. Each candidate carries
// its raw dot product (_sim) and its vector row (_row, which addresses the
// abstract shard), so the blend stage has everything it needs without a
// second pass over the matrix.
function retrieve(qv,k){
  const dim=qv.length;
  // belt and braces: never iterate past the rows the buffer actually holds
  const n=Math.min(VEC_UIDS.length,dim?Math.floor(VEC.length/dim):0),out=[];
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
    // An unverified item is one the Claude digest mailed in whose link the
    // source site would not confirm. Fine to browse with a marker; not fine to
    // quote back as evidence in an answer, where the marker does not travel.
    if(it.unverified)continue;
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
try{ANS=JSON.parse(localStorage.getItem('qd_ans_v2')||'{}');}catch(e){ANS={};}
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
function persistAns(){try{localStorage.setItem('qd_ans_v2',JSON.stringify(_evict(ANS,ANS_MAX)));}catch(e){}}
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
const FT_PAPERS=14;                   // papers to open at passage depth
const FT_PASSAGES=16;                 // passages carried into the answer
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
      // A shard is keyed by ROW INDEX, and row indices only mean anything
      // relative to the manifest they were built with. A cached shard served
      // against a newer vec.json therefore hands back a DIFFERENT paper's
      // abstract, which goes into the Ask prompt as evidence -- no error, no
      // warning, a confident answer citing a paper that was never read.
      // Ascending row order makes that rare; _build makes it detectable.
      // Discarding is safe: the abstract is an enrichment, and an answer built
      // from summaries is merely thinner, while one built from the wrong
      // abstracts is wrong.
      fetch('abs/'+s+'.json').then(r=>r.json()).then(j=>{
        const rows=(j&&j.rows)?j.rows:j;          // pre-stamp shards were flat
        if(j&&j._build&&VEC_BUILD&&j._build!==VEC_BUILD){
          console.warn('abs shard '+s+' was built for index '+j._build
            +' but the manifest is '+VEC_BUILD+'; discarding it');
          ABS_SHARD[s]={};
          return;
        }
        ABS_SHARD[s]=rows||{};
      }).catch(()=>{ABS_SHARD[s]={};})));
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
// ---- Quote verification --------------------------------------------------
// The depth contract REQUIRES a verbatim quote next to any specification-level
// claim. Nothing checked it. QuantMind's summariser refuses a finding whose
// quote is not a literal substring of its chunk -- an assertion in code, not a
// request in prose -- and that single difference is why its citations can be
// trusted and ours could only be hoped over.
//
// We still hold every passage we sent, so this is a substring test, not new
// infrastructure. It FLAGS rather than rejects: the first pass also catches
// legitimate near-misses -- smart quotes, ellipsis, a collapsed line break --
// and normalising those is the actual work. Throwing away a good answer over
// punctuation would be a worse failure than the one being fixed.
//
// Normalisation is deliberately generous, because a false alarm trains you to
// ignore the flag, which is the only way this feature can truly fail.
function _qnorm(t){
  return String(t||'')
    .replace(/[\u2018\u2019\u201b\u2032]/g,"'")     // curly singles -> '
    .replace(/[\u201c\u201d\u201f\u2033]/g,'"')     // curly doubles -> "
    .replace(/[\u2010-\u2015\u2212]/g,'-')          // dashes/minus -> -
    .replace(/[\u2026]/g,'...')
    .replace(/\\s+/g,' ')
    .trim().toLowerCase();
}
// Quoted spans in the answer. Only doubles: single quotes are apostrophes far
// more often than citations, and a check that fires on "don't" is noise.
// Short spans are skipped -- a quoted term of art is not a sourced claim.
const _QMIN=25;
function _quotesIn(text){
  const out=[];
  const rx=/[\u201c"]([^\u201c\u201d"]{25,400})[\u201d"]/g;
  let m;
  while((m=rx.exec(String(text||''))))out.push(m[1]);
  return out;
}
// A quote verifies if it appears in ANY supplied source, not only the one it
// cites. Tying it to the bracketed number would flag correct quotes whenever
// the model cited the right passage under a neighbouring index, and the claim
// being tested here is "did this text come from the sources at all".
function verifyQuotes(answer,ctx){
  const quotes=_quotesIn(answer);
  if(!quotes.length)return null;
  const hay=(ctx||[]).map(p=>_qnorm(p.summary||'')).join(' \u2022 ');
  const missing=quotes.filter(q=>{
    const n=_qnorm(q);
    if(n.length<_QMIN)return false;
    if(hay.includes(n))return false;
    // A quote elided mid-sentence ("... and therefore") is still faithful to
    // its source; check the ends independently before calling it unsupported.
    const parts=n.split('...').map(x=>x.trim()).filter(x=>x.length>=_QMIN);
    return !(parts.length>1&&parts.every(x=>hay.includes(x)));
  });
  return {n:quotes.length,missing:missing};
}
function quoteBadge(v){
  if(!v||!v.n)return '';
  if(!v.missing.length)
    return `<div class="qok" title="Every quoted passage was found verbatim in the sources supplied to the model">`
      +`\u2713 ${v.n} quote${v.n>1?'s':''} verified against the sources</div>`;
  const list=v.missing.slice(0,3).map(q=>
    `<div class="qbad-q">\u201c${esc(q.slice(0,160))}${q.length>160?'\u2026':''}\u201d</div>`).join('');
  return `<div class="qbad" title="These strings were not found in any source sent to the model">`
    +`\u26a0 ${v.missing.length} of ${v.n} quoted passages could not be found in the sources`
    +list+`<div class="qbad-n">Treat the claims resting on these as unsupported until you have checked the paper.</div></div>`;
}
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
    turns.push({q:q,state:'done',answer:hit.answer,model:hit.model||'',cached:true,ts:Date.now(),
      sources:hit.sources||[],outside:hit.outside||[]});
    titleChat();saveChats();renderAsk();return;
  }
  asking=true;
  const turn={q:q,state:'retrieving',ts:Date.now()};
  turns.push(turn);titleChat();
  renderAsk();
  try{
    // Started before the embed is awaited so the two round-trips overlap: the
    // lexical index is 7.9 MB gzipped and fetching it after would add its
    // latency to the first question rather than hiding it behind one already
    // being paid for.
    const lex=loadBm25();
    const er=await fetch('/api/ask',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({mode:'embed',q:q,model:VEC_MODEL,dim:VEC_DIM})});
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
    // Ordering for the GRAPH SEED only. fuseRank decides the final order.
    cands.forEach(c=>{c._rank=askRank(c,terms);});
    cands.sort((a,b)=>b._rank-a._rank);
    // Graph hop: bring in neighbours of the strongest candidates that
    // similarity alone did not surface, then let them compete in the same
    // ranking rather than being appended blindly.
    let viaGraph=0;
    if(E_OFF){          // CSR present; EDGES is now a boolean flag, not a Map
      const seedRows=cands.slice(0,GRAPH_SEED).map(c=>c._row);
      const extraRows=expandGraph(seedRows,GRAPH_EXPAND);
      const known=new Set(cands.map(c=>c._row));
      for(const [row,m] of extraRows){
        if(known.has(row))continue;
        const uid=VEC_UIDS[row], it=ITEM_BY_UID[uid];
        if(!it)continue;
        // graph mass is carried, not folded into a score: in rank space an
        // additive bonus is not comparable to 1/(k+rank), and bolting one on
        // would make the graph either inert or overwhelming depending on K.
        const c=Object.assign({},it,{_row:row,_sim:0,_mass:m,_viaGraph:true});
        cands.push(c);viaGraph++;
      }
    }
    await lex;      // resolves to null if it is unavailable; never rejects
    // THE LEXICAL CHANNEL AS RECALL. Its purpose is the papers cosine never
    // returned -- ranks 2,889 and 5,124 among the measured misses -- so adding
    // it after the candidate list closed would be pointless. Anything it finds
    // that cosine already had simply gets a second vote.
    let viaLex=0;
    if(BM){
      const byUid=new Map();
      cands.forEach(c=>{const u=_uid(c);if(u)byUid.set(u,c);});
      for(const pair of bmSearch(terms,BM25_RECALL)){
        const uid=pair[0],sc=pair[1];
        const had=byUid.get(uid);
        if(had){had._bm25=sc;continue;}
        const it=ITEM_BY_UID[uid];
        if(!it||it.unverified)continue;
        cands.push(Object.assign({},it,{_row:-1,_sim:0,_mass:0,_bm25:sc,_viaLex:true}));
        viaLex++;
      }
    }
    fuseRank(cands,terms);
    turn.viaGraph=viaGraph;turn.viaLex=viaLex;
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
    // Build answers are written FROM the typed artifacts, so fetch them before
    // assembling context. Analyse never pays for this.
    if(askMode()==='build'){ try{ await loadArts(); }catch(e){} }
    // Typed artifacts, attached only in Build mode and only where they exist.
    // ARTS is keyed by uid, so a passage and its parent paper resolve to the
    // same block -- deliberately: the extractor read the whole paper, not the
    // one section BM25 happened to surface.
    const _arts=p=>(askMode()==='build'&&ARTS&&p&&p.uid)?ARTS[p.uid]:undefined;
    const ctx=psg.map(x=>({title:x.paper.title+(x.sec?' — '+x.sec:''),
        authors:x.paper.authors,date:x.paper.date,source:x.paper.source,
        topic:x.paper.topic,summary:x.text,depth:'full',
        artifacts:_arts(x.paper)}))
      .concat(picks.map(p=>({title:p.title,authors:p.authors,
        date:p.date,source:p.source,topic:p.topic,
        summary:foundBy[p._row]||p._full||p.summary,
        // tell the agent HOW MUCH text is behind each source, so it can refuse a
        // specification question on an abstract instead of inventing one
        depth:p._full?'abstract':'summary_only',
        artifacts:_arts(p)})));
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
    // Truncate HERE, keeping the outside hits: they are appended last, so a
    // server-side slice would drop them first and the whole point of looking
    // outside is lost silently. The displayed source list is cut to the same
    // length below, so [n] in the answer and [n] on screen stay in lockstep.
    const _out=outside.slice(0,OUTSIDE_CTX);
    const ctxAll=ctx.slice(0,Math.max(0,CTX_MAX-_out.length)).concat(_out.map(h=>({
      title:h.title,authors:h.authors,date:h.year?String(h.year):'',
      source:h.venue||h.via,summary:h.abstract||'',depth:'abstract',external:true})));
    // the source list must mirror ctx order exactly or [n] resolves to the
    // wrong paper -- the outside hits are appended in both, in the same order
    turn.sources=(turn.sources||[]).slice(0,Math.max(0,CTX_MAX-_out.length))
      .concat(_out.map(h=>({
      title:h.title,url:h.url,authors:h.authors,date:h.year?String(h.year):'',
      source:h.venue||'',uid:h.uid,_external:true})));
    turn.state='thinking';renderAsk();
    // ---- Council ---------------------------------------------------------
    // Four calls, sequenced HERE rather than inside one Function: the browser
    // already fans out scan batches this way, it keeps each request well inside
    // a Worker's limits, and it lets the UI show which stage is running instead
    // of one long opaque wait.
    if(askMode()==='council'){
      const call=async (role,prior,rotate)=>{
        const r=await fetch('/api/ask',{method:'POST',
          headers:{'content-type':'application/json'},
          body:JSON.stringify({mode:'council',role:role,q:q,ctx:ctxAll,
                               prior:prior||'',rotate:rotate})});
        const j=await r.json();
        if(!r.ok)throw new Error(j.error||(role+' failed'));
        return {text:j.answer||'',model:j.model||''};
      };
      turn.state='proposing';renderAsk();
      const prop=await call('propose','',0);
      turn.council={proposal:prop};renderAsk();

      turn.state='challenging';renderAsk();
      // rotate 1 and 2 so the challengers are answered by DIFFERENT providers
      // than the proposer where more than one is configured -- a model arguing
      // with itself agrees with itself
      // The newline escapes below are doubled on purpose: this text passes
      // through portal.py's non-raw _INDEX literal on its way to the browser,
      // so a single escape arrives as a real newline and splits the string.
      const priorProp='THE POSITION UNDER REVIEW:\\n\\n'+prop.text;
      const [ev,im]=await Promise.all([
        call('challenge_evidence',priorProp,1),
        call('challenge_implementation',priorProp,2),
      ]);
      turn.council={proposal:prop,evidence:ev,implementation:im};renderAsk();

      turn.state='reconciling';renderAsk();
      const rec=await call('reconcile',
        priorProp+'\\n\\nCHALLENGE - EVIDENCE:\\n\\n'+ev.text+
        '\\n\\nCHALLENGE - IMPLEMENTATION:\\n\\n'+im.text,3);
      turn.council={proposal:prop,evidence:ev,implementation:im,reconciled:rec};
      turn.answer=rec.text;turn.model=rec.model;
      turn.quotes=verifyQuotes(rec.text,ctxAll);
      // If every role landed on the same model the exchange is one model
      // talking to itself. Say so rather than let the format imply an
      // independence that was not there.
      const models=[prop.model,ev.model,im.model,rec.model];
      turn.councilSolo=models.every(m=>m===models[0]);
      turn.state='done';
      titleChat();saveChats();renderAsk();
      asking=false;return;
    }
    const ar=await fetch('/api/ask',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({mode:'answer',q:q,ctx:ctxAll,shape:askMode(),
        // prior turns, oldest first, so a follow-up resolves against the thread
        history:turns.slice(0,-1).filter(t=>t.state==='done')
                     .slice(-HIST_SEND).map(t=>({q:t.q,a:t.answer||''}))})});
    const aj=await ar.json();
    if(!ar.ok)throw new Error(aj.error||'answer failed');
    turn.answer=aj.answer;turn.model=aj.model||'';turn.state='done';
    turn.quotes=verifyQuotes(aj.answer,ctxAll);
    // A four-provider chain that degrades silently is one nobody can
    // reason about: the only symptom of falling back to a much smaller
    // model is that the writing gets worse. Record what actually answered.
    if(aj.tried&&aj.tried.length)console.info('[ask] fell back:',aj.tried);
    // cache the FIRST turn only, for the same reason it is only read there
    if(!turns.slice(0,-1).length){
      // Cache the SOURCE LIST, not uids. The answer's [n] are numbered over
      // ctxAll = passages -> picks -> outside, but this used to store picks
      // alone, so a replayed answer renumbered against a list missing the
      // leading passages: every citation pointed at the wrong paper, shifted
      // by up to FT_PASSAGES. The depth and external flags were lost too, so
      // specification-level quotes appeared attributed to abstracts.
      ANS[key]={answer:aj.answer,model:aj.model||'',t:Date.now(),
        sources:(turn.sources||[]).map(x=>({title:x.title,url:x.url,authors:x.authors,
          date:x.date,seen:x.seen,source:x.source,score:x.score,uid:x.uid,
          _depth:x._depth,_sec:x._sec,_external:x._external})),
        outside:(turn.outside||[]).slice(0,OUTSIDE_SHOW)};
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
// IMPLEMENT: one paper, read closely, turned into a specification.
//
// Retrieval here is the opposite of the Ask flow's. Ask casts wide and narrows;
// this reads deep into a SINGLE paper and deliberately fuses in nothing else.
// Cross-paper contamination is how the wrong lag structure ends up attributed
// to the wrong author, and that mistake is invisible in the output -- it reads
// exactly like the right answer.
const IMPL_PASSAGES=20;      // within one paper, not across the corpus
// Sections in the order this mode needs them. The method, the data and the
// results table are all required and are almost never adjacent, so taking the
// first N passages would reliably return the introduction three times.
const IMPL_SECTION_RANK=[
  [/method|model|estimat|specif|framework|approach|algorithm/i,0],
  [/data|sample|universe|variable/i,1],
  [/result|table|empirical|finding|evidence/i,2],
  [/robust|appendix|supplement/i,3],
];
function _implRank(sec){
  const s=String(sec||'');
  for(const [re,r] of IMPL_SECTION_RANK)if(re.test(s))return r;
  return 4;
}
// A passage carrying an equation is worth more here than one that does not:
// this mode's whole output is symbols and their timing.
function _hasMath(t){
  // Every backslash is DOUBLED because _INDEX is a non-raw Python string:
  // written once, Python turns a word-boundary escape into a backspace byte
  // (0x08) and ships a regex that parses fine and silently matches nothing.
  // tools/check_js.py fails the build on those bytes now -- this has happened
  // three times, and it caught the comment that used to be on this line too.
  return /[=<>]\\s*[-+(]?\\s*\\w|\\\\[a-zA-Z]+|\\bsum_|\\balpha\\b|\\bbeta\\b|_\\{|\\^\\{/.test(String(t||''));
}
// ---- add a PDF -------------------------------------------------------
// The reader has the paper; the archive does not. Rather than leaving the
// Implement button greyed with an explanation, take the PDF.
//
// IT IS PARSED IN THE BROWSER AND THE FILE IS NEVER UPLOADED. pdf.js runs
// locally, and only the extracted passages are sent. A paper someone has a
// licence to read stays on their machine; the archive gains the text it needs
// to quote it by section.
//
// This is weaker than GROBID, which is what tools/fulltext.py uses server-side:
// no reference parsing, no table extraction, headings recovered by shape rather
// than by understanding. The depth gate does not care -- what makes an
// Implement answer trustworthy is the verbatim-quote requirement and the "gaps
// may not be empty" contract, and both work on any faithful text.
const PDFJS_SRC='https://cdn.jsdelivr.net/npm/pdfjs-dist@4.4.168/build/pdf.min.mjs';
const PDFJS_WORKER='https://cdn.jsdelivr.net/npm/pdfjs-dist@4.4.168/build/pdf.worker.min.mjs';
const FT_UPLOAD_MAX=52000;      // must stay under FT_MAX_CHARS in ask.js
let _pdfjs=null;
function loadPdfJs(){
  if(_pdfjs)return _pdfjs;
  _pdfjs=import(PDFJS_SRC).then(m=>{
    m.GlobalWorkerOptions.workerSrc=PDFJS_WORKER;
    return m;
  });
  return _pdfjs;
}
// A heading, by shape rather than by meaning: numbered ("4.2 Robustness"), or
// a short line in title/upper case with no terminal full stop. Crude, and it
// only has to be good enough to label a passage -- a wrong label costs a
// citation that reads oddly, a wrong PASSAGE would cost the answer.
const HEAD_NUM=/^\\s*(\\d{1,2}(\\.\\d{1,2}){0,2})[.)]?\\s+(\\S.{0,70})$/;
const HEAD_WORDS=/^(abstract|introduction|related work|literature|data|methodology|methods?|model|estimation|results?|empirical|robustness|discussion|conclusions?|appendix|references)\\b/i;
function _looksHeading(line){
  const s=line.trim();
  if(s.length<3||s.length>80)return false;
  if(/[.;:,]$/.test(s))return false;
  if(HEAD_NUM.test(s))return true;
  if(HEAD_WORDS.test(s))return true;
  // ALL CAPS, few words -- common in older working papers
  return s.length<50&&s===s.toUpperCase()&&/[A-Z]{3}/.test(s)&&s.split(/\\s+/).length<=7;
}
// Join the per-item text pdf.js returns into lines, then lines into passages
// under the last heading seen. Paragraph breaks come from the y coordinate,
// because pdf.js gives position, not structure.
function _pagePassages(items){
  const lines=[]; let cur='', lastY=null;
  items.forEach(it=>{
    const y=(it.transform&&it.transform[5])||0;
    if(lastY!==null&&Math.abs(y-lastY)>3){ if(cur.trim())lines.push(cur.trim()); cur=''; }
    cur+=it.str+(it.hasEOL?' ':'');
    lastY=y;
  });
  if(cur.trim())lines.push(cur.trim());
  return lines;
}
async function parsePdf(file,onProgress){
  const pdfjs=await loadPdfJs();
  const buf=await file.arrayBuffer();
  return parsePdfDoc(await pdfjs.getDocument({data:buf}).promise,onProgress);
}
// Split from parsePdf so a document fetched from a URL and one chosen from
// disk go through exactly the same extraction -- an uploaded paper and an
// auto-fetched one must not become different kinds of record.
async function parsePdfDoc(doc,onProgress){
  const out=[]; let sec='', para='';
  const flush=()=>{
    const t=para.replace(/\\s+/g,' ').trim();
    // Below ~80 characters it is a caption, a page number or a stray line.
    if(t.length>=80)out.push({s:sec.slice(0,120),t:t});
    para='';
  };
  for(let p=1;p<=doc.numPages;p++){
    const page=await doc.getPage(p);
    const content=await page.getTextContent();
    _pagePassages(content.items).forEach(line=>{
      if(_looksHeading(line)){ flush(); sec=line; return; }
      para+=' '+line;
      if(para.length>1400)flush();          // keep passages citable, not chapters
    });
    flush();
    if(onProgress)onProgress(p,doc.numPages);
  }
  return out;
}
// References and appendices are the bulk of a long paper and the least useful
// part for an implementation, so they go first when trimming to fit. What
// Implement needs is method, data and results.
function _trimPassages(ps,budget){
  const rank=s=>{
    const x=String(s||'').toLowerCase();
    if(/refer|bibliograph/.test(x))return 4;
    if(/appendix|supplement/.test(x))return 3;
    if(/result|table|empirical|evidence/.test(x))return 1;
    if(/method|model|estimat|data|specif/.test(x))return 0;
    return 2;
  };
  const sorted=ps.map((p,i)=>({p:p,i:i,r:rank(p.s)}))
                 .sort((a,b)=>a.r-b.r||a.i-b.i);
  const keep=[]; let size=2;
  for(const it of sorted){
    const cost=JSON.stringify(it.p).length+1;
    if(size+cost>budget)continue;
    size+=cost; keep.push(it);
  }
  keep.sort((a,b)=>a.i-b.i);                 // restore reading order
  return keep.map(x=>x.p);
}
// ---- reading a PDF without leaving -----------------------------------
// Every PDF link used to be target="_blank". Leaving the portal to read a
// paper loses the tab, the filter and the place in a list 21,000 rows deep,
// and coming back means rebuilding all three by hand.
//
// TWO WAYS TO GET THE BYTES, and the reason is CORS, measured rather than
// assumed: arxiv.org sends `access-control-allow-origin: *`, so the page can
// fetch it directly and the proxy is not involved at all. nber.org sends no
// CORS header, and neither do most publisher hosts, so those go through
// /api/pdf -- which is host-allowlisted precisely because a Worker that
// fetches any URL it is handed is an open proxy.
//
// Direct first, proxy on failure: it keeps arXiv -- much of the archive --
// off the Worker entirely, and a fetch that CORS blocks fails immediately.
async function pdfBytes(url){
  try{
    const r=await fetch(url,{mode:'cors'});
    if(r.ok)return await r.arrayBuffer();
  }catch(e){ /* CORS or network: fall through to the proxy */ }
  const r2=await fetch('/api/pdf?url='+encodeURIComponent(url));
  if(!r2.ok){
    let msg='could not fetch that PDF';
    try{ msg=(await r2.json()).error||msg; }catch(e){}
    throw new Error(msg);
  }
  return await r2.arrayBuffer();
}

let PDF_VIEW=null;
async function openPdf(url,title){
  if(!url)return;
  // The canvases live in a DETACHED node that render() re-attaches. A filter
  // change elsewhere calls render(), which rewrites #view -- without this the
  // pages would vanish and never come back, because the render loop has
  // already finished by then.
  const host=document.createElement('div'); host.className='pdfpages';
  const v={url:url,title:title||'',state:'loading',err:'',pages:0,host:host};
  PDF_VIEW=v;
  setView('pdf'); render();
  try{
    const buf=await pdfBytes(url);
    const pdfjs=await loadPdfJs();
    const doc=await pdfjs.getDocument({data:buf}).promise;
    if(PDF_VIEW!==v)return;                       // navigated away while fetching
    v.state='ready'; v.pages=doc.numPages; render();
    // A page at a time: a 60-page paper is 60 canvases, and building them all
    // up front freezes the tab for several seconds.
    for(let p=1;p<=doc.numPages;p++){
      const page=await doc.getPage(p);
      if(PDF_VIEW!==v)return;
      const wide=(host.clientWidth||820)-24;
      const raw=page.getViewport({scale:1});
      const vp=page.getViewport({scale:Math.max(.5,Math.min(2,wide/raw.width))});
      const cv=document.createElement('canvas');
      cv.className='pdfpage'; cv.width=vp.width; cv.height=vp.height;
      host.appendChild(cv);
      await page.render({canvasContext:cv.getContext('2d'),viewport:vp}).promise;
    }
  }catch(e){
    if(PDF_VIEW!==v)return;
    v.state='error'; v.err=String(e.message||e); render();
  }
}
function renderPdf(){
  const v=PDF_VIEW;
  if(!v){$('view').innerHTML='<div class="empty">No PDF open.</div>';return;}
  $('view').innerHTML='<div class="dateline">'+esc(v.title||'PDF')
    +'<span class="n"> · '+(v.state==='ready'?(v.pages+' pages'):esc(v.state))+'</span>'
    +' <a class="pdfbtn" href="'+esc(v.url)+'" target="_blank" rel="noopener"'
    +' title="Open at the source">source</a></div>';
  if(v.state==='error'){
    $('view').innerHTML+='<div class="empty">'+esc(v.err)+'</div>';
    return;
  }
  if(v.state==='loading'){
    $('view').innerHTML+='<div class="empty">Fetching the PDF…</div>';
    return;
  }
  $('view').appendChild(v.host);
}

// The send half, shared by the file picker and the automatic fetch: a paper
// added by hand and one taken from a URL must land as the same record.
async function sendPassages(uid,it,ps,turn){
  if(ps.length<3||ps.reduce((a,b)=>a+b.t.split(' ').length,0)<400){
    throw new Error('this PDF yielded almost no text ('+ps.length+' passages). '
      +'It is probably a scan with no text layer, and archiving it would let '
      +'Implement claim a specification nobody can check.');
  }
  const before=ps.length;
  ps=_trimPassages(ps,FT_UPLOAD_MAX);
  turn.state='thinking';
  turn.npsg=ps.length; renderAsk();
  const r=await fetch('/api/ask',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({mode:'ingest_ft',uid:uid,title:(it&&it.title)||'',passages:ps})});
  const j=await r.json();
  if(!r.ok)throw new Error(j.error||'upload failed');
  turn.state='done';
  turn.answer='**Parsed in your browser and sent to the archive.**\\n\\n'
    +'- '+before+' passages extracted'
    +(ps.length<before?(', '+ps.length+' sent (references and appendices dropped first to fit)'):'')
    +'\\n- The PDF itself was never uploaded.\\n'
    +'\\nThe archive is being updated now. Once it finishes, this paper shows a '
    +'**full text** marker and **▸ implement** becomes available on its card. '
    +'That takes a few minutes and a redeploy.';
  // The uid is parsed NOW so the button stops offering an upload again in
  // this session, even though the deployed index has not caught up yet.
  if(FT_SET)FT_SET.add(uid);
}

// Papers whose automatic fetch already failed this session. Without this the
// button would retry the same dead URL forever instead of falling back.
const PDF_AUTO_FAILED={};

// A KNOWN PDF IS NOT SOMETHING TO ASK SOMEONE FOR. An NBER working paper, an
// arXiv preprint and every open-access record carry a derivable PDF location,
// so fetching it is strictly better than opening a file picker in front of
// someone who would have to go and download that exact file first.
async function implementFromPdf(uid,url){
  const it=ITEM_BY_UID[uid];
  setView('ask'); newChat(true);
  const chat=curChat(); const turns=chat?chat.turns:[];
  const label='Add PDF: '+((it&&it.title)||uid);
  const turn={q:label,state:'passages',ts:Date.now()};
  turns.push(turn); titleChat(); renderAsk();
  try{
    let hostname=url; try{hostname=new URL(url).hostname;}catch(e){}
    turn.q='Add PDF: fetching from '+hostname; renderAsk();
    const buf=await pdfBytes(url);
    const pdfjs=await loadPdfJs();
    const doc=await pdfjs.getDocument({data:buf}).promise;
    const ps=await parsePdfDoc(doc,(p,n)=>{
      turn.npsg=0; turn.q='Add PDF: parsing page '+p+' of '+n; renderAsk();
    });
    turn.q=label;
    await sendPassages(uid,it,ps,turn);
  }catch(e){
    // The picker stays reachable: an allowlist miss, a paywall or a stale
    // derivation is a reason to offer it, not to give up on the paper.
    turn.error=String(e.message||e)+' — press ▸ implement again to choose the file by hand.';
    turn.state='error';
    PDF_AUTO_FAILED[uid]=1;
  }
  saveChats(); renderAsk();
}

function addPdf(uid){
  const it=ITEM_BY_UID[uid];
  const url=(it&&!PDF_AUTO_FAILED[uid])?_pdfUrl(it):'';
  if(url)return implementFromPdf(uid,url);
  const inp=document.createElement('input');
  inp.type='file'; inp.accept='application/pdf,.pdf';
  inp.onchange=async ()=>{
    const f=inp.files&&inp.files[0];
    if(!f)return;
    setView('ask');
    newChat(true);
    const chat=curChat(); const turns=chat?chat.turns:[];
    const turn={q:'Add PDF: '+((it&&it.title)||uid),state:'passages',ts:Date.now()};
    turns.push(turn); titleChat(); renderAsk();
    try{
      const ps=await parsePdf(f,(p,n)=>{
        turn.npsg=0; turn.q='Add PDF: parsing page '+p+' of '+n; renderAsk();
      });
      turn.q='Add PDF: '+((it&&it.title)||uid);
      await sendPassages(uid,it,ps,turn);
    }catch(e){
      turn.error=String(e.message||e); turn.state='error';
    }
    saveChats(); renderAsk();
  };
  inp.click();
}

async function openImplement(uid){
  if(asking)return;
  setView('ask');
  loadIndex(async ()=>{
    await loadFtIndex();
    const it=ITEM_BY_UID[uid];
    if(!it){alert('That paper is not in the loaded archive.');return;}
    newChat(true);
    const chat=curChat();if(chat)chat.mode='analyse';   // implement is per-turn
    const turns=chat?chat.turns:[];
    const q='Implement: '+(it.title||uid);
    const turn={q:q,state:'passages',ts:Date.now()};
    turns.push(turn);titleChat();renderAsk();
    asking=true;
    try{
      const psg=await loadPassages([it]);
      if(!psg.length)throw new Error('no parsed passages for this paper — run tools/fulltext.py on it first');
      // rank by section priority, then by whether the passage carries maths,
      // then by position, so the ordering is deterministic and explicable
      psg.sort((a,b)=>_implRank(a.sec)-_implRank(b.sec)
        ||(_hasMath(b.text)?1:0)-(_hasMath(a.text)?1:0)||a.k-b.k);
      const ctx=psg.slice(0,IMPL_PASSAGES).map(p=>({
        uid:it.uid,title:it.title,authors:it.authors,url:it.url,date:it.date||it.seen,
        source:it.source,depth:'full',sec:p.sec,text:p.text}));
      turn.state='thinking';turn.npsg=ctx.length;renderAsk();
      const r=await fetch('/api/ask',{method:'POST',headers:{'content-type':'application/json'},
        body:JSON.stringify({mode:'answer',q:q,ctx:ctx,shape:'implement',target:uid})});
      const j=await r.json();
      if(!r.ok)throw new Error(j.error||'implement failed');
      turn.answer=j.answer;turn.model=j.model||'';turn.state='done';
      turn.refused=!!j.refused;
      // The Source column of the notation table and every line under
      // SPECIFICATION are supposed to be verbatim. This is where a fabricated
      // quote does the most damage, because it arrives wearing quotation marks
      // and a symbol definition, so it gets checked like any other answer.
      turn.quotes=verifyQuotes(j.answer,ctx);
      turn.sources=ctx.map(c=>Object.assign({},it,{_depth:'full',_sec:c.sec}));
    }catch(e){
      turn.error=String(e.message||e);turn.state='error';
    }
    asking=false;saveChats();renderAsk();
  });
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
// The reconciled view is the answer; the argument sits under it, collapsed.
// Worth keeping visible: "the identification objection was not answered" is
// usually more informative than the confident paragraph above it, and a reader
// who cannot see the challenges cannot tell scrutiny from ceremony.
function councilBlock(t,ti){
  const c=t.council;
  if(!c||!c.reconciled)return '';
  const part=(label,x)=>x&&x.text
    ? `<details class="cdet"><summary>${esc(label)}${x.model?` <span class="cm">${esc(x.model)}</span>`:''}</summary>
        <div class="cbody">${md(x.text,ti)}</div></details>` : '';
  const solo=t.councilSolo
    ? `<div class="csolo">Every role was answered by the same model \u2014 only one provider is
        configured, so these are not independent views.</div>` : '';
  return `<div class="council"><div class="chead">The argument</div>${solo}
    ${part('Position',c.proposal)}
    ${part('Challenge \u2014 evidence',c.evidence)}
    ${part('Challenge \u2014 implementation',c.implementation)}</div>`;
}
let _lastTurns=-1;
function renderAsk(notice){
  if(!VEC||!ARCHIVE_DATA){
    if(!notice){loadIndex(renderAsk);
      $('view').innerHTML='<div class="empty">Loading the semantic index\u2026</div>';return;}
  }
  if(!CHATS.length)newChat(true);
  const turns=curTurns();
  const note=(notice||indexWarning)?`<div class="askerr">${esc(notice||indexWarning)}</div>`:'';
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
    else if(t.state==='proposing')body='<div class="thinking">Opening the argument \u2014 stating a position from '+(t.sources||[]).length+' sources\u2026</div>';
    else if(t.state==='challenging')body='<div class="thinking">Two challenges in parallel \u2014 evidence, and whether it can be run\u2026</div>';
    else if(t.state==='reconciling')body='<div class="thinking">Reconciling \u2014 deciding what survives\u2026</div>';
    else if(t.state==='thinking')body='<div class="thinking">Synthesising from '+(t.sources||[]).length+' sources'+(t.viaGraph?' ('+t.viaGraph+' via the citation/similarity graph)':'')+((t.passages||[]).length?' ('+t.passages.length+' full-text passages)':'')+(t.extra?' ('+t.extra+' surfaced by the wider screen)':'')+(t.reused?' \u00b7 '+t.reused+' from memory':'')+'\u2026</div>';
    else if(t.state==='error')body='<div class="askerr">'+esc(t.error)+'</div>';
    else body='<div class="answer">'+md(t.answer,ti)+'</div>'
      +quoteBadge(t.quotes)
      +councilBlock(t,ti)
      +(t.model?'<div class="bywhom">answered by '+esc(t.model)+'</div>':'');
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
    <div class="askmode">
      <button data-mode2="analyse" class="${askMode()==='analyse'?'on':''}"
        title="Read the evidence and give a view">Analyse</button>
      <button data-mode2="build" class="${askMode()==='build'?'on':''}"
        title="Architecture, data plan, pseudocode, traps and how to validate it">Build</button>
      <button data-mode2="council" class="${askMode()==='council'?'on':''}"
        title="A position, challenged on evidence and on implementation, then reconciled \u2014 four model calls, slower">Council</button>
    </div>
    <div class="askbox"><textarea id="askq" rows="2" placeholder="${
      askMode()==='build'
        ? (turns.length?'Follow up \u2014 same build, or change one thing'
                       :'What are you building? \u2014 e.g. a multi-asset strategy using graph neural networks')
        : askMode()==='council'
        ? 'A question worth arguing about \u2014 e.g. does trend following still work post-2010?'
        : (turns.length?'Follow up \u2014 it remembers this conversation'
                       :'Ask anything about the archive \u2014 e.g. what does the evidence say about trend decay?')
    }"></textarea>
      <button id="asksend" ${asking?'disabled':''}>${asking?'\u2026':(askMode()==='build'?'Build':askMode()==='council'?'Convene':'Ask')}</button></div>
    <label class="outtog"><input type="checkbox" id="outtog" ${ASK_OUTSIDE?'checked':''}> Also search outside the archive (OpenAlex, arXiv)</label>`;
  const send=$('asksend');if(send)send.onclick=askSubmit;
  document.querySelectorAll('[data-mode2]').forEach(b=>b.onclick=()=>setAskMode(b.dataset.mode2));
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
  return `<div class="entry${x.url===SEL?' on':''}"${_rk(x)}><div class="rail">${cites}</div>
    <div class="body">
    <div class="cwrap"><a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>${cls}</div>
    <div class="meta">${x.authors?esc(x.authors):''} · ${esc(x.wp||'')}${_ftBtn(x)}${_pdfBtn({url:x.url})}${_implBtn(x)}${_mapBtn(x)}${_saveBtn({url:x.url,title:x.title,authors:x.authors})}</div>
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
// --- CFTC positioning (docs/cot.json, written by tools/cot.py) -------------
// Lazy, like nber.json: it is only needed on this one tab, and a briefing that
// blocks first paint on a market-data file is a worse briefing.
let COT=null, cotLoading=false, cotFailed=false;
const COT_OPEN={};              // group key -> showing all rows
const COT_TOP=6;                // rows per group before the disclosure
function loadCOT(cb){
  if(COT||cotFailed){cb();return;}
  if(cotLoading)return;
  cotLoading=true;
  fetch('cot.json').then(r=>r.json()).then(d=>{
    COT=d&&d.groups?d:null;cotLoading=false;
    if(!COT)cotFailed=true;
    if(VIEW==='foryou')cb();
  }).catch(()=>{
    // A missing or broken cot.json must degrade to papers-only, never to a
    // blank tab -- the papers are the part that is always there.
    cotLoading=false;cotFailed=true;if(VIEW==='foryou')cb();
  });
}
const cotNum=n=>(n<0?'\u2212':'+')+Math.abs(n).toLocaleString();
// Compact for the WoW column: a +116,517 next to a -1,243,004 is unreadable at
// a glance, and the change only needs an order of magnitude.
function cotK(n){
  const a=Math.abs(n);
  const t=a>=1e6?(a/1e6).toFixed(1)+'M':a>=1e4?Math.round(a/1e3)+'k':
          a>=1e3?(a/1e3).toFixed(1)+'k':String(a);
  return (n<0?'\u2212':'+')+t;
}
// Sign is carried by the minus sign and the arrow as well as by colour --
// colour alone fails for ~8% of men and in every printout.
function cotCell(n){
  return `<span class="${n<0?'sh':'lg'}">${cotNum(n)}</span>`;
}
function cotWow(n){
  if(n===null||n===undefined)return '<span style="color:var(--faint)">n/a</span>';
  if(n===0)return '<span style="color:var(--faint)">\u2013</span>';
  return `<span class="${n<0?'sh':'lg'}">${n<0?'\u25be':'\u25b4'} ${cotK(n)}</span>`;
}
function cotPct(p){
  if(p===null||p===undefined)
    return '<span class="pbar"><span class="v" style="width:auto">too short</span></span>';
  return `<span class="pbar"><span class="t"><i style="left:calc(${p}% - 1.5px)"></i></span>`+
         `<span class="v">p${p}</span></span>`;
}
function cotGroupHTML(g){
  const all=g.rows||[], open=!!COT_OPEN[g.key];
  const rows=open?all:all.slice(0,COT_TOP);
  // Drop only the TRAILING " - <exchange>" segment. Taking split(' - ')[0]
  // instead turns "DOMINION - SOUTH POINT - ICE FUTURES ENERGY DIV" into
  // "DOMINION", which is a different delivery point.
  const short=n=>{const p=String(n).split(' - ');
    return (p.length>1?p.slice(0,-1).join(' - '):n).trim();};
  const body=rows.map(r=>`<tr><td class="c">${esc(short(r.name))}</td>`+
    `<td class="n">${cotCell(r.net)}</td>`+
    `<td class="w">${cotWow(r.wow)}</td>`+
    `<td class="p">${cotPct(r.pct)}</td></tr>`).join('');
  const more=all.length>COT_TOP
    ? `<button class="cotmore" data-cotgrp="${esc(g.key)}">`+
      (open?'Show fewer':`${all.length-COT_TOP} more`)+'</button>'
    : '';
  return `<div class="cotgrp">${esc(g.label)}<span style="color:var(--faint)">`+
    `${all.length}</span></div>`+
    (g.note?`<div class="cotnote">${esc(g.note)}</div>`:'')+
    `<table class="cot">${body}</table>${more}`;
}
function cotPanelHTML(){
  if(cotFailed||!COT)
    return '<div class="empty">Positioning unavailable — cot.json did not load.</div>';
  if(!COT.groups.length)
    return '<div class="empty">No positioning data yet — run tools/cot.py.</div>';
  return (COT.stale?`<div class="cotstale">\u26a0 Latest CFTC report is ${esc(COT.as_of)} `+
           '— older than three weeks. Treat as historical, not current.</div>':'')+
    COT.groups.map(cotGroupHTML).join('');
}
// One delegated listener rather than one per button: the panel is re-rendered
// on every disclosure toggle, so per-node handlers would leak on each redraw.
document.addEventListener('click',e=>{
  const b=e.target.closest&&e.target.closest('.cotmore');
  if(!b)return;
  const k=b.getAttribute('data-cotgrp');
  COT_OPEN[k]=!COT_OPEN[k];
  renderForYou();
});
function renderForYou(){
  if(!COT&&!cotFailed){loadCOT(renderForYou);}
  const q=$('q').value.toLowerCase().trim();
  const cs=sinceDays(21);
  const pool=(cs?DATA.filter(x=>(x.seen||'')>=cs):DATA)
    .filter(x=>!q||(x.title+' '+x.authors+' '+x.source).toLowerCase().includes(q));
  // keyword-fit nudged by author reputation (same bounded multiplier as
  // everywhere else), so a strong-author match ranks a touch higher
  const matched=pool.map(x=>({...x,_fy:forYouScore(x)})).filter(x=>x._fy>=2.5)
    .sort((a,b)=>b._fy*(b.reputation||1)-a._fy*(a.reputation||1)||byDate(a,b));
  // Desk notes and papers are now SEPARATE BANDS, which is what retired the
  // reserved-slot arithmetic that used to live here: a raw keyword sum
  // structurally favours long academic abstracts over short practitioner
  // posts (more text, more hits), so the two had to be kept from competing
  // for one top-10. Splitting them structurally does the same job honestly.
  const mark=x=>({...x,_displayScore:Math.min(100,Math.round(x._fy*10)),
                  _displayLabel:'match'});
  const prac=matched.filter(isPrac).slice(0,8).map(mark);
  const acad=matched.filter(x=>!isPrac(x)).slice(0,14).map(mark);
  const byFy=(a,b)=>(b._fy||0)-(a._fy||0);
  const asof=COT&&COT.as_of?`week ending ${esc(COT.as_of)}`:'loading…';

  $('view').innerHTML=
    `<div class="dateline">For you <span class="n">· systematic macro / CTA \u2014 `+
      `positioning, desk notes and papers</span></div>`+
    `<div class="band">Positioning<span class="sub">CFTC Commitments of Traders `+
      `\u00b7 Leveraged Funds &amp; Managed Money net \u00b7 ${asof}</span></div>`+
    (cotLoading&&!COT?'<div class="empty">Loading positioning\u2026</div>':cotPanelHTML())+
    `<div class="band">Desk notes<span class="sub">practitioner &amp; house `+
      `research \u00b7 last 21 days \u00b7 ${prac.length}</span></div>`+
    (prac.length?byCategory(prac,byFy)
      :'<div class="empty">No desk notes matched in the last 3 weeks.</div>')+
    `<div class="band">Papers<span class="sub">journals &amp; preprints `+
      `\u00b7 last 21 days \u00b7 ${acad.length}</span></div>`+
    (acad.length?byCategory(acad,byFy)
      :'<div class="empty">No papers matched strongly in the last 3 weeks.</div>');
}
// Mailed in by the Claude digest and the host would not confirm the link
// (Macrosynergy answers 403 to every request). Every other record here came
// from an API or a feed, so the difference is worth showing rather than
// quietly presenting a generated item as established fact.
const _unver=x=>x.unverified?'<span class="unver" title="Mailed in by the Claude digest; the source site would not confirm this link, so it has not been verified">unverified</span>':'';
const _psource=x=>String(x.source||'').replace(/^journal:/,'').replace(/^topic:/,'').trim()||'Other';
// Archive's own source label. Kept separate from _psource because Archive holds
// the whole corpus -- journals, NEP, arXiv, SSRN -- and a topic-sweep source
// like "topic:asset allocation,topic:momentum" is a list, not a name.
const _asource=x=>{
  const s=String(x.source||'').split(',')[0].trim();
  if(s.startsWith('journal:'))return s.slice(8);
  if(s.startsWith('topic:')||s==='topic-sweep')return 'Topic sweep';
  return s||'Other';
};
// A PDF-availability filter, because "which of these can I actually read right
// now" is a different question from what any other control answers.
let PDF_ONLY=false;
function pracEntry(x){
  const sm=x.summary?`<div class="summary">${esc(x.summary)}</div>`:'';
  return `<div class="entry${x.url===SEL?' on':''}"${_rk(x)}><div class="rail"></div><div class="body">
    <a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>
    <div class="meta">${_unver(x)}${x.authors?esc(x.authors)+' · ':''}${esc(x.date||x.seen)}${_ftBtn(x)}${_pdfBtn(x)}${_implBtn(x)}${_mapBtn(x)}${_saveBtn(x)}</div>
    ${tagChips(x)}
    ${sm}</div></div>`;
}
// ===================== Build =========================================
// The rubric answers "is this paper good?". Build answers the question you
// actually have with your hands on a keyboard: I am putting together X -- what
// do these papers hand me, what data will I need, and what is going to bite me?
//
// No model call. tools/artifacts.py has already decomposed each paper into
// Methods, Factors, Datasets and a Thesis; this retrieves papers the same way
// Ask does (embed the question, nearest neighbours, then a graph hop for the
// adjacent work similarity alone misses) and REGROUPS their artifacts by kind.
// So it answers instantly, costs nothing, and cites a paper for every claim.
// Typed artifacts (tools/artifacts.py), fetched once and only when a Build
// answer actually needs them -- 1.9 MB has no business loading for a reader who
// only ever asks analytical questions.
let ARTS=null, artsLoading=null;
function loadArts(){
  if(ARTS)return Promise.resolve(ARTS);
  if(artsLoading)return artsLoading;          // concurrent callers share one fetch
  artsLoading=fetch('artifacts.json').then(r=>r.json())
    .then(a=>{ARTS=a||{};artsLoading=null;return ARTS;})
    // An absent or broken artifacts.json must not stop the answer: Build then
    // runs on the model's own knowledge and the papers' text, which is worse
    // but still useful, and the prompt already forbids inventing settings.
    .catch(()=>{ARTS={};artsLoading=null;return ARTS;});
  return artsLoading;
}
// Reads ARCHIVE_DATA, not DATA, and that is the whole point.
//
// data.json is windowed to PORTAL_RECENT_WINDOW_DAYS (60) on the paper's own
// date, and renderArchive() excludes practitioner items by design. So a
// practitioner post older than 60 days was reachable from NO tab: not from
// Archive, which filters it out, and not from here, which never saw it. That
// was a handful of posts until the Alpha Architect backfill made it ~2,500,
// and nothing looked broken because Ask reads archive.json + vec.bin directly
// and could still find them.
let pracPage=0,pracSrcFilled=false,archSrcFilled=false;
// The Archive dropdown is built from ARCHIVE_DATA, not DATA: a journal whose
// papers are all older than the 60-day window -- which after the backfill is
// most of them -- has no entry otherwise.
function _fillArchiveSources(){
  if(archSrcFilled||!ARCHIVE_DATA)return;
  archSrcFilled=true;
  const sel=$('asrc'), keep=sel.value;
  const counts={};
  ARCHIVE_DATA.filter(x=>!isPrac(x)).forEach(x=>{
    const k=_asource(x); counts[k]=(counts[k]||0)+1;
  });
  sel.innerHTML='';
  sel.add(new Option('All sources','all'));
  Object.keys(counts).sort((a,b)=>counts[b]-counts[a]||a.localeCompare(b))
    .forEach(k=>sel.add(new Option(k+' ('+counts[k]+')',k)));
  sel.value=[...sel.options].some(o=>o.value===keep)?keep:'all';
}
function renderPractitioners(){
  if(!ARCHIVE_DATA){loadArchive(renderPractitioners);return;}
  // The source dropdown was built from DATA at init, so a publisher whose
  // posts are all older than the 60-day window had no option at all. Rebuild
  // it from the archive the first time we get here, preserving the selection.
  _fillArchiveSources();
  if(!pracSrcFilled){
    pracSrcFilled=true;
    const sel=$('psrc'), keep=sel.value;
    sel.innerHTML='';
    sel.add(new Option('All sources','all'));
    [...new Set(ARCHIVE_DATA.filter(isPrac).map(_psource))].sort()
      .forEach(s=>sel.add(new Option(s,s)));
    sel.value=[...sel.options].some(o=>o.value===keep)?keep:'all';
  }
  const q=$('q').value.toLowerCase().trim();
  const src=$('psrc').value||'all';
  const rows=tagFilter(sleeveFilter(ARCHIVE_DATA)).filter(isPrac)
    .filter(x=>src==='all'||_psource(x)===src)
    .filter(x=>!q||(x.title+' '+x.authors+' '+x.source+' '+(x.summary||'')).toLowerCase().includes(q))
    .slice().sort(byDate);
  let h=`<div class="dateline">Practitioner &amp; house research <span class="n">· ${rows.length} posts · by source · latest first</span>${tagNote()}</div>`;
  if(!rows.length){$('view').innerHTML=h+'<div class="empty">No matches.</div>';return;}
  // Paginated, like Archive: this list is now thousands of entries deep, and
  // rendering every card at once is what made Archive itself feel slow.
  const shownCount=Math.min(rows.length,(pracPage+1)*ARCHIVE_PAGE_SIZE);
  const shown=rows.slice(0,shownCount);
  const remaining=rows.length-shownCount;
  const groups={}; shown.forEach(x=>{const s=_psource(x);(groups[s]=groups[s]||[]).push(x);});
  Object.keys(groups).sort().forEach(s=>{const a=groups[s];
    h+=`<div class="sechead t2">${esc(s)}<span class="cnt">${a.length}</span></div>`+a.map(pracEntry).join('');});
  if(remaining>0)h+=`<button class="loadmore" id="pracmore">Show ${Math.min(ARCHIVE_PAGE_SIZE,remaining)} more <span class="n">(${remaining} left)</span></button>`;
  $('view').innerHTML=h;
  if(remaining>0)$('pracmore').onclick=()=>{pracPage++;renderPractitioners();};
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
  _fillArchiveSources();
  const q=$('q').value.toLowerCase().trim();
  const t=$('topic').value||'all';
  const asrc=$('asrc').value||'all';
  let rows=tagFilter(sleeveFilter(ARCHIVE_DATA)).filter(x=>!isPrac(x)&&(t==='all'||((x.topic||'Other')===t)))
    .filter(x=>asrc==='all'||_asource(x)===asrc)
    .filter(x=>!PDF_ONLY||!!_pdfUrl(x))
    .filter(x=>!q||(x.title+' '+x.authors+' '+x.source+' '+(x.topic||'')).toLowerCase().includes(q))
    .slice().sort(byDate);
  const label=t==='all'?'All topics':t;
  // paginated -- rendering thousands of animated cards at once is what
  // actually made Archive itself feel slow, independent of fetch time
  const shownCount=Math.min(rows.length,(archivePage+1)*ARCHIVE_PAGE_SIZE);
  const shown=rows.slice(0,shownCount);
  const remaining=rows.length-shownCount;
  const more=remaining>0?`<button class="loadmore" id="archmore">Show ${Math.min(ARCHIVE_PAGE_SIZE,remaining)} more <span class="n">(${remaining} left)</span></button>`:'';
  $('view').innerHTML=`<div class="dateline">Archive · ${esc(label)} <span class="n">· ${rows.length} papers · date-wise</span>${tagNote()}</div>`+
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
  const meta=`<span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''}${x.date?' · '+esc(x.date):''}${x.cites!=null?' · '+fmtK(x.cites)+' cites':''}${x.consensus_n?' · '+x.consensus_n+'× '+(x.consensus_agree?'agree':'split'):''}${_ftBtn(x)}${_pdfBtn(x)}${_implBtn(x)}${_mapBtn(x)}${_saveBtn(x)}`;
  const cval=Math.round(x.composite);
  return `<div class="entry${x.url===SEL?' on':''}"${_rk(x)}><div class="rail">
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
  const tag=x.type?`<span class="ctag ${esc(String(x.type).toLowerCase())}">${esc(x.type)}</span>`:'';
  const cites=x.cites!=null?` · ${fmtK(x.cites)} cites`:'';
  const why=x.why||x.summary||'';
  return `<div class="entry${x.url===SEL?' on':''}"${_rk(x)}><div class="rail"><div class="yr">${esc(x.year||'')}</div></div>
    <div class="body">
      <div class="cwrap"><a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>${tag}</div>
      ${why?`<div class="summary">${esc(why)}</div>`:''}
      <div class="meta"><span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''}${cites}${_ftBtn(x)}${_pdfBtn(x)}${_implBtn(x)}${_mapBtn(x)}${_saveBtn(x)}</div>
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
    (rows.length?rows.map(x=>`<div class="entry classic${x.url===SEL?' on':''}"${_rk(x)}><div class="body">
      <div class="cwrap"><a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a><span class="cites">${fmtK(x.cites||0)}<small>cites</small></span></div>
      <div class="bar"><i style="width:${((x.cites||0)/max*100).toFixed(1)}%"></i></div>
      <div class="meta"><span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''} · ${esc(x.year||'')}${_ftBtn(x)}${_pdfBtn(x)}${_implBtn(x)}${_mapBtn(x)}${_saveBtn(x)}</div>
      ${x.summary?`<div class="summary">${esc(x.summary)}</div>`:''}</div></div>`).join('')
      :'<div class="empty">No history generated yet — run backfill.py.</div>');
}
function render(){if(VIEW.slice(0,3)==='sl:'){renderSleeve(VIEW.slice(3));return;}
  if(VIEW==="map"){renderMap();return;}
  if(VIEW==="pmap"){renderPaperMap();return;}
  if(VIEW==="pdf"){renderPdf();return;}
  VIEW==="monthly"?renderMonthly():VIEW==="ask"?renderAsk():VIEW==="foryou"?renderForYou():VIEW==="watched"?renderWatched():VIEW==="anchors"?renderAnchors():VIEW==="nber"?renderNBER():VIEW==="recent"?renderRecent():VIEW==="practitioners"?renderPractitioners():VIEW==="archive"?renderArchive():VIEW==="saved"?renderSaved():renderClassics();}
// Eleven flat tabs gave every destination the same weight and scrolled half of
// them off screen. They group by INTENT: read what's new, question the corpus,
// consult the standing reference, revisit your own picks. Ask and Saved are
// groups of one -- they are modes, not lists, so they get no sub-row.
const GROUPS={
  papers:['recent','foryou','watched','nber','monthly','practitioners','archive','map','pmap','pdf'],
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
  $('app').classList.toggle('wide',v==='map'||v==='ask'||v==='pmap'||v==='pdf');
  $('tagbar').hidden=!(SLEEVE_VIEWS.indexOf(v)>=0||v.slice(0,3)==='sl:');
  if(!$('tagbar').hidden)renderTagbar();
  $('psrc').style.display=v==="practitioners"?'':'none';
  const arch=(v==="archive"||v.slice(0,3)==='sl:');
  $('asrc').style.display=arch?'':'none';
  $('pdfonly').style.display=arch?'':'none';
  if(v==="archive"||v.slice(0,3)==='sl:')archivePage=0;
  if(v==="practitioners"||v.slice(0,3)==='sl:')pracPage=0;
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
$('t-map').onclick=()=>setView('map');
$('t-saved').onclick=()=>setView('saved');
$('q').addEventListener('input',()=>{archivePage=0;pracPage=0;render();});
$('month').addEventListener('change',render);
$('nbermonth').addEventListener('change',render);
$('cat').addEventListener('change',render);
$('topic').addEventListener('change',()=>{archivePage=0;pracPage=0;render();});
$('psrc').addEventListener('change',()=>{pracPage=0;render();});
$('asrc').addEventListener('change',()=>{archivePage=0;render();});
$('pdfonly').addEventListener('click',()=>{
  PDF_ONLY=!PDF_ONLY;
  $('pdfonly').classList.toggle('on',PDF_ONLY);
  archivePage=0;render();
});
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
  // Browser-held state must load BEFORE anything can save it. This ran inside
  // loadArchive(), which Ask never calls -- so opening Ask first left CHATS
  // empty, and the first saveChats() overwrote every stored conversation with
  // the new one. Same for PINS and the ingest queue.
  PINS=loadPins();rebuildViews();renderPinTabs();
  loadChats();
  STATE_LOADED=true;
  // setView, not render: render() alone never clears the tag rail's `hidden`
  // attribute, so the sleeve tags stayed invisible until you switched tabs.
  setView(VIEW);
});

// ---------------------------------------------------------- Detail pane
// The card used to carry the summary and all six rubric bars, on every row.
// One selected paper shows them instead, in a column with room to read.
function showDetail(url){
  const d=$('detail');
  const x=ROWMAP[url]||ITEM_BY_URL[url];
  if(!x){d.innerHTML='<div class="dempty">Select a paper.</div>';return;}
  // exported papers, monthly picks, classics and anchors name their fields
  // differently; one shape from here on
  const n={title:x.title||x.t||'(untitled)',
    authors:x.authors||x.by||'',
    src:jlabel(x)||x.journal||x.ul||'',
    date:x.date||x.seen||(x.year!=null?String(x.year):(x.yr!=null?String(x.yr):'')),
    topic:x.topic||'',
    summary:x.summary||x.why||''};
  const dv=x._displayScore!=null?x._displayScore:x.score;
  const lvl=v=>v==null?'\u2013':v+'/3';
  const prov=x.contribution_provisional?' (prov.)':'';
  const ctype=(x.novelty_type&&x.novelty_type!=='none')?' ('+esc(x.novelty_type)+')':'';
  const hasScores=(x.generality!=null||x.contribution!=null||x.testability!=null||x.novelty_posterior!=null);
  const label=x._displayLabel||'rating';
  const rub=hasScores?'<div class="drubh"><span>Rubric</span><em>'+
      (dv!=null?dv+' / 100 '+esc(label):'not scored')+'</em></div><div class="drub">'+[
    _subBar('Relevance (vs history)',x.relevance_posterior!=null?Math.round(x.relevance_posterior*100)+'%':'\u2013',(x.relevance_posterior||0)*100),
    _subBar('Generality',lvl(x.generality),(x.generality||0)/3*100),
    _subBar('Contribution'+ctype,lvl(x.contribution)+prov,(x.contribution||0)/3*100),
    _subBar('Testability',lvl(x.testability),(x.testability||0)/3*100),
    _subBar('Novelty vs history',x.novelty_posterior!=null?Math.round(x.novelty_posterior*100)+'%':'\u2013',(x.novelty_posterior||0)*100),
    (x.author_score!=null?_subBar('Author',Math.round(x.author_score),x.author_score):''),
  ].join('')+'</div><div class="dnote">'+
    (x.consensus_n?x.consensus_n+' models \u00b7 '+(x.consensus_agree?'agree':'split'):'single pass \u00b7 no consensus check')+
    '</div>':(isPrac(x)?'<div class="dnote" style="margin-top:24px">Practitioner post \u2014 listed as-is, not scored.</div>':'');
  const sl=(x.sleeves||[]).filter(k=>k!=='other');
  const chips=sl.length?'<div class="sleeves" style="margin-top:18px">'+sl.map(k=>
    '<span class="sl" data-sleeve="'+k+'">'+esc(SLEEVE_LABEL[k]||k)+'</span>').join('')+'</div>':'';
  const kick=[n.src,n.date,n.topic].filter(Boolean).map(esc).join(' \u00b7 ');
  d.innerHTML='<div class="dwrap">'+
    (kick?'<div class="dkick">'+kick+'</div>':'')+
    '<h1 class="dtitle">'+esc(n.title)+'</h1>'+
    '<div class="dauth">'+esc(n.authors||'Unattributed')+'</div>'+
    '<div class="dacts"><a class="dbtn prim" href="'+esc(x.url)+'" target="_blank" rel="noopener">Open paper</a>'+
      _pdfBtn(x)+_implBtn(x)+_mapBtn(x)+_saveBtn(x)+'</div>'+
    chips+
    (x.cites!=null?'<div class="dkick" style="margin-top:22px;font-size:24px;letter-spacing:-.01em;'+
      'text-transform:none;color:var(--cite);font-family:var(--serif);font-weight:600">'+fmtK(x.cites)+
      ' <span style="font-family:var(--sans);font-size:9.5px;letter-spacing:.13em;text-transform:uppercase;'+
      'color:var(--faint)">citations</span></div>':'')+
    (n.summary?'<p class="dsum">'+esc(n.summary)+'</p>':'')+
    (x.pdf?'<div class="dnote" style="margin-top:18px">Free copy: <a href="'+esc(x.pdf)+
      '" target="_blank" rel="noopener" style="color:var(--accent);font-weight:600;text-decoration:underline">\u2b73 '+
      esc(x.pl||'PDF')+'</a></div>':'')+
    rub+'</div>';
  d.scrollTop=0;
}
$('detail').addEventListener('click',e=>{
  const c=e.target.closest('.sl[data-sleeve]');
  if(c){setSleeve(c.dataset.sleeve);return;}
  const m=e.target.closest('[data-pmap]');
  if(m){e.preventDefault();openPaperMap(m.dataset.pmap);return;}
  const im=e.target.closest('[data-impl]');
  if(im){e.preventDefault();openImplement(im.dataset.impl);}
  const ap=e.target.closest('[data-addpdf]');
  if(ap){e.preventDefault();e.stopPropagation();addPdf(ap.dataset.addpdf);return;}
  const pv=e.target.closest('[data-pdf]');
  // A modified click is a deliberate "open this elsewhere" and stays the
  // browser's to handle; a plain click reads it here.
  if(pv&&!(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)){
    e.preventDefault();e.stopPropagation();
    openPdf(pv.dataset.pdf,pv.dataset.pdftitle);return;
  }
  const tg=e.target.closest('[data-tag]');
  if(tg){e.preventDefault();e.stopPropagation();setTag(tg.dataset.tag);}
});

// ---------------------------------------------------------- Keyboard
// Read daily, so the list should move under the hands: j/k down the feed,
// / to search, s to save, p for the PDF, n for the neighbourhood.
const KEYS=[['j / k','Next / previous paper'],['/','Search titles and authors'],
  ['\u2318K','Search'],['s','Save or unsave the selected paper'],
  ['p','Open its PDF'],['n','Show its neighbourhood'],
  ['[ / ]','Previous / next view'],['Esc','Close this panel'],['?','This panel']];
function toggleHelp(force){
  const w=$('helpwrap');
  const show=force!==undefined?force:w.hidden;
  if(show&&!w.innerHTML){
    w.innerHTML='<div class="helpcard"><div class="helphead"><b>Keyboard</b><span>Esc to close</span></div>'+
      KEYS.map(([k,t])=>'<div class="helprow"><kbd>'+esc(k)+'</kbd><span>'+esc(t)+'</span></div>').join('')+'</div>';
    w.onclick=ev=>{if(!ev.target.closest('.helpcard'))toggleHelp(false);};
  }
  w.hidden=!show;
}
function _rows(){return Array.from(document.querySelectorAll('.entry[data-key]'));}
function _selectRow(el){
  if(!el)return;
  SEL=el.dataset.key;
  document.querySelectorAll('.entry.on').forEach(n=>n.classList.remove('on'));
  el.classList.add('on');
  showDetail(SEL);
  const r=el.getBoundingClientRect(), c=$('listcol').getBoundingClientRect();
  if(r.top<c.top+60)$('listcol').scrollTop-=(c.top+60-r.top);
  else if(r.bottom>c.bottom-20)$('listcol').scrollTop+=(r.bottom-c.bottom+20);
}
function _moveSel(d){
  const rows=_rows();
  if(!rows.length)return;
  const i=rows.findIndex(el=>el.dataset.key===SEL);
  _selectRow(rows[Math.min(rows.length-1,Math.max(0,(i<0?(d>0?-1:0):i)+d))]);
}
function _cycleView(d){
  const g=GROUPS[GROUP_OF[VIEW]]||[];
  const vs=g.filter(v=>{const b=$('t-'+v);return b&&!b.hidden;});
  const i=vs.indexOf(VIEW);
  if(i<0||!vs.length)return;
  setView(vs[(i+d+vs.length)%vs.length]);
}
addEventListener('keydown',e=>{
  const t=e.target;
  const typing=t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable);
  if(e.key==='Escape'){if(typing&&t.blur)t.blur();toggleHelp(false);return;}
  if(typing)return;
  if(e.metaKey||e.ctrlKey){
    if(e.key.toLowerCase()==='k'){e.preventDefault();$('q').focus();$('q').select();}
    return;
  }
  switch(e.key){
    case 'j': case 'ArrowDown': e.preventDefault();_moveSel(1);break;
    case 'k': case 'ArrowUp': e.preventDefault();_moveSel(-1);break;
    case '/': e.preventDefault();$('q').focus();$('q').select();break;
    case '?': e.preventDefault();toggleHelp();break;
    case 's': {const b=document.querySelector('.entry.on .savebtn');if(b)b.click();break;}
    case 'p': {const a=document.querySelector('.entry.on .pdfbtn');
      if(a)openPdf(a.dataset.pdf,a.dataset.pdftitle);break;}
    case 'n': {const b=document.querySelector('.entry.on [data-pmap]');if(b)openPaperMap(b.dataset.pmap);break;}
    case '[': _cycleView(-1);break;
    case ']': _cycleView(1);break;
    default: break;
  }
});
</script>
</body>
</html>
"""
