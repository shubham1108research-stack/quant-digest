"""Generate a static GitHub Pages portal (docs/) from the SQLite archive.

Exports every archived item to docs/data.json and writes a self-contained
single-page browser (docs/index.html): filter by digest date, full-text search,
filter by section/tier, sorted by LLM rank when available. No server, no build
step, no external assets -- publish by pointing GitHub Pages at /docs.
"""

import json
import pathlib

SECTION_NAMES = {
    "1": "Working papers (NEP + NBER)",
    "2": "arXiv",
    "3": "Journals + SSRN/preprint probe",
    "4": "Practitioner blogosphere",
    "5": "OpenAlex topic sweep",
}


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
            "why": m.get("summary") or m.get("why", ""),
        })
    # newest digest first, then best LLM score within a digest
    out.sort(key=lambda x: (x["seen"] or "", x["score"] if x["score"] is not None
                            else -1), reverse=True)
    return out


def build(con) -> int:
    data = _export(con)
    docs = pathlib.Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "data.json").write_text(json.dumps(data, default=str), encoding="utf-8")
    (docs / "index.html").write_text(
        _INDEX.replace("__SECTIONS__", json.dumps(SECTION_NAMES)), encoding="utf-8")
    return len(data)


_INDEX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quant Research Digest — Archive</title>
<style>
  :root { --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e5e5e5; --accent:#0b5; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#141414; --fg:#e8e8e8; --muted:#999; --line:#2a2a2a; --accent:#2c8; }
  }
  * { box-sizing:border-box; }
  body { font-family:system-ui,-apple-system,sans-serif; margin:0; background:var(--bg);
         color:var(--fg); line-height:1.45; }
  header { position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line);
           padding:12px 16px; }
  h1 { font-size:1.25em; margin:0 0 8px; }
  .controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  input, select { font:inherit; padding:6px 8px; border:1px solid var(--line);
                  border-radius:6px; background:var(--bg); color:var(--fg); }
  #q { flex:1 1 220px; min-width:0; }
  .count { color:var(--muted); font-size:.85em; margin-left:auto; }
  main { max-width:760px; margin:0 auto; padding:8px 16px 48px; }
  .item { padding:12px 0; border-bottom:1px solid var(--line); }
  .item a { color:var(--fg); text-decoration:none; font-weight:600; }
  .item a:hover { text-decoration:underline; }
  .meta { color:var(--muted); font-size:.82em; margin-top:3px; }
  .badge { display:inline-block; font-size:.72em; font-weight:700; padding:1px 6px;
           border-radius:10px; border:1px solid var(--line); margin-right:6px;
           vertical-align:middle; }
  .score { background:var(--accent); color:#fff; border-color:var(--accent); }
  .why { color:var(--muted); font-style:italic; font-size:.85em; margin-top:2px; }
  .empty { color:var(--muted); padding:32px 0; text-align:center; }
</style>
</head>
<body>
<header>
  <h1>Quant Research Digest — Archive</h1>
  <div class="controls">
    <input id="q" type="search" placeholder="Search title, author, source…" autocomplete="off">
    <select id="date"><option value="">All dates</option></select>
    <select id="section"><option value="">All sections</option></select>
    <label style="font-size:.85em;color:var(--muted)">
      <input id="ranked" type="checkbox"> ranked only</label>
    <span class="count" id="count"></span>
  </div>
</header>
<main><div id="list"></div></main>
<script>
const SECTIONS = __SECTIONS__;
let DATA = [];
const $ = id => document.getElementById(id);

fetch('data.json').then(r => r.json()).then(d => {
  DATA = d;
  const dates = [...new Set(d.map(x => x.seen).filter(Boolean))].sort().reverse();
  for (const dt of dates) $('date').add(new Option(dt, dt));
  for (const [k,v] of Object.entries(SECTIONS)) $('section').add(new Option(v, k));
  render();
}).catch(e => { $('list').innerHTML = '<div class="empty">Could not load data.json</div>'; });

['q','date','section','ranked'].forEach(id => {
  $(id).addEventListener('input', render);
  $(id).addEventListener('change', render);
});

function render() {
  const q = $('q').value.toLowerCase().trim();
  const dt = $('date').value, sec = $('section').value, rk = $('ranked').checked;
  const rows = DATA.filter(x => {
    if (dt && x.seen !== dt) return false;
    if (sec && x.section !== sec) return false;
    if (rk && (x.score === null || x.score === undefined)) return false;
    if (q) {
      const hay = (x.title + ' ' + x.authors + ' ' + x.source).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  $('count').textContent = rows.length + ' items';
  if (!rows.length) { $('list').innerHTML = '<div class="empty">No matches.</div>'; return; }
  $('list').innerHTML = rows.map(x => {
    const score = (x.score !== null && x.score !== undefined)
      ? `<span class="badge score">${x.score}</span>` : '';
    const tier = x.tier ? `<span class="badge">${esc(x.tier)}</span>` : '';
    const why = x.why ? `<div class="why">${esc(x.why)}</div>` : '';
    const who = x.authors ? ' · ' + esc(x.authors) : '';
    return `<div class="item">${score}${tier}
      <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>
      <div class="meta">${esc(SECTIONS[x.section] || x.source)}${who} · ${esc(x.date || x.seen)}</div>
      ${why}</div>`;
  }).join('');
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
</script>
</body>
</html>
"""
