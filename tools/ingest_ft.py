#!/usr/bin/env python3
"""Store passages a reader extracted from their own PDF in the browser.

The write end of the portal's "add PDF" button. tools/fulltext.py is the other
producer of docs/ft, via GROBID on a server; this one takes what pdf.js pulled
out on the reader's machine, so the PDF itself never leaves it.

WHAT MAKES THIS SAFE TO ARCHIVE
docs/ft is no longer committed. It lives in R2 beside state.db, private, and
the workflow pushes it there rather than to git. The same button written a day
earlier would have published every uploaded paper to a public repository.

WHY THE VALIDATION IS STRICT
Input arrives from a browser, through a Pages Function, through a workflow
dispatch -- three hops, none of which are a place to discover that `uid` was a
path. The uid must match the archive's own uid grammar and the filename is
derived the same way tools/fulltext.py derives it, so a crafted uid cannot
write outside docs/ft.

Reads FT_UID, FT_TITLE and FT_PASSAGES from the environment rather than argv:
passages are tens of kilobytes and a command line has limits that differ per
platform and truncate silently.

    FT_UID=doi:10.1234/x FT_PASSAGES='[{"s":"1 Intro","t":"..."}]' \\
        python tools/ingest_ft.py
"""

import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store    # noqa: E402

FT_DIR = pathlib.Path("docs/ft")
INDEX = FT_DIR / "index.json"
# Matches store.make_uid's output: "doi:...", "arxiv:...", "t:<sha1>".
UID_RE = re.compile(r"^(?:doi:|arxiv:|t:)[\w./:+-]{3,180}$", re.I)
MIN_PASSAGES = 3
MIN_WORDS = 400          # below this the parse failed, whatever it returned


def log(m):
    print(m, flush=True)


def _safe(uid):
    """Filename for a uid -- IDENTICAL to tools/fulltext.py's rule.

    Two copies of this would be two chances to disagree about where a paper's
    passages live, and the portal fetches by this name, so a mismatch is a
    silent 404 rather than an error.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", uid)[:120]


def main():
    uid = (os.environ.get("FT_UID") or "").strip()
    title = (os.environ.get("FT_TITLE") or "").strip()[:300]
    raw = os.environ.get("FT_PASSAGES") or ""

    if not UID_RE.match(uid):
        log(f"[ft] refusing uid {uid[:60]!r}: not a doi:/arxiv:/t: identifier")
        return 1
    try:
        passages = json.loads(raw)
    except Exception as e:                              # noqa: BLE001
        log(f"[ft] passages are not valid JSON: {type(e).__name__}: {e}")
        return 1
    if not isinstance(passages, list):
        log("[ft] passages must be a JSON array")
        return 1

    clean = []
    for p in passages:
        if not isinstance(p, dict):
            continue
        t = re.sub(r"\s+", " ", str(p.get("t") or "")).strip()
        if len(t) < 80:            # a heading or a stray line, not a passage
            continue
        clean.append({"s": str(p.get("s") or "").strip()[:120], "t": t[:20000]})

    words = sum(len(p["t"].split()) for p in clean)
    if len(clean) < MIN_PASSAGES or words < MIN_WORDS:
        # A scanned PDF with no text layer parses to a handful of ligatures and
        # would otherwise be archived as a "full text" paper, licensing
        # specification claims the reader can never check. Refusing is the
        # honest outcome, and the depth gate downstream depends on it.
        log(f"[ft] refusing: {len(clean)} passages / {words} words "
            f"(need >= {MIN_PASSAGES} and >= {MIN_WORDS}). "
            f"Usually a scanned PDF with no text layer.")
        return 1

    FT_DIR.mkdir(parents=True, exist_ok=True)
    path = FT_DIR / f"{_safe(uid)}.json"
    existed = path.exists()
    path.write_text(json.dumps(
        {"uid": uid, "title": title, "n": len(clean), "p": clean,
         "src": "upload"}, ensure_ascii=False), encoding="utf-8")
    log(f"[ft] {'replaced' if existed else 'wrote'} {path} "
        f"({len(clean)} passages, {words:,} words)")

    # index.json is what the browser reads to know which papers HAVE full text.
    # A passage file the index does not list is invisible: FT_SET never contains
    # the uid, so the Implement button stays greyed on a paper we just parsed.
    try:
        idx = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    except Exception:                                   # noqa: BLE001
        idx = {}
    uids = list(idx.get("uids") or [])
    if uid not in uids:
        uids.append(uid)
    idx["uids"] = uids
    idx["n"] = len(uids)
    idx.setdefault("pdfs", {})
    INDEX.write_text(json.dumps(idx), encoding="utf-8")
    log(f"[ft] index now lists {len(uids):,} papers")

    # Same bookkeeping tools/fulltext.py does, so `status` means one thing
    # whichever producer wrote the passages.
    con = store.connect()
    con.execute(
        "INSERT OR REPLACE INTO fulltext (uid, status, passages, words) "
        "VALUES (?,?,?,?)", (uid, "ok", len(clean), words))
    con.commit()
    log("[ft] recorded in the fulltext table")
    return 0


if __name__ == "__main__":
    sys.exit(main())
