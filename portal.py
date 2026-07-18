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


def _export(con) -> list[dict]:
    rows = con.execute(
        "SELECT uid, title, source, section, url, meta, first_seen FROM items"
    ).fetchall()
    out = []
    for uid, title, source, section, url, meta, first_seen in rows:
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
            "summary": m.get("summary") or m.get("why", ""),
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
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--serif);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;}
a{color:inherit;text-decoration:none;}
.wrap{max-width:768px;margin:0 auto;padding:0 22px 72px;}
header{position:sticky;top:0;z-index:9;background:var(--ground);border-bottom:1px solid var(--line);}
.mast{max-width:768px;margin:0 auto;padding:18px 22px 0;}
.brandrow{display:flex;align-items:baseline;justify-content:space-between;gap:14px;}
.brand{font-weight:600;font-size:26px;letter-spacing:-.01em;line-height:1;}
.brand .the{font-style:italic;font-weight:400;color:var(--muted);}
.tagline{font-family:var(--sans);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--faint);margin-top:6px;}
.toggle{font-family:var(--sans);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);background:none;border:1px solid var(--line);border-radius:20px;
  padding:5px 11px;cursor:pointer;}
.toggle:hover{border-color:var(--accent);color:var(--accent);}
.doublerule{height:3px;border-top:1px solid var(--ink);border-bottom:1px solid var(--ink);margin:14px 0 0;}
.nav{display:flex;gap:26px;align-items:center;padding:11px 0 0;flex-wrap:wrap;}
.nav button{font-family:var(--serif);font-size:16px;color:var(--muted);background:none;
  border:0;padding:0 0 12px;cursor:pointer;position:relative;}
.nav button.on{color:var(--ink);}
.nav button.on:after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:2px;background:var(--accent);}
.nav .sp{flex:1;}
#q{font-family:var(--sans);font-size:13px;color:var(--ink);background:var(--panel);
  border:1px solid var(--line);border-radius:7px;padding:6px 10px;width:190px;max-width:42vw;}
#q:focus{outline:2px solid var(--accent);outline-offset:1px;}
#month,#cat,#jsel{font-family:var(--serif);font-size:15px;color:var(--ink);background:var(--panel);
  border:1px solid var(--line);border-radius:7px;padding:5px 9px;cursor:pointer;max-width:44vw;}
#cat:focus,#month:focus,#jsel:focus{outline:2px solid var(--accent);outline-offset:1px;}
.dateline{font-family:var(--sans);font-size:12px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);margin:26px 0 2px;display:flex;align-items:center;gap:10px;}
.dateline .n{font-variant-numeric:tabular-nums;color:var(--faint);}
.sechead{font-size:19px;font-weight:600;margin:26px 0 2px;padding-bottom:7px;
  border-bottom:1px solid var(--ink);display:flex;justify-content:space-between;align-items:baseline;}
.sechead .cnt{font-family:var(--sans);font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--faint);font-weight:400;}
.sechead.t2{color:var(--muted);border-bottom-color:var(--line);}
.entry{display:grid;grid-template-columns:44px 1fr;gap:14px;padding:16px 0;border-bottom:1px solid var(--line2);}
.rail{text-align:right;padding-top:2px;}
.rail .rank{font-family:var(--serif);font-size:15px;font-weight:600;line-height:1;color:var(--accent);
  font-variant-numeric:tabular-nums;padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid var(--line);}
.score{font-size:23px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1;color:var(--score,var(--ink));}
.rail .ratebar{height:3px;width:26px;margin:5px 0 0 auto;border-radius:2px;background:var(--score,var(--line));}
.rail .cap{font-family:var(--sans);font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);margin-top:4px;}
.title{font-size:17px;font-weight:600;line-height:1.28;text-wrap:balance;}
.title:hover{color:var(--accent);text-decoration:underline;text-underline-offset:2px;}
.meta{font-family:var(--sans);font-size:11.5px;letter-spacing:.02em;color:var(--muted);margin-top:4px;}
.meta .j{color:var(--ink);font-weight:500;}
.summary{font-size:15px;line-height:1.5;color:var(--ink);margin-top:7px;max-width:63ch;}
.empty{color:var(--muted);font-style:italic;padding:40px 0;text-align:center;}
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
.sub s{height:2px;width:100%;background:var(--line);border-radius:2px;display:block;text-decoration:none;overflow:hidden;}
.sub s u{display:block;height:100%;background:var(--accent);opacity:.6;}
footer{font-family:var(--sans);font-size:11px;line-height:1.6;color:var(--faint);
  margin-top:40px;border-top:1px solid var(--line);padding-top:16px;}
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
    <div class="nav">
      <button id="t-monthly" class="on">Monthly</button>
      <button id="t-recent">Recent</button>
      <button id="t-classics">Classics</button>
      <span class="sp"></span>
      <select id="cat" title="Category" style="display:none">
        <option value="all">All categories</option>
        <option value="0">Academic · Tier 1</option>
        <option value="1">Academic · Tier 2</option>
        <option value="2">Preprints &amp; working papers</option>
        <option value="3">Practitioner &amp; blogs</option>
      </select>
      <select id="jsel" title="Journal" style="display:none"></select>
      <select id="month" style="display:none"></select>
      <input id="q" type="search" placeholder="Search…" autocomplete="off">
    </div>
  </div>
</header>
<main class="wrap"><div id="view"></div>
  <footer>Ranking and summaries are LLM-generated (Gemini, with a Groq fallback) — skim,
    don't trust blindly. Sources: NEP · NBER · arXiv · finance journals &amp; SSRN via
    Crossref · PM-Research · OpenAlex · practitioner &amp; asset-manager research.
  </footer>
</main>
<script>
let DATA=[], CLASSICS=[], MONTHLY={}, VIEW="monthly", MAXSEEN="";
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtK=n=>n>=1000?(n/1000).toFixed(1)+'k':String(n);
const bandColor=s=>s>=70?'var(--strong)':s>=45?'var(--medium)':'var(--low)';
const jlabel=x=>String(x.source||'').replace(/^journal:/,'').replace(/^topic:/,'topic · ');

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
  const sc=(x.score!=null)?`<div class="rail" style="--score:${bandColor(x.score)}">${rk}<div class="score">${x.score}</div><div class="ratebar"></div><div class="cap">rating</div></div>`:`<div class="rail">${rk}</div>`;
  const sm=x.summary?`<div class="summary">${esc(x.summary)}</div>`:'';
  const who=x.authors?' · '+esc(x.authors):'';
  return `<div class="entry">${sc}<div class="body">
    <a class="title" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>
    <div class="meta"><span class="j">${esc(jlabel(x))}</span>${who} · ${esc(x.date||x.seen)}</div>
    ${sm}</div></div>`;
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
function sinceDays(n){
  if(!MAXSEEN)return"";
  const c=new Date(MAXSEEN);c.setDate(c.getDate()-(n-1));
  return c.toISOString().slice(0,10);
}
function renderRecent(){
  const cs=sinceDays(7);
  let rows=cs?DATA.filter(x=>(x.seen||'')>=cs):DATA;
  $('view').innerHTML=`<div class="dateline">Last 7 days <span class="n">· ${rows.length} papers</span></div>`+grouped(rows);
}
const SUBS=[['Innov','innovation'],['Rel','relevance'],['Cites','paper_cites'],['Author','author_cites'],['IF','journal_if']];
function monthlyEntry(x,rank){
  const band=bandColor(x.composite);
  const subs=SUBS.map(([l,k])=>{const v=x[k];const w=Math.max(0,Math.min(100,v||0));
    return `<span class="sub"><i>${l}</i><b>${v==null?'–':Math.round(v)}</b><s><u style="width:${w}%"></u></s></span>`;}).join('');
  const meta=`<span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''}${x.date?' · '+esc(x.date):''}${x.cites!=null?' · '+fmtK(x.cites)+' cites':''}`;
  return `<div class="entry"><div class="rail" style="--score:${band}">
      <div class="rank">${rank}</div><div class="score">${Math.round(x.composite)}</div><div class="ratebar"></div><div class="cap">composite</div></div>
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
      <div class="meta"><span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''}${cites}</div>
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
      <div class="meta"><span class="j">${esc(x.journal||'')}</span>${x.authors?' · '+esc(x.authors):''} · ${esc(x.year||'')}</div>
      ${x.summary?`<div class="summary">${esc(x.summary)}</div>`:''}</div></div>`).join('')
      :'<div class="empty">No history generated yet — run backfill.py.</div>');
}
function render(){VIEW==="monthly"?renderMonthly():VIEW==="recent"?renderRecent():renderClassics();}
function setView(v){
  VIEW=v;['monthly','recent','classics'].forEach(k=>$('t-'+k).classList.toggle('on',k===v));
  $('month').style.display=v==="monthly"?'':'none';
  $('jsel').style.display=v==="classics"?'':'none';
  $('cat').style.display=v==="recent"?'':'none';render();
}
$('t-recent').onclick=()=>setView('recent');
$('t-monthly').onclick=()=>setView('monthly');
$('t-classics').onclick=()=>setView('classics');
$('q').addEventListener('input',render);
$('month').addEventListener('change',render);
$('cat').addEventListener('change',render);
$('jsel').addEventListener('change',render);
const root=document.documentElement;
$('toggle').onclick=()=>{
  const dark=!(root.getAttribute('data-theme')==='dark'||
    (!root.getAttribute('data-theme')&&matchMedia('(prefers-color-scheme:dark)').matches));
  root.setAttribute('data-theme',dark?'dark':'light');$('toggle').textContent=dark?'Light':'Dark';
};
Promise.all([
  fetch('data.json').then(r=>r.json()).catch(()=>[]),
  fetch('classics.json').then(r=>r.json()).catch(()=>[]),
  fetch('monthly.json').then(r=>r.json()).catch(()=>({})),
]).then(([d,c,mo])=>{
  DATA=d;CLASSICS=c;MONTHLY=mo||{};
  MAXSEEN=d.reduce((m,x)=>(x.seen||'')>m?(x.seen||''):m,"");
  Object.keys(MONTHLY).filter(k=>/^\\d{4}-\\d{2}$/.test(k)).sort().reverse()
    .forEach(m=>$('month').add(new Option(new Date(m+"-01").toLocaleString('en',{month:'long',year:'numeric'}),m)));
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
  render();
});
</script>
</body>
</html>
"""
