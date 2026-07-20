"""Generate the static portal (docs/) from the SQLite archive.

Exports every archived item to docs/data.json and writes a self-contained
"research journal" browser (docs/index.html): three pages -- Recent (last 7
days), Monthly (grouped by calendar month, Jul 2026 onward), and Classics (the
all-time most-cited finance papers, docs/classics.json, produced by backfill.py).
Each page groups entries by source category (Academic T1 / T2 / Preprints /
Practitioner) latest-first. Self-hosted Newsreader serif in docs/fonts/.
"""

import json
import pathlib

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
            "generality": (m.get("generality") or {}).get("level"),
            "contribution": (m.get("contribution") or {}).get("level"),
            "contribution_provisional": (m.get("contribution") or {}).get("provisional", True),
            "testability": (m.get("testability") or {}).get("level"),
            "novelty_type": m.get("novelty_type"),
            "novelty_posterior": m.get("novelty_posterior"),
            "consensus_n": m.get("consensus_n"),
            "consensus_agree": m.get("consensus_agree"),
            "topic": m.get("topic", ""),
            # practitioners aren't LLM-summarised -> fall back to their RSS blurb
            "summary": m.get("summary") or m.get("why") or (m.get("abstract") or "")[:400],
        })
    out.sort(key=lambda x: (x["seen"] or "", x["date"] or ""), reverse=True)
    return out


def build(con) -> int:
    data = _export(con)
    docs = pathlib.Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "data.json").write_text(json.dumps(data, default=str), encoding="utf-8")
    if not (docs / "classics.json").exists():      # placeholder until backfill runs
        (docs / "classics.json").write_text("[]", encoding="utf-8")
    if not (docs / "monthly.json").exists():        # placeholder until monthly runs
        (docs / "monthly.json").write_text("{}", encoding="utf-8")
    (docs / "index.html").write_text(_INDEX, encoding="utf-8")
    return len(data)


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
.pdfbtn{font-family:var(--sans);font-size:10px;font-weight:700;letter-spacing:.04em;
  color:var(--accent);border:1px solid var(--accent);border-radius:4px;padding:1px 5px;
  margin-left:8px;text-decoration:none;vertical-align:1px;display:inline-block;
  transition:background .15s,color .15s,transform .15s;}
.pdfbtn:hover{background:var(--accent);color:var(--panel);transform:translateY(-1px);}
footer{font-family:var(--sans);font-size:11px;line-height:1.6;color:var(--faint);
  margin-top:40px;border-top:1px solid var(--line);padding-top:16px;}
@keyframes entryIn{from{opacity:0;transform:translateY(7px);}to{opacity:1;transform:translateY(0);}}
@keyframes growBar{from{transform:scaleX(0);}to{transform:scaleX(1);}}
.entry,.dateline,.sechead{animation:entryIn .3s cubic-bezier(.2,.7,.2,1) both;}
@media (prefers-reduced-motion:reduce){
  *{animation-duration:.001ms!important;animation-iteration-count:1!important;
    transition-duration:.001ms!important;scroll-behavior:auto!important;}
}
</style>
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
    <div class="navtabs">
      <button id="t-recent" class="on">Recent</button>
      <button id="t-foryou">For You</button>
      <button id="t-monthly">Monthly</button>
      <button id="t-classics">Classics</button>
      <button id="t-practitioners">Practitioners</button>
      <button id="t-archive">Archive</button>
      <button id="t-saved">Saved</button>
    </div>
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
      <span class="sp"></span>
      <span class="searchwrap"><input id="q" type="search" placeholder="Search…" autocomplete="off"></span>
    </div>
  </div>
</header>
<main class="wrap"><div id="view"></div>
  <footer>Rubric scores are an ensemble consensus of multiple LLMs (Groq · Mistral ·
    OpenAI) — skim, don't trust blindly. Practitioner posts are listed as-is, not scored.
    Sources: NEP · NBER · arXiv · finance journals &amp; SSRN via Crossref · PM-Research ·
    OpenAlex · practitioner &amp; asset-manager research.
  </footer>
</main>
<script>
let DATA=[], CLASSICS=[], MONTHLY={}, VIEW="recent", MAXSEEN="";
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
  return null;
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
  const rows=Object.values(SAVED)
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
  const sc=(dv!=null)?`<div class="rail">${rk}<div class="gauge" style="--pct:${dv};--gc:${bandColor(dv)}"><span>${dv}</span></div><div class="cap">${cap}</div></div>`:`<div class="rail">${rk}</div>`;
  const sm=x.summary?`<div class="summary">${esc(x.summary)}</div>`:'';
  const who=x.authors?' · '+esc(x.authors):'';
  const lvl=v=>v==null?'–':v+'/3';
  const prov=x.contribution_provisional?' <span style="opacity:.55">(prov.)</span>':'';
  const ctype=(x.novelty_type&&x.novelty_type!=='none')?' ('+esc(x.novelty_type)+')':'';
  const hasScores=(x.generality!=null||x.contribution!=null||x.testability!=null||x.novelty_posterior!=null);
  const subs=hasScores?'<div class="subs">'+[
    _subBar('Relevance',lvl(x.relevance),(x.relevance||0)/3*100),
    _subBar('Generality',lvl(x.generality),(x.generality||0)/3*100),
    _subBar('Contribution'+ctype,lvl(x.contribution)+prov,(x.contribution||0)/3*100),
    _subBar('Testability',lvl(x.testability),(x.testability||0)/3*100),
    _subBar('Novelty vs history',x.novelty_posterior!=null?Math.round(x.novelty_posterior*100)+'%':'–',(x.novelty_posterior||0)*100),
  ].join('')+'</div>':'';
  return `<div class="entry">${sc}<div class="body">
    <a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>
    <div class="meta"><span class="j">${esc(jlabel(x))}</span>${who} · ${esc(x.date||x.seen)}${x.topic?' · '+esc(x.topic):''}${x.consensus_n?' · '+x.consensus_n+'× '+(x.consensus_agree?'agree':'split'):''}${_pdfBtn(x)}${_saveBtn(x)}</div>
    ${sm}${subs}</div></div>`;
}
function grouped(rows){
  const q=$('q').value.toLowerCase().trim();
  const cf=$('cat').value;
  rows=rows.filter(x=>!q||(x.title+' '+x.authors+' '+x.source).toLowerCase().includes(q));
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
// Recent's bar requires relevance at ceiling (score===100) as a *gate*, so
// every qualified item would show the same "100" -- not a differentiator.
// Blend the dimensions that still vary among qualified items (generality,
// testability, novelty-vs-history) into a display-only strength score.
function _strengthScore(x){
  const g=x.generality||0,t=x.testability||0,np=x.novelty_posterior||0;
  return Math.max(0,Math.min(100,Math.round((g+t)/6*50+np*50)));
}
function renderRecent(){
  const cs=sinceDays(7);
  const pool=(cs?DATA.filter(x=>(x.seen||'')>=cs):DATA).filter(x=>x.score!=null&&!isPrac(x));
  // absolute quality bar, no percentage cut: relevance at ceiling AND a
  // genuinely novel, non-provisional contribution -- then cap to the top 10
  // by strength so a busy week doesn't dump dozens of "good enough" papers
  const qualified=pool.filter(x=>x.score===100&&x.contribution===3&&!x.contribution_provisional);
  const rows=qualified.slice().sort((a,b)=>
    (b.novelty_posterior||0)-(a.novelty_posterior||0)||(b.generality||0)-(a.generality||0)||byDate(a,b)
  ).slice(0,10).map(x=>({...x,_displayScore:_strengthScore(x),_displayLabel:'strength'}));
  $('view').innerHTML=`<div class="dateline">Last 7 days · genuinely strong <span class="n">· top ${rows.length} of ${qualified.length} that cleared the bar — the rest are in Archive</span></div>`+grouped(rows);
}
const FORYOU_KEYWORDS=[
  ['trend following',3],['trend premi',2],[' cta ',2],['ewma',2],
  ['commodit',3],['multi asset',2.5],['regime',2],
  ['tactical asset allocation',2],['quality factor',2.5],['factor timing',2.5],
  ['factor tilt',2],['tilting',1.5],['equity factor',1],
  ['profitability',2],['growth signal',1.5],['sector rotation',2],
  ['cross sectional',1.5],['cross section of',1.5],['bayesian',2.5],
  ['kalman filter',3],['state space',2.5],
  ['gibbs sampling',3],['dynamic model averaging',3],['hedge fund replication',3],
  ['gold',1],['oil price',2],['oil return',2],['stock bond correlation',2.5],
  ['bond equity correlation',2.5],['equity bond correlation',2.5],
  ['volatility targeting',2],['target volatility',2],['transaction cost',1],
  ['turnover',0.5],['systematic trading',1.5],['systematic alpha',1.5],
  ['regularized regression',1],['regularised regression',1],['carry trade',1.5],
  ['carry factor',1.5],['basis momentum',2],['skewness',1],['econometrics',0.5],
  ['asset pricing',0.8],['portfolio construction',1],['macro',0.6],
  ['time series forecasting',1],['factor investing',1.5],['momentum',1.2],
  ['futures',0.8],['systematic macro',2],['risk premia',1.2],
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
  const matched=pool.map(x=>({...x,_fy:forYouScore(x)})).filter(x=>x._fy>=2.5)
    .sort((a,b)=>b._fy-a._fy||byDate(a,b));
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
  $('view').innerHTML=`<div class="dateline">For you <span class="n">· matched to trend/CTA, commodities, macro regime, factor models, Bayesian state-space, hedge-fund replication · top ${rows.length}, across journals/preprints/practitioner</span></div>`+
    (rows.length?byCategory(rows,(a,b)=>(b._fy||0)-(a._fy||0)):'<div class="empty">Nothing matched strongly in the last 3 weeks.</div>');
}
const _psource=x=>String(x.source||'').replace(/^journal:/,'').replace(/^topic:/,'').trim()||'Other';
function pracEntry(x){
  const sm=x.summary?`<div class="summary">${esc(x.summary)}</div>`:'';
  return `<div class="entry"><div class="rail"></div><div class="body">
    <a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>
    <div class="meta">${x.authors?esc(x.authors)+' · ':''}${esc(x.date||x.seen)}${_pdfBtn(x)}${_saveBtn(x)}</div>
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
function renderArchive(){
  const q=$('q').value.toLowerCase().trim();
  const t=$('topic').value||'all';
  let rows=DATA.filter(x=>!isPrac(x)&&(t==='all'||((x.topic||'Other')===t)))
    .filter(x=>!q||(x.title+' '+x.authors+' '+x.source+' '+(x.topic||'')).toLowerCase().includes(q))
    .slice().sort(byDate);
  const label=t==='all'?'All topics':t;
  $('view').innerHTML=`<div class="dateline">Archive · ${esc(label)} <span class="n">· ${rows.length} papers · date-wise</span></div>`+
    (rows.length?rows.map(x=>entry(x)).join(''):'<div class="empty">No matches.</div>');
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
  ].join('');
  const meta=`<span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''}${x.date?' · '+esc(x.date):''}${x.cites!=null?' · '+fmtK(x.cites)+' cites':''}${x.consensus_n?' · '+x.consensus_n+'× '+(x.consensus_agree?'agree':'split'):''}${_pdfBtn(x)}${_saveBtn(x)}`;
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
      <div class="meta"><span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''}${cites}${_pdfBtn(x)}${_saveBtn(x)}</div>
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
      <div class="meta"><span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''} · ${esc(x.year||'')}${_pdfBtn(x)}${_saveBtn(x)}</div>
      ${x.summary?`<div class="summary">${esc(x.summary)}</div>`:''}</div></div>`).join('')
      :'<div class="empty">No history generated yet — run backfill.py.</div>');
}
function render(){VIEW==="monthly"?renderMonthly():VIEW==="foryou"?renderForYou():VIEW==="recent"?renderRecent():VIEW==="practitioners"?renderPractitioners():VIEW==="archive"?renderArchive():VIEW==="saved"?renderSaved():renderClassics();}
function setView(v){
  VIEW=v;['recent','foryou','monthly','classics','practitioners','archive','saved'].forEach(k=>$('t-'+k).classList.toggle('on',k===v));
  $('month').style.display=v==="monthly"?'':'none';
  $('jsel').style.display=v==="classics"?'':'none';
  $('cat').style.display=v==="recent"?'':'none';
  $('topic').style.display=v==="archive"?'':'none';
  $('psrc').style.display=v==="practitioners"?'':'none';render();
}
$('t-recent').onclick=()=>setView('recent');
$('t-foryou').onclick=()=>setView('foryou');
$('t-monthly').onclick=()=>setView('monthly');
$('t-classics').onclick=()=>setView('classics');
$('t-practitioners').onclick=()=>setView('practitioners');
$('t-archive').onclick=()=>setView('archive');
$('t-saved').onclick=()=>setView('saved');
$('q').addEventListener('input',render);
$('month').addEventListener('change',render);
$('cat').addEventListener('change',render);
$('topic').addEventListener('change',render);
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
]).then(([d,c,mo])=>{
  DATA=d;CLASSICS=c;MONTHLY=mo||{};
  MAXSEEN=d.reduce((m,x)=>(x.seen||'')>m?(x.seen||''):m,"");
  Object.keys(MONTHLY).filter(k=>/^\\d{4}-\\d{2}$/.test(k)).sort().reverse()
    .forEach(m=>$('month').add(new Option(new Date(m+"-01").toLocaleString('en',{month:'long',year:'numeric'}),m)));
  $('topic').add(new Option('All topics','all'));
  [...new Set(d.filter(x=>!isPrac(x)).map(x=>x.topic||'Other'))].sort()
    .forEach(t=>$('topic').add(new Option(t,t)));
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
