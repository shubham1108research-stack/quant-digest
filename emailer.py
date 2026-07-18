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
    4: "Practitioner blogosphere (Quantocracy)",
    5: "OpenAlex topic sweep (net-new)",
}


def _esc(s):
    return html.escape(s or "")


def render(items: list[dict], notes: list[str]) -> str:
    today = dt.date.today().isoformat()
    parts = [f"<h1 style='font-size:1.3em'>Research digest — {today} "
             f"({len(items)} new items)</h1>"]

    for sec in sorted(SECTION_NAMES):
        sec_items = [it for it in items if it.get("section") == sec]
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
            for it in sorted(group, key=lambda x: x.get("date", ""), reverse=True):
                who = _esc(it.get("authors", ""))
                d = _esc(it.get("date", ""))
                tier = it.get("tier")
                parts.append(
                    "<li>"
                    + (f"<b>[{_esc(tier)}]</b> " if tier else "")
                    + f"<a href='{_esc(it['url'])}'>{_esc(it['title'])}</a>"
                    + (f" — {who}" if who else "")
                    + (f" ({d})" if d else "") + "</li>")
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
    to = _clean_secret(os.environ.get("DIGEST_RECIPIENT", user))

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = f"{config.SUBJECT_PREFIX} — {dt.date.today().isoformat()}"
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, pw)
        smtp.sendmail(user, [to], msg.as_string())
