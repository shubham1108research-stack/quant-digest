"""Asset-manager house research via headless Playwright (AQR, Man, RA).

These are JS-rendered sites with no RSS. Best-effort: if Playwright/Chromium is
unavailable, a site changes its markup, or a firm blocks the browser, that firm
is logged and skipped -- it never breaks the run. SSRN is intentionally
excluded; it Cloudflare-blocks headless browsers.
"""

import datetime as dt
import re
from urllib.parse import urljoin

import config
import store

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"


def _norm_date(raw: str) -> str:
    """Best-effort ISO date from whatever a page's metadata carries.

    Deliberately stdlib-only. python-dateutil reaches this environment purely
    as a transitive dependency of botocore, so importing it would work today
    and break silently the day boto3 stops pulling it in.
    """
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    if not s:
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)             # ISO 8601 / JSON-LD
    if m:
        return m.group(0)
    bare = s.replace(",", "")
    for fmt in ("%d %B %Y", "%B %d %Y", "%b %d %Y", "%d %b %Y",
                "%m/%d/%Y", "%B %Y", "%b %Y"):
        try:
            return dt.datetime.strptime(bare, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


# Pull the date/summary/author out of the ARTICLE page's standard metadata --
# JSON-LD, OpenGraph, <meta>, <time> -- rather than per-firm CSS selectors.
# All three firms emit at least one of these, and unlike a hand-written
# selector per site it does not break the next time one of them reskins.
_META_JS = """() => {
  const pick = sels => {
    for (const s of sels) {
      const el = document.querySelector(s);
      if (!el) continue;
      const v = el.content || el.getAttribute('datetime') || el.textContent;
      if (v && v.trim()) return v.trim();
    }
    return '';
  };
  let ld = '', ldAuthor = '';
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const j = JSON.parse(s.textContent);
      for (const o of (Array.isArray(j) ? j : [j])) {
        if (!o) continue;
        if (!ld && (o.datePublished || o.dateCreated)) ld = o.datePublished || o.dateCreated;
        if (!ldAuthor && o.author) {
          const a = Array.isArray(o.author) ? o.author : [o.author];
          ldAuthor = a.map(x => (typeof x === 'string' ? x : (x && x.name) || '')).filter(Boolean).join(', ');
        }
      }
    } catch (e) { /* a malformed block must not stop the others */ }
  }
  return {
    date: ld || pick(['meta[property="article:published_time"]', 'meta[name="date"]',
                      'meta[name="publish-date"]', 'meta[itemprop="datePublished"]',
                      'time[datetime]', 'time']),
    desc: pick(['meta[property="og:description"]', 'meta[name="description"]',
                'meta[name="twitter:description"]']),
    author: ldAuthor || pick(['meta[name="author"]', 'meta[property="article:author"]']),
    body: Array.from(document.querySelectorAll('article p, main p, .content p, p'))
            .map(p => p.textContent.trim())
            .filter(t => t.length > 80)
            .slice(0, 3).join(' ')
  };
}"""


_BOILERPLATE = (
    "marketing communication", "may only be accessed by", "cookie",
    "professional investor", "institutional investor", "terms and conditions",
    "by accessing this website", "privacy policy", "not intended for",
)


def _is_boilerplate(text: str) -> bool:
    """True for legal/disclaimer walls served instead of article text."""
    t = (text or "").casefold()
    return sum(1 for k in _BOILERPLATE if k in t) >= 2


def _enrich(browser, item: dict, log) -> None:
    """Fill date/abstract/authors from the article page, in place.

    The listing pages carry only an href and its link text, which is why every
    one of these items was archived with an empty date and a zero-length
    abstract. For You ranks on title + summary, so with no summary a firm post
    scored on its title alone and sank -- house research was effectively
    invisible on the Desk notes band despite being collected.

    Best-effort per item: one slow or broken article page is logged and skipped
    rather than costing the whole firm.
    """
    page = browser.new_page(user_agent=_UA)
    try:
        page.goto(item["url"], timeout=25000, wait_until="domcontentloaded")
        meta = page.evaluate(_META_JS)
    except Exception as e:                             # noqa: BLE001
        log(f"[firms] {item['url']}: no metadata ({type(e).__name__})")
        return
    finally:
        page.close()

    item["date"] = item.get("date") or _norm_date(meta.get("date", ""))
    # og:description is usually the editorial standfirst -- the right summary.
    # Fall back to the opening paragraphs when a page omits it.
    desc = re.sub(r"\s+", " ", (meta.get("desc") or "")).strip()
    body = re.sub(r"\s+", " ", (meta.get("body") or "")).strip()
    text = desc if len(desc) >= 120 else (body or desc)
    if _is_boilerplate(text):
        # Man gates article pages behind a "Marketing communication ... may only
        # be accessed by" interstitial, so the first paragraphs are a legal
        # notice. Storing that as the abstract is worse than storing nothing:
        # it is the same text on every article, so it adds no signal and
        # actively pollutes the embedding index.
        log(f"[firms] {item['url']}: page returned boilerplate, no summary")
        return
    item["abstract"] = text[:config.ABSTRACT_CHARS]
    item["authors"] = re.sub(r"\s+", " ", (meta.get("author") or "")).strip()[:200]


def _clean_title(t: str) -> str:
    # Man Group's aria-label puts the editorial kicker ("The Early View") on
    # one line and the actual headline on the next. Collapsing whitespace first
    # glued them together, producing titles like "The Early View Looking for AI
    # Alpha Without AI Beta" -- which then failed to match anywhere in the
    # listing block, so the summary split fell back to offset 0 and repeated
    # the title back as the abstract. Take the last real line before collapsing.
    lines = [ln.strip() for ln in (t or "").splitlines() if ln.strip()]
    if len(lines) > 1:
        t = lines[-1]
    t = re.sub(r"\s+", " ", (t or "")).strip()
    t = re.sub(r"(?i)^save\b", "", t).strip()                 # RA "Save" button
    t = re.sub(r"(?i)^(article|podcast|video|paper)\s*\|?\s*\d+\s*min\s*\|?\s*",
               "", t).strip()                                 # Man
    t = re.sub(rf"(?i)\b({_MONTHS})[a-z]*\.?\s*20\d\d\b", "", t).strip()   # dates
    return re.sub(r"^[|–—\-\s]+", "", t).strip()


def _title_from_slug(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1].split("?")[0]
    slug = re.sub(r"^\d+-", "", slug)                          # RA numeric prefix
    return slug.replace("-", " ").title()


# "August 19, 2026" / "Aug 19 2026" first, then bare "JUL 2026" -- longest
# match wins, so a day-precise date is never truncated to month precision.
_DATE_RX = re.compile(
    rf"(?i)\b(?:{_MONTHS})[a-z]*\.?\s+\d{{1,2}},?\s+20\d\d\b"
    rf"|\b(?:{_MONTHS})[a-z]*\.?\s+20\d\d\b")


def _listing_meta(link_text: str, block_text: str, title: str) -> tuple[str, str]:
    """Date and summary for one article, read off the LISTING page.

    None of the three firms publishes a machine-readable date on the article
    page -- no JSON-LD, no <meta>, no <time>, checked on all three. But all
    three print the date and a standfirst next to the link on the listing, and
    `_clean_title` was actively deleting the date while tidying the title.

    The summary is whatever follows the LATER of the date and the title: AQR
    lists "<category> <title> <date> <summary>", Man and RAFI list
    "<kicker> <date> <title> <summary>". Taking the later end handles both
    without knowing which firm this is.
    """
    hay = f"{link_text} {block_text}".strip()
    m = _DATE_RX.search(hay)
    date = _norm_date(m.group(0)) if m else ""

    cut = 0
    if m and block_text:
        bm = _DATE_RX.search(block_text)
        if bm:
            cut = bm.end()
    if title and block_text:
        i = block_text.find(title)
        if i >= 0:
            cut = max(cut, i + len(title))
    summary = re.sub(r"\s+", " ", block_text[cut:]).strip(" -–—|·:?!.,")
    # Belt and braces: if the split still left the headline at the front (a
    # listing that renders the title differently from its link text), drop it
    # rather than storing the title twice. An abstract that merely repeats the
    # title tells the ranker nothing it does not already have.
    if title and summary[:len(title)].casefold() == title.casefold():
        summary = summary[len(title):].strip(" -–—|·:?!.,")
    return date, summary


def _scrape(browser, name: str, url: str, pat: str) -> list[dict]:
    rx = re.compile(pat)
    page = browser.new_page(user_agent=_UA)
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:                              # noqa: BLE001
            pass
        # Also walk up to the nearest ancestor carrying real text: that block
        # is where the date and the standfirst live on every one of these
        # listings, so one page load yields the whole record.
        links = page.eval_on_selector_all(
            "a", """els => els.map(e => {
                let n = e, txt = '';
                for (let i = 0; i < 4 && n; i++) {
                    n = n.parentElement;
                    if (n) txt = n.innerText || '';
                    if (txt.length > 60) break;
                }
                return [e.getAttribute('href'),
                        (e.getAttribute('aria-label') || e.textContent || '').trim(),
                        (txt || '').replace(/\\s+/g, ' ').slice(0, 900)];
            })""")
    finally:
        page.close()

    seen, out = set(), []
    for h, t, block in links:
        if not h or not rx.search(h):
            continue
        key = h.split("#")[0].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        title = _clean_title(t)
        # Some Man cards wrap the ENTIRE tile in one anchor, so its text is
        # kicker + date + headline + standfirst run together. Nothing sane can
        # be cut out of that, but the slug is exactly the headline -- use it
        # once the "title" has clearly stopped being a title.
        from_slug = (len(title) < 12 or len(title) > 90
                     or "shimmer" in title.lower() or "{" in title)
        if from_slug:
            title = _title_from_slug(key)
        date, summary = _listing_meta(t, block or "", title)
        if from_slug:
            # A slug title is accurate but mangled -- "Ri Podcast James Talbot",
            # "Views From The Floor 2026 14 July". When the card's standfirst
            # opens with the real headline ("Chips Down, Then What? This week's
            # selloff..."), that sentence is the better title, and moving it out
            # of the summary stops it being stored twice.
            m = re.match(r"\s*(.{15,90}?[.?!])\s+(?=[A-Z“\"'])", summary)
            if m:
                title, summary = m.group(1).strip(), summary[m.end():].strip()
        out.append({
            "title": title, "authors": "",
            "abstract": summary[:config.ABSTRACT_CHARS],
            "url": urljoin(url, h), "date": date,
            "source": name, "section": 4,
        })
        if len(out) >= config.FIRM_MAX_ITEMS:
            break
    return out


def firms(log, existing: set | None = None) -> list[dict]:
    """Scrape each firm's listing, then open only the ARTICLES WE HAVE NOT SEEN
    to pull their date and summary.

    `existing` is the archive's uid set. Firm items have no DOI, so their uid is
    a hash of the title -- which the listing page already gives us. That means a
    known article can be recognised before its page is ever fetched, and a
    steady-state run opens only the handful that are genuinely new instead of
    all 45 every day.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:                             # noqa: BLE001 (ImportError)
        log(f"[firms] Playwright unavailable ({type(e).__name__}); skipped")
        return []
    known = existing or set()
    out = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for name, (url, pat) in config.FIRM_SITES.items():
                try:
                    got = _scrape(browser, name, url, pat)
                except Exception as e:                 # noqa: BLE001
                    log(f"[firms] '{name}' failed: {type(e).__name__}: {e}")
                    continue
                # The listing normally yields date + summary already. Only open
                # article pages for NEW items it could not describe -- a known
                # article is recognised from its title hash without a fetch, so
                # a steady-state run opens almost nothing.
                thin = [it for it in got
                        if not it["abstract"] and store.make_uid(it) not in known]
                for it in thin[:config.FIRM_ENRICH_MAX]:
                    _enrich(browser, it, log)
                dated = sum(1 for it in got if it.get("date"))
                described = sum(1 for it in got if it.get("abstract"))
                print(f"  firms/{name}: {len(got)} articles "
                      f"({dated} dated, {described} with summary, "
                      f"{len(thin)} needed a page fetch)")
                out += got
            browser.close()
    except Exception as e:                             # noqa: BLE001
        log(f"[firms] browser launch failed: {type(e).__name__}: {e}")
    return out
