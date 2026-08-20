#!/usr/bin/env python3
"""Render the blind bake-off report as a self-contained HTML page.

Two things this has to get right:
  * The KEY stays hidden behind a closed disclosure. Blind grading is the whole
    point; a key visible on load would waste the exercise.
  * Model answers contain their own "### " headings, so the split must anchor on
    exactly "### Model X" and nothing else.
"""
import html
import pathlib
import re
import sys

SRC = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "_artifact/bakeoff-report.md")
OUT = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "bakeoff.html")

CSS = """
:root{
  --paper:#fbfbfa; --ink:#16191d; --muted:#5c6470; --faint:#8b929b;
  --line:#e2e4e6; --panel:#ffffff; --accent:#2f5d50; --accent-soft:#eaf1ee;
  --warn:#8a5a2b; --warn-soft:#f7efe6;
  --serif:Georgia,"Iowan Old Style","Times New Roman",serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --paper:#14161a; --ink:#e8eaec; --muted:#9aa2ac; --faint:#6f7782;
    --line:#282c33; --panel:#1a1d22; --accent:#6fae9b; --accent-soft:#1b2a26;
    --warn:#c99a63; --warn-soft:#2a2119;
  }
}
:root[data-theme="dark"]{
  --paper:#14161a; --ink:#e8eaec; --muted:#9aa2ac; --faint:#6f7782;
  --line:#282c33; --panel:#1a1d22; --accent:#6fae9b; --accent-soft:#1b2a26;
  --warn:#c99a63; --warn-soft:#2a2119;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--serif);font-size:17px;line-height:1.62;}
.wrap{max-width:820px;margin:0 auto;padding:56px 24px 96px;
  display:flex;flex-direction:column;gap:44px;}
header{display:flex;flex-direction:column;gap:14px;
  border-bottom:2px solid var(--ink);padding-bottom:24px;}
h1{font-size:34px;line-height:1.15;margin:0;font-weight:600;text-wrap:balance;
  letter-spacing:-.01em;}
.lede{font-family:var(--sans);font-size:15px;color:var(--muted);margin:0;max-width:62ch;}
.meta{font-family:var(--mono);font-size:12px;color:var(--faint);
  letter-spacing:.04em;text-transform:uppercase;}
.caution{font-family:var(--sans);font-size:14px;color:var(--warn);
  background:var(--warn-soft);border:1px solid var(--warn);border-radius:3px;
  padding:12px 15px;margin:0;}
.case{display:flex;flex-direction:column;gap:18px;}
.eyebrow{font-family:var(--mono);font-size:11.5px;letter-spacing:.13em;
  text-transform:uppercase;color:var(--accent);font-weight:600;}
h2{font-family:var(--sans);font-size:20px;line-height:1.35;margin:0;
  font-weight:600;text-wrap:balance;}
.expect{font-family:var(--sans);font-size:14.5px;line-height:1.55;
  border-left:3px solid var(--accent);background:var(--accent-soft);
  padding:13px 16px;color:var(--ink);}
.expect b{font-weight:600;}
.sources{display:flex;flex-direction:column;gap:6px;margin:0;padding:0;list-style:none;}
.sources li{font-family:var(--sans);font-size:13.5px;color:var(--muted);
  display:flex;gap:9px;align-items:baseline;}
.chip{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;
  text-transform:uppercase;padding:2px 7px;border-radius:2px;flex:none;
  border:1px solid var(--line);color:var(--faint);}
.chip.full{background:var(--accent);color:var(--paper);border-color:var(--accent);
  font-weight:600;}
.answer{border-top:1px solid var(--line);padding-top:20px;
  display:flex;flex-direction:column;gap:10px;}
.badge{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.1em;
  color:var(--paper);background:var(--ink);border-radius:2px;padding:4px 11px;
  align-self:flex-start;}
.body{font-size:16.5px;}
.body h3{font-family:var(--sans);font-size:15px;font-weight:600;margin:18px 0 6px;}
.body p{margin:0 0 11px;}
.body ul{margin:0 0 12px;padding-left:22px;}
.body li{margin:0 0 5px;}
.body code{font-family:var(--mono);font-size:.86em;background:var(--accent-soft);
  padding:1px 5px;border-radius:2px;}
.body strong{font-weight:600;}
.body table{border-collapse:collapse;font-size:14px;font-family:var(--sans);}
.body td,.body th{border:1px solid var(--line);padding:5px 9px;text-align:left;}
.scroll{overflow-x:auto;}
.grid{width:100%;border-collapse:collapse;font-family:var(--sans);font-size:14px;
  font-variant-numeric:tabular-nums;}
.grid th,.grid td{border:1px solid var(--line);padding:9px 11px;text-align:left;}
.grid th{background:var(--panel);font-weight:600;}
.grid td.score{text-align:center;color:var(--faint);width:78px;}
details{border:1px solid var(--line);border-radius:3px;background:var(--panel);}
summary{font-family:var(--sans);font-size:14.5px;font-weight:600;cursor:pointer;
  padding:15px 18px;color:var(--warn);}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
.keybody{padding:0 18px 18px;font-family:var(--mono);font-size:13.5px;
  display:flex;flex-direction:column;gap:7px;color:var(--ink);}
footer{font-family:var(--sans);font-size:13px;color:var(--faint);
  border-top:1px solid var(--line);padding-top:20px;}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;}}
"""


def md(t):
    """Small markdown renderer. LaTeX is shown as monospace rather than
    typeset -- font/script CDNs are blocked here, and a silent KaTeX failure
    would render raw backslashes instead."""
    out, ul = [], False
    for raw in t.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if ul:
                out.append("</ul>")
                ul = False
            continue
        esc = html.escape(stripped)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
        esc = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", esc)
        esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
        esc = re.sub(r"\$\$(.+?)\$\$", r"<code>\1</code>", esc)
        esc = re.sub(r"\$([^$]+)\$", r"<code>\1</code>", esc)
        m = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if m:
            if ul:
                out.append("</ul>")
                ul = False
            head = re.sub(r"\*\*(.+?)\*\*", r"\1", html.escape(m.group(1)))
            out.append(f"<h3>{head}</h3>")
            continue
        if re.match(r"^[-*+]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
            if not ul:
                out.append("<ul>")
                ul = True
            item = re.sub(r"^([-*+]|\d+\.)\s+", "", esc)
            out.append(f"<li>{item}</li>")
            continue
        if ul:
            out.append("</ul>")
            ul = False
        out.append(f"<p>{esc}</p>")
    if ul:
        out.append("</ul>")
    return "\n".join(out)


src = SRC.read_text(encoding="utf-8")
# key lives at the end; hold it back for the disclosure
key_split = re.split(r"^## Key \(read only after grading\)\s*$", src, flags=re.M)
body_src, key_src = key_split[0], (key_split[1] if len(key_split) > 1 else "")
grid_split = re.split(r"^## Scoring grid\s*$", body_src, flags=re.M)
cases_src = grid_split[0]

blocks = re.split(r"^## Case: (.+)$", cases_src, flags=re.M)[1:]
cases = list(zip(blocks[0::2], blocks[1::2]))

parts = []
for name, block in cases:
    # anchor on EXACTLY "### Model X" -- answers contain their own ### headings
    seg = re.split(r"^### Model ([A-D])\s*$", block, flags=re.M)
    head, answers = seg[0], list(zip(seg[1::2], seg[2::2]))
    q = re.search(r"\*\*Question\.\*\*\s*(.+)", head)
    ctx = re.search(r"\*\*Context\.\*\*\s*(.+)", head)
    exp = re.search(r"\*\*Correct behaviour\.\*\*\s*(.+)", head)
    srcs = re.findall(r"^-\s+`(\w+)`\s+(.+)$", head, flags=re.M)

    p = [f'<section class="case"><div class="eyebrow">{html.escape(name.strip())}</div>']
    if q:
        p.append(f"<h2>{html.escape(q.group(1).strip())}</h2>")
    if ctx:
        p.append(f'<div class="meta">{html.escape(ctx.group(1).strip())}</div>')
    if exp:
        p.append(f'<div class="expect"><b>Expected:</b> '
                 f'{html.escape(exp.group(1).strip())}</div>')
    if srcs:
        p.append('<ul class="sources">')
        for depth, title in srcs:
            cls = "chip full" if depth == "full" else "chip"
            p.append(f'<li><span class="{cls}">{html.escape(depth)}</span>'
                     f'<span>{html.escape(title.strip())}</span></li>')
        p.append("</ul>")
    for letter, ans in answers:
        p.append(f'<div class="answer"><div class="badge">MODEL {letter}</div>'
                 f'<div class="body">{md(ans.strip().rstrip("- "))}</div></div>')
    p.append("</section>")
    parts.append("\n".join(p))

letters = sorted({l for _, b in cases
                  for l in re.findall(r"^### Model ([A-D])\s*$", b, flags=re.M)})
grid = ['<div class="scroll"><table class="grid"><thead><tr><th>Case</th>']
grid += [f"<th>Model {l}</th>" for l in letters]
grid.append("</tr></thead><tbody>")
for name, _ in cases:
    grid.append(f"<tr><td>{html.escape(name.strip())}</td>"
                + '<td class="score">·</td>' * len(letters) + "</tr>")
grid.append("</tbody></table></div>")

key_rows = re.findall(r"\*\*Model ([A-D])\*\*\s*=\s*`([^`]+)`", key_src)
key_html = "".join(f"<div><strong>Model {l}</strong> &nbsp;=&nbsp; {html.escape(m)}</div>"
                   for l, m in key_rows)

page = f"""<title>Ask Model Bake-off</title>
<style>{CSS}</style>
<div class="wrap">
<header>
  <div class="meta">{len(cases)} cases &middot; {len(letters)} models &middot; 20 answers</div>
  <h1>Which model should answer your research questions?</h1>
  <p class="lede">Four candidates answered the same five cases over identical context
  drawn from your archive. They appear only as letters. The case that matters most
  supplies <em>abstracts only</em> and asks for a regression specification &mdash; any
  model that produces one has invented it.</p>
  <p class="caution">The key is at the bottom, behind a closed panel. Score every case
  before you open it.</p>
</header>
{"".join(parts)}
<section class="case">
  <div class="eyebrow">scoring</div>
  <h2>Score each 0&ndash;2</h2>
  <div class="expect"><b>2</b> &mdash; did the right thing &nbsp;·&nbsp;
  <b>1</b> &mdash; partly &nbsp;·&nbsp; <b>0</b> &mdash; did the wrong thing,
  e.g. invented a specification it could not have known.</div>
  {"".join(grid)}
</section>
<details>
  <summary>Reveal which model is which &mdash; only after scoring</summary>
  <div class="keybody">{key_html}</div>
</details>
<footer>Generated from bakeoff-report.md. Contexts were byte-identical across
models, so the only variable is the model itself.</footer>
</div>
"""
OUT.write_text(page, encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size/1000:.0f} KB) "
      f"— {len(cases)} cases, {len(letters)} models, key hidden: {bool(key_rows)}")
