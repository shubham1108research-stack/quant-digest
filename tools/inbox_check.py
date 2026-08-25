#!/usr/bin/env python3
"""Check the subscription mailbox is reachable, before wiring it into a run.

Reads only environment variables -- nothing is written, nothing is printed that
could leak a credential, and the mailbox is opened READ-ONLY.

Outlook.com is the reason this exists. It advertises AUTH=PLAIN, but Microsoft
has been withdrawing basic authentication for personal accounts in favour of
OAuth2, and the only way to know whether an app password still works for a
given account is to try it. A clear failure here is worth more than a
mysterious "0 items" inside a digest run.

  FEED_IMAP_HOST=outlook.office365.com \
  FEED_IMAP_USER=you@outlook.com \
  FEED_IMAP_PASS='app password' \
  python tools/inbox_check.py
"""

import email
import imaplib
import os
import sys
import collections

# `or`, not a get() default. An unset GitHub Secret still SETS the environment
# variable -- to the empty string -- so get("FEED_IMAP_HOST", "imap.gmail.com")
# returns "" and this connects to nowhere, reporting a mysterious failure for a
# mailbox that is fine. sources.py already carries that warning; the tool
# written to DIAGNOSE the mailbox had the bug itself.
HOST = os.environ.get("FEED_IMAP_HOST") or "imap.gmail.com"
USER = os.environ.get("FEED_IMAP_USER") or ""
PASS = os.environ.get("FEED_IMAP_PASS") or ""
# Comma-separated, matching the collector: SSRN alerts are filtered to one
# label and the mailed-in digest to another, so checking only the first would
# report an empty mailbox while the papers sat in the second.
FOLDER = os.environ.get("FEED_IMAP_FOLDER") or "INBOX"
FOLDERS = [f.strip() for f in FOLDER.split(",") if f.strip()]


def main():
    if not (USER and PASS):
        print("FEED_IMAP_USER / FEED_IMAP_PASS not set")
        return 1
    print(f"host   : {HOST}")
    print(f"user   : {USER[:3]}...@{USER.split('@')[-1] if '@' in USER else '?'}")
    print(f"folder : {FOLDER}\n")

    try:
        M = imaplib.IMAP4_SSL(HOST, 993)
    except Exception as e:                             # noqa: BLE001
        print(f"CONNECT FAILED: {type(e).__name__}: {e}")
        return 1
    print(f"capabilities: {[c for c in M.capabilities if c.startswith('AUTH')]}")

    try:
        M.login(USER, PASS)
    except imaplib.IMAP4.error as e:
        msg = str(e)
        print(f"\nLOGIN REFUSED: {msg[:200]}")
        if "AUTHENTICATE" in msg or "basic" in msg.lower() or "disabled" in msg.lower():
            print("\nThat reads like basic auth being switched off for this account.")
            print("Options, in order of least work:")
            print("  1. auto-forward this mailbox to a free Gmail account and point")
            print("     FEED_IMAP_* at Gmail (app passwords still work there)")
            print("  2. OAuth2: an Azure app registration and a refresh token")
        return 1
    print("login  : OK")

    try:
        # List the labels that actually exist first. A Gmail label IS an IMAP
        # folder, so a filter nobody created, or a name differing only in case,
        # shows up here as "not present" rather than as an empty mailbox --
        # which is the difference between "subscribe to something" and "your
        # filter is misspelled".
        typ, boxes = M.list()
        names = []
        if typ == "OK":
            for b in boxes or []:
                line = b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)
                if '"' in line:
                    names.append(line.rsplit('"', 2)[-2])
        if names:
            print("labels present: " + ", ".join(sorted(names)[:24]))
        print("")

        total = 0
        senders = collections.Counter()
        newest_ssrn = None
        for folder in FOLDERS:
            if names and folder not in names:
                print(f"{folder:<16} NOT PRESENT -- no label by that name")
                continue
            typ, _ = M.select(f'"{folder}"', readonly=True)   # read-only, always
            if typ != "OK":
                print(f"{folder:<16} SELECT failed: {typ}")
                continue
            typ, data = M.search(None, "ALL")
            ids = data[0].split() if data and data[0] else []
            total += len(ids)
            print(f"{folder:<16} {len(ids)} messages")
            for num in ids[-40:]:
                typ, raw = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])
                frm = (msg.get("From") or "")
                dom = frm.split("@")[-1].strip("> ").lower() if "@" in frm else "?"
                senders[dom] += 1
                if "ssrn" in dom:
                    newest_ssrn = num
        # WHAT DO THE LINKS ACTUALLY LOOK LIKE.
        # "IMAP works" and "the mail parses" are different claims, and when the
        # second fails the question is always the same: has the publisher
        # changed its URL shape under the regex? Printing the distinct link
        # SHAPES (host + query key, never a whole body) answers it in one run
        # instead of by guessing.
        if newest_ssrn is not None:
            typ, raw = M.fetch(newest_ssrn, "(BODY.PEEK[])")
            if typ == "OK" and raw and raw[0]:
                import re as _re
                # DECODE FIRST. The body is quoted-printable, so a raw read
                # sees "=3D" where the collector sees "=" -- and the regex
                # being diagnosed is one that hunts for "=". Measuring the
                # undecoded bytes answers a question nobody asked, which is
                # what the first version of this probe did.
                msg = email.message_from_bytes(raw[0][1])
                parts, html_parts = [], []
                for part in (msg.walk() if msg.is_multipart() else [msg]):
                    ct = part.get_content_type()
                    if ct not in ("text/plain", "text/html"):
                        continue
                    try:
                        b = part.get_payload(decode=True) or b""
                        t = b.decode(part.get_content_charset() or "utf-8", "replace")
                    except Exception:                  # noqa: BLE001
                        continue
                    (parts if ct == "text/plain" else html_parts).append(t)
                print("mime parts: text/plain=%d text/html=%d"
                      % (len(parts), len(html_parts)))
                body = "\n".join(parts) or "\n".join(html_parts)
                urls = _re.findall(r"https?://[^\s\"'<>]+", body)
                shapes = collections.Counter()
                for u in urls:
                    m = _re.match(r"https?://([^/]+)(/[^?]*)?(\?.*)?$", u)
                    if not m:
                        continue
                    host, path, qs = m.group(1), m.group(2) or "", m.group(3) or ""
                    key = _re.sub(r"=\d+", "=<n>", qs.split("&")[0]) if qs else ""
                    shapes[f"{host}{_re.sub(r'/\d+', '/<n>', path)[:40]}{key[:28]}"] += 1
                print("")
                print("link shapes in the newest SSRN mail (%d links):" % len(urls))
                for shape, n in shapes.most_common(10):
                    print(f"   {shape:<62} {n:>4}")
                # the specific thing the collector greps for
                # Every plausible shape of an SSRN paper id, so the answer is
                # "which one is it" rather than "the old one is missing".
                for label, pat in (
                        ("sources._SSRN_ABS  abstract_id=<n>", r"abstract[_-]?id=(\d{5,9})"),
                        ("percent-encoded    abstract_id%3D", r"abstract[_-]?id%3D(\d{5,9})"),
                        ("bare               abstract=<n>", r"abstract=(\d{5,9})"),
                        ("percent-encoded    abstract%3D", r"abstract%3D(\d{5,9})"),
                        ("path form          /abstract/<n>", r"/abstract/(\d{5,9})"),
                        ("doi form           ssrn.<n>", r"ssrn\.(\d{5,9})")):
                    n = len(set(_re.findall(pat, body, _re.I)))
                    print(f"   {label:<36} {n:>4} distinct")
                # what a wrapped link actually looks like once decoded
                red = [u for u in urls if "RedirectClick" in u][:1]
                if red:
                    print("")
                    print("   a wrapped link, decoded:")
                    print("     " + red[0][:200])

                # THE LAYOUT AROUND AN ENTRY. Finding the id is only half the
                # job -- the parser also has to know where the title ends and
                # the authors begin, and that is pure layout. Print a window
                # around the LAST abstract link (the first is usually inside
                # the table of contents, which is the trap) so the structure is
                # visible instead of assumed.
                spots = [m.start() for m in
                         _re.finditer(r"abstract(?:[_-]?id)?=\d{5,9}", body, _re.I)]
                if spots:
                    at = spots[-1]
                    lo, hi = max(0, at - 700), min(len(body), at + 260)
                    print("")
                    print("   layout around the last abstract link:")
                    for line in body[lo:hi].split("\n"):
                        if line.strip():
                            print("     | " + line.rstrip()[:96])

        print("")
        if senders:
            print("who is writing to these labels (last 40 each):")
            for dom, n in senders.most_common(12):
                print(f"   {dom:<34} {n:>3}")
        elif total == 0:
            print("no messages in those labels. Either nothing is subscribed yet,")
            print("or the Gmail filter is not applying the label -- compare the")
            print("label list above with FEED_IMAP_FOLDER.")
    finally:
        try:
            M.logout()
        except Exception:                              # noqa: BLE001
            pass
    print("\nmailbox reachable and read-only access works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
