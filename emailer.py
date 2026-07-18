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
    3: "Journals + SSRN/preprint probe",
    4: "Practitioner blogosphere",
    5: "OpenAlex topic sweep (net-new)",
}


def _esc(s):
    return html.escape(s or "")


def _plevel(it: dict) -> int:
    """Prominence tier: 1 (top), 2 (middle), or 0 (rest)."""
    score = it.get("rank_score")
    score = score if isinstance(score, int) else -1
    h = it.get("prom_hindex") or 0
    if it.get("tier") == "T1" or h >= config.PROM_H1 or score >= config.RANK_T1:
        return 1
    if it.get("tier") == "T2" or h >= config.PROM_H2 or score >= config.RANK_T2:
        return 2
    return 0


def _item_li(it: dict) -> str:
    who = _esc(it.get("authors", ""))
    d = _esc(it.get("date", ""))
    why = _esc(it.get("why", ""))
    badges = ""
    if isinstance(it.get("rank_score"), int):
        badges += f"<b>[{it['rank_score']}]</b> "
    if it.get("tier"):
        badges += f"<b>[{_esc(it['tier'])}]</b> "
    if it.get("prom_hindex"):
        last = (it.get("prom_author", "").split() or [""])[-1]
        badges += f"<b>[★h{it['prom_hindex']} {_esc(last)}]</b> "
    return ("<li>" + badges
            + f"<a href='{_esc(it['url'])}'>{_esc(it['title'])}</a>"
            + (f" — {who}" if who else "")
            + (f" ({d})" if d else "")
            + (f" <i>— {why}</i>" if why else "") + "</li>")


def _by_score(it: dict):
    s = it.get("rank_score")
    return (s if isinstance(s, int) else -1, it.get("date", ""))


def render(items: list[dict], notes: list[str]) -> str:
    today = dt.date.today().isoformat()

    # When the LLM layer ran, hide the off-topic/noise band from the email
    # (the portal/archive still keeps every item). Unscored items are kept.
    graded = any(isinstance(it.get("rank_score"), int) for it in items)
    hidden = 0
    if graded:
        kept = [it for it in items
                if not (isinstance(it.get("rank_score"), int)
                        and it["rank_score"] < config.MIN_SHOW_SCORE)]
        hidden = len(items) - len(kept)
        items = kept

    subtitle = f"{len(items)} items"
    if hidden:
        subtitle += f"; {hidden} low-relevance hidden"
    parts = [f"<h1 style='font-size:1.3em'>Research digest — {today} "
             f"({subtitle})</h1>"]

    # Prominence division: Tier 1 (top journals / prominent authors / must-reads)
    # and Tier 2 lead; everything else falls through to the per-source sections.
    tier1 = sorted((it for it in items if _plevel(it) == 1), key=_by_score,
                   reverse=True)
    tier2 = sorted((it for it in items if _plevel(it) == 2), key=_by_score,
                   reverse=True)
    tiered = {id(it) for it in tier1} | {id(it) for it in tier2}

    for label, group in (("★ Tier 1 — top journals, prominent authors & "
                          "must-reads", tier1),
                         ("Tier 2 — strong field, established authors & "
                          "relevant work", tier2)):
        if not group:
            continue
        parts.append(f"<h2 style='font-size:1.15em'>{label} ({len(group)})</h2>"
                     "<ul style='margin-top:0'>")
        parts += [_item_li(it) for it in group]
        parts.append("</ul>")

    rest = [it for it in items if id(it) not in tiered]
    for sec in sorted(SECTION_NAMES):
        sec_items = [it for it in rest if it.get("section") == sec]
        if not sec_items:
            continue
        parts.append(f"<h2 style='font-size:1.15em'>{SECTION_NAMES[sec]} "
                     f"({len(sec_items)})</h2>")
        by_src: dict[str, list] = {}
        for it in sec_items:
            key = ", ".join(it.get("sources", [it["source"]]))
            by_src.setdefault(key, []).append(it)
        for src_label in sorted(by_src):
            group = by_src[src_label]
            parts.append(f"<p><b>{_esc(src_label)}</b></p><ul style='margin-top:0'>")
            parts += [_item_li(it) for it in
                      sorted(group, key=lambda x: x.get("date", ""), reverse=True)]
            parts.append("</ul>")

    parts.append("<p><i>Reminder: check this week's SSRN email alert — the "
                 "automated preprint probe lags and is partial. Google Scholar "
                 "is deliberately excluded (no API). Firm house-research "
                 "(AQR, Man, etc.) is not covered in this no-LLM version — "
                 "Quantocracy carries part of the practitioner voice.</i></p>")
    if notes:
        parts.append("<h2 style='font-size:1.15em'>Run notes</h2><ul>")
        parts += [f"<li>{_esc(n)}</li>" for n in notes]
        parts.append("</ul>")
    return ("<div style='font-family:sans-serif;max-width:680px'>"
            + "".join(parts) + "</div>")


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
