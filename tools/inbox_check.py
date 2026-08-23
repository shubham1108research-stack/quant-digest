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

HOST = os.environ.get("FEED_IMAP_HOST", "imap.gmail.com")
USER = os.environ.get("FEED_IMAP_USER")
PASS = os.environ.get("FEED_IMAP_PASS")
FOLDER = os.environ.get("FEED_IMAP_FOLDER", "INBOX")


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
        typ, _ = M.select(FOLDER, readonly=True)       # read-only, always
        if typ != "OK":
            print(f"SELECT {FOLDER} failed: {typ}")
            return 1
        typ, data = M.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        print(f"folder : {len(ids)} messages\n")
        senders = collections.Counter()
        for num in ids[-40:]:
            typ, raw = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            frm = (msg.get("From") or "")
            dom = frm.split("@")[-1].strip("> ").lower() if "@" in frm else "?"
            senders[dom] += 1
        if senders:
            print("who is writing to this mailbox (last 40):")
            for dom, n in senders.most_common(12):
                print(f"   {dom:<34} {n:>3}")
        else:
            print("no messages yet -- subscribe to something and re-run")
    finally:
        try:
            M.logout()
        except Exception:                              # noqa: BLE001
            pass
    print("\nmailbox reachable and read-only access works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
