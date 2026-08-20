"""Before building a headless browser, measure what a plain HTML fetch recovers.

The gap we are attacking: OpenAlex reports ~83% of DOI papers as free to read,
but only ~30% expose a direct pdf_url. The difference is papers that ARE open
access where the link simply is not in any API -- it is in the landing page.

Most repositories publish <meta name="citation_pdf_url"> in STATIC html, which
needs no browser. A headless browser is only required for pages that build the
link in JavaScript. So measure the cheap path first; escalate only for what it
misses.

Only landing pages an OA service already flags as open are visited. No paywall
is probed, no credentials are used.
"""
import collections
import json
import re
import sqlite3
import sys
import time

import requests

EMAIL = "upadhyays1108@gmail.com"
UA = {"User-Agent": f"quant-digest/1.0 (mailto:{EMAIL})",
      "Accept": "text/html,application/xhtml+xml"}
N = 30

con = sqlite3.connect("state.db")
# classics that failed to resolve, which is where the shortfall is
failed = []
for uid, in con.execute("SELECT uid FROM fulltext WHERE status='no_pdf'"):
    failed.append(uid)
fset = set(failed)
cands = []
for uid, title, meta in con.execute("SELECT uid, title, meta FROM items"):
    if uid not in fset or not uid.startswith("doi:"):
        continue
    try:
        d = json.loads(meta)
    except Exception:
        continue
    if d.get("classic"):
        cands.append((uid[4:], title))
print(f"classics that failed PDF resolution: {len(cands)}")
cands = cands[:N]
print(f"probing {len(cands)}\n")

PDF_META = re.compile(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)',
                      re.I)
PDF_META2 = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url',
                       re.I)
PDF_HREF = re.compile(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', re.I)

oa_pages = found = verified = 0
hosts = collections.Counter()

for doi, title in cands:
    # where does an OA service say the free copy lives?
    pages = []
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}",
                         params={"email": EMAIL}, timeout=30, headers=UA)
        if r.ok:
            j = r.json()
            for loc in ([j.get("best_oa_location")] + (j.get("oa_locations") or [])):
                if loc and loc.get("url"):
                    pages.append(loc["url"])
    except Exception:
        pass
    pages = [p for p in dict.fromkeys(pages)][:2]
    if not pages:
        print(f"  --   no OA landing page   {title[:52]}")
        continue
    oa_pages += 1

    got = None
    for page in pages:
        try:
            r = requests.get(page, timeout=30, headers=UA, allow_redirects=True)
            if not r.ok or "html" not in r.headers.get("content-type", ""):
                continue
            html = r.text[:400000]
            m = PDF_META.search(html) or PDF_META2.search(html)
            if m:
                got = requests.compat.urljoin(r.url, m.group(1))
                break
            hrefs = PDF_HREF.findall(html)
            if hrefs:
                got = requests.compat.urljoin(r.url, hrefs[0])
                break
        except Exception:
            continue
        finally:
            time.sleep(0.5)
    if not got:
        print(f"  --   no pdf link in html  {title[:52]}")
        continue
    found += 1
    # a link is not a PDF until it returns %PDF
    try:
        rr = requests.get(got, timeout=40, headers=UA, stream=True,
                          allow_redirects=True)
        head = rr.raw.read(5, decode_content=True)
        if head.startswith(b"%PDF"):
            verified += 1
            hosts[got.split("/")[2].replace("www.", "")] += 1
            print(f"  OK   {title[:52]}")
        else:
            print(f"  ~    link not a pdf      {title[:46]}")
    except Exception:
        print(f"  ~    fetch failed         {title[:46]}")
    time.sleep(0.6)

print(f"\nOA landing page known : {oa_pages}/{len(cands)}")
print(f"pdf link in static html: {found}")
print(f"verified real PDF      : {verified}  "
      f"({100*verified/max(1,len(cands)):.0f}% of the failures)")
if hosts:
    print("hosts:", dict(hosts.most_common(8)))
