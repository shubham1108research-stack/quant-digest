"""Asset-manager house research via headless Playwright (AQR, Man, RA).

These are JS-rendered sites with no RSS. Best-effort: if Playwright/Chromium is
unavailable, a site changes its markup, or a firm blocks the browser, that firm
is logged and skipped -- it never breaks the run. SSRN is intentionally
excluded; it Cloudflare-blocks headless browsers.
"""

import re
from urllib.parse import urljoin

import config

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"


def _clean_title(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    t = re.sub(r"(?i)^save\b", "", t).strip()                 # RA "Save" button
    t = re.sub(r"(?i)^article\s*\|?\s*\d+\s*min\s*\|?\s*", "", t).strip()  # Man
    t = re.sub(rf"(?i)\b({_MONTHS})[a-z]*\.?\s*20\d\d\b", "", t).strip()   # dates
    return t


def _title_from_slug(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1].split("?")[0]
    slug = re.sub(r"^\d+-", "", slug)                          # RA numeric prefix
    return slug.replace("-", " ").title()


def _scrape(browser, name: str, url: str, pat: str) -> list[dict]:
    rx = re.compile(pat)
    page = browser.new_page(user_agent=_UA)
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:                              # noqa: BLE001
            pass
        links = page.eval_on_selector_all(
            "a", "els => els.map(e => [e.getAttribute('href'), "
            "(e.getAttribute('aria-label')||e.textContent||'').trim()])")
    finally:
        page.close()

    seen, out = set(), []
    for h, t in links:
        if not h or not rx.search(h):
            continue
        key = h.split("#")[0].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        title = _clean_title(t)
        if len(title) < 12 or "shimmer" in title.lower() or "{" in title:
            title = _title_from_slug(key)
        out.append({
            "title": title, "authors": "", "abstract": "",
            "url": urljoin(url, h), "date": "",
            "source": name, "section": 4,
        })
        if len(out) >= config.FIRM_MAX_ITEMS:
            break
    return out


def firms(log) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:                             # noqa: BLE001 (ImportError)
        log(f"[firms] Playwright unavailable ({type(e).__name__}); skipped")
        return []
    out = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for name, (url, pat) in config.FIRM_SITES.items():
                try:
                    got = _scrape(browser, name, url, pat)
                    print(f"  firms/{name}: {len(got)} articles")
                    out += got
                except Exception as e:                 # noqa: BLE001
                    log(f"[firms] '{name}' failed: {type(e).__name__}: {e}")
            browser.close()
    except Exception as e:                             # noqa: BLE001
        log(f"[firms] browser launch failed: {type(e).__name__}: {e}")
    return out
