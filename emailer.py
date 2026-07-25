"""Render the digest as phone-friendly HTML and send it via Gmail SMTP."""

import datetime as dt
import html
import os
import smtplib
from email.mime.text import MIMEText

import config

SECTION_NAMES = {
    1: "Working papers (NEP + NBER)",
    2: "arXiv",
    3: "Journals + SSRN / preprints",
    4: "Practitioner & house research",
    5: "OpenAlex topic sweep",
}

_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
         "Arial,sans-serif")


def _esc(s):
    return html.escape(s or "")


def _plevel(it: dict) -> int:
    """Prominence tier: 1 (top), 2 (middle), or 0 (rest). A strong author score
    (>=80 top / >=65 mid -- the same reputation signal used everywhere else)
    lifts the tier, consistent with h-index/venue already doing so."""
    score = it.get("rank_score")
    score = score if isinstance(score, int) else -1
    h = it.get("prom_hindex") or 0
    a = it.get("author_score") or 0
    if it.get("tier") == "T1" or h >= config.PROM_H1 or score >= config.RANK_T1 or a >= 80:
        return 1
    if it.get("tier") == "T2" or h >= config.PROM_H2 or score >= config.RANK_T2 or a >= 65:
        return 2
    return 0


def _by_score(it: dict):
    # relevance score nudged by author reputation (the same bounded multiplier
    # the Monthly composite / Recent / For You use -- consistent everywhere)
    s = it.get("rank_score")
    base = s if isinstance(s, int) else -1
    return (base * (it.get("reputation") or 1), it.get("date", ""))


def _pill(text: str, bg: str) -> str:
    return (f'<span style="display:inline-block;background:{bg};color:#fff;'
            'font-size:11px;font-weight:700;padding:2px 8px;border-radius:11px;'
            f'margin:0 5px 4px 0;white-space:nowrap">{_esc(text)}</span>')


def _score_bg(s: int) -> str:
    return "#0a7f4f" if s >= 70 else "#b26b00" if s >= 45 else "#7a7a7a"


def _source_label(it: dict) -> str:
    src = ", ".join(it.get("sources", [it.get("source", "")]))
    return src.replace("journal:", "").replace("topic:", "topic · ")


def _item_card(it: dict) -> str:
    pills = ""
    if it.get("watchlist"):
        pills += _pill(f"★ {it.get('watchlist_author', 'watched')}", "#7a5c00")
    s = it.get("rank_score")
    if isinstance(s, int):
        # for a watched item the score is a LABEL, not a gate -- say so
        label = f"relevance {s}" if it.get("watchlist") else str(s)
        pills += _pill(label, _score_bg(s))
    if it.get("tier"):
        pills += _pill(it["tier"], "#12203a" if it["tier"] == "T1" else "#5b6b8c")
    if it.get("prom_hindex"):
        last = (it.get("prom_author", "").split() or [""])[-1]
        pills += _pill(f"★ h{it['prom_hindex']} {last}", "#6b4a9a")

    meta = " · ".join(x for x in [_esc(_source_label(it)),
                                       _esc(it.get("authors", "")),
                                       _esc(it.get("date", ""))] if x)
    summary = _esc(it.get("summary", ""))
    return (
        '<div style="padding:12px 0;border-bottom:1px solid #ededed">'
        + (f'<div style="margin-bottom:5px">{pills}</div>' if pills else "")
        + f'<a href="{_esc(it["url"])}" style="color:#12203a;'
        'text-decoration:none;font-weight:600;font-size:15px;line-height:1.35">'
        f'{_esc(it["title"])}</a>'
        + (f'<div style="color:#6a6a6a;font-size:12px;margin-top:4px">{meta}</div>'
           if meta else "")
        + (f'<div style="color:#333;font-size:13.5px;margin-top:6px;'
           f'line-height:1.5">{summary}</div>' if summary else "")
        + "</div>")


def _section(title: str, count: int, accent: str) -> str:
    return (f'<h2 style="font-size:15px;margin:26px 0 2px;padding:8px 12px;'
            f'background:{accent};color:#fff;border-radius:6px">{_esc(title)} '
            f'<span style="opacity:.75;font-weight:400">({count})</span></h2>')


def render(items: list[dict], notes: list[str]) -> str:
    today = dt.date.today().strftime("%A, %d %b %Y")

    # When the LLM layer ran, hide the off-topic/noise band from the email
    # (the portal/archive still keeps every item, and unscored ones will get
    # a real score once the LLM catches up in a later run). An item the LLM
    # never got to triage (batch budget exhausted) has NO basis for
    # inclusion -- keeping it "just in case" is how completely unrelated
    # papers (medical, engineering, chemistry -- collected by broad sweeps
    # like OpenAlex/Crossref, never triaged) used to flood the email.
    graded = any(isinstance(it.get("rank_score"), int) for it in items)
    hidden = 0
    if graded:
        # watched-author papers survive the score threshold (you judge them),
        # BUT a watchlist match to an OFF-TOPIC paper is a false positive
        # (a common surname like "Gu"/"Piazzesi" matching a power-electronics
        # paper or a PhD thesis) -- those are dropped, not surfaced.
        def keep(it):
            if it.get("watchlist"):
                return it.get("relevance_category") != "off_topic"
            return (isinstance(it.get("rank_score"), int)
                    and it["rank_score"] >= config.MIN_SHOW_SCORE)
        kept = [it for it in items if keep(it)]
        hidden = len(items) - len(kept)
        items = kept

    subtitle = f"{len(items)} new items"
    if hidden:
        subtitle += f" · {hidden} low-relevance hidden"

    p = [f'<div style="font-family:{_FONT};background:#f4f5f7;padding:16px 0;'
         'margin:0">',
         '<div style="max-width:720px;margin:0 auto;background:#fff;'
         'border-radius:10px;padding:22px 24px;color:#12203a">',
         '<h1 style="font-size:22px;margin:0 0 3px;letter-spacing:-.2px">'
         'Quant Research Digest</h1>',
         f'<div style="color:#6a6a6a;font-size:13px">{today} · '
         f'{subtitle}</div>']

    if config.PORTAL_URL:
        p.append(f'<a href="{_esc(config.PORTAL_URL)}" style="display:inline-block;'
                 'background:#0a7f4f;color:#fff;text-decoration:none;'
                 'font-weight:600;font-size:13px;padding:9px 18px;'
                 'border-radius:7px;margin-top:14px">\U0001F4CA  Browse the full '
                 'searchable archive</a>')

    # ★ Watched authors lead -- always surfaced, whatever they scored; the
    # relevance number is shown as a label (in _item_card), never a filter.
    watched = sorted((it for it in items if it.get("watchlist")),
                     key=_by_score, reverse=True)
    watched_ids = {id(it) for it in watched}
    if watched:
        p.append(_section("★ Watched authors — everything they published",
                          len(watched), "#7a5c00"))
        p += [_item_card(it) for it in watched]

    # Prominence division: Tier 1 / Tier 2 lead; the rest fall to source sections.
    # (watchlist items already shown above -- exclude them from the tiers)
    tier1 = sorted((it for it in items
                    if _plevel(it) == 1 and id(it) not in watched_ids),
                   key=_by_score, reverse=True)
    tier2 = sorted((it for it in items
                    if _plevel(it) == 2 and id(it) not in watched_ids),
                   key=_by_score, reverse=True)
    tiered = {id(it) for it in tier1} | {id(it) for it in tier2} | watched_ids

    if tier1:
        p.append(_section("★ Tier 1 — top journals, prominent authors & "
                          "must-reads", len(tier1), "#12203a"))
        p += [_item_card(it) for it in tier1]
    if tier2:
        p.append(_section("Tier 2 — strong field & established work",
                          len(tier2), "#5b6b8c"))
        p += [_item_card(it) for it in tier2]

    rest = [it for it in items if id(it) not in tiered]
    for sec in sorted(SECTION_NAMES):
        sec_items = sorted((it for it in rest if it.get("section") == sec),
                           key=_by_score, reverse=True)
        if not sec_items:
            continue
        p.append(_section(SECTION_NAMES[sec], len(sec_items), "#8a8a8a"))
        p += [_item_card(it) for it in sec_items]

    p.append('<p style="color:#8a8a8a;font-size:11.5px;line-height:1.5;'
             'margin-top:26px;border-top:1px solid #ededed;padding-top:14px">'
             'Ranking, tiers and summaries are LLM-generated (Gemini) — '
             'skim, don’t trust blindly. SSRN arrives via Crossref (fresh) '
             'and OpenAlex (lagged); asset-manager pages via headless scrape. '
             'Google Scholar is excluded (no API, redundant).</p>')
    if notes:
        p.append('<h2 style="font-size:13px;color:#6a6a6a;margin:18px 0 4px">'
                 'Run notes</h2><ul style="color:#8a8a8a;font-size:11.5px;'
                 'line-height:1.5;padding-left:18px">')
        p += [f"<li>{_esc(n)}</li>" for n in notes]
        p.append("</ul>")

    p.append("</div></div>")
    return "".join(p)


def _clean_secret(s: str) -> str:
    """Strip a leading UTF-8 BOM (U+FEFF) and surrounding whitespace. GitHub
    secrets set from a Windows shell can pick up a stray BOM from stdin
    encoding, which then breaks smtplib's ascii-only command lines."""
    return s.strip().lstrip("﻿")


def send(html_body: str) -> None:
    user = _clean_secret(os.environ["GMAIL_ADDRESS"])
    pw = _clean_secret(os.environ["GMAIL_APP_PASSWORD"]).replace(" ", "")
    # os.environ.get(key, default) only falls back when the key is ABSENT --
    # the workflow's `env:` block always defines DIGEST_RECIPIENT (as "" when
    # the secret is unset), so `or user` is needed to treat blank as unset too.
    to = _clean_secret(os.environ.get("DIGEST_RECIPIENT") or user)

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = f"{config.SUBJECT_PREFIX} — {dt.date.today().isoformat()}"
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, pw)
        smtp.sendmail(user, [to], msg.as_string())
