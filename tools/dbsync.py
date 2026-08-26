#!/usr/bin/env python3
"""Move state.db in and out of Cloudflare R2 instead of git.

WHY: state.db is 53 MB of binary and was committed on every run. Two problems.
It crossed GitHub's 50 MB warning and grows with the archive -- but far worse,
a binary file cannot be merged, so any two workflows whose commits interleave
produce an unresolvable rebase and one run's work is silently discarded. That
has already destroyed a 240-paper rescore and a 1,946-paper GROBID parse.

R2 is object storage: a PUT replaces the object atomically, and the `state-db`
concurrency group already serialises writers, so the conflict cannot arise.

THE RISK THIS TRADES FOR: git kept 64 versions and made mistakes recoverable.
An object store keeps one. So push() refuses to overwrite anything that looks
wrong, and always copies the current object aside first:

    state.db          the live database
    state.db.prev     the version it replaced (one command to roll back)

  python tools/dbsync.py pull          # fetch before a job reads it
  python tools/dbsync.py push          # store after a job writes it
  python tools/dbsync.py status        # what is in the bucket
  python tools/dbsync.py rollback      # promote .prev back to live

Env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET.
With none of them set every command is a NO-OP that exits 0, so a workflow
carries on using the committed copy and nothing breaks before the bucket
exists.
"""

import argparse
import hashlib
import os
import pathlib
import sqlite3
import sys

DB = pathlib.Path("state.db")
KEY = "state.db"
PREV = "state.db.prev"
# A healthy database is tens of MB. Anything far below that is a truncated
# download, an empty file created by a crashed run, or a fresh schema with no
# rows -- none of which should ever replace a good copy.
MIN_BYTES = 5_000_000
MIN_ITEMS = 1_000


def log(m):
    print(m, flush=True)


def _cfg():
    need = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    vals = {k: os.environ.get(k) for k in need}
    return vals if all(vals.values()) else None


def _client(cfg):
    import boto3                                   # noqa: PLC0415
    from botocore.config import Config             # noqa: PLC0415
    return boto3.client(
        "s3",
        endpoint_url=f"https://{cfg['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=cfg["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _healthy(p) -> tuple[bool, str]:
    """Is this file a database worth keeping? Checked before every push, and
    after every pull -- a corrupt object is as bad as a lost one."""
    if not p.exists():
        return False, "file does not exist"
    n = p.stat().st_size
    if n < MIN_BYTES:
        return False, f"only {n/1e6:.1f} MB (expected >= {MIN_BYTES/1e6:.0f} MB)"
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            return False, "failed SQLite quick_check"
        items = con.execute("SELECT count(*) FROM items").fetchone()[0]
        con.close()
    except Exception as e:                          # noqa: BLE001
        return False, f"unreadable: {type(e).__name__}: {e}"
    if items < MIN_ITEMS:
        return False, f"only {items} items (expected >= {MIN_ITEMS})"
    return True, f"{n/1e6:.1f} MB, {items:,} items"


def pull(args):
    cfg = _cfg()
    if not cfg:
        log("[dbsync] R2 not configured -- using the working-copy state.db")
        return 0
    cli = _client(cfg)
    tmp = DB.with_suffix(".db.part")
    try:
        cli.download_file(cfg["R2_BUCKET"], KEY, str(tmp))
    except Exception as e:                          # noqa: BLE001
        # Absent object on a first run is fine; anything else must be loud,
        # because carrying on would rebuild the archive from nothing.
        if "404" in str(e) or "NoSuchKey" in str(e):
            log(f"[dbsync] no {KEY} in the bucket yet -- using the working copy")
            return 0
        log(f"[dbsync] download FAILED: {type(e).__name__}: {str(e)[:200]}")
        return 1
    ok, why = _healthy(tmp)
    if not ok:
        tmp.unlink(missing_ok=True)
        log(f"[dbsync] downloaded copy rejected ({why}) -- keeping the working copy")
        return 1
    tmp.replace(DB)
    log(f"[dbsync] pulled {KEY} ({why})")
    return 0


def push(args):
    cfg = _cfg()
    if not cfg:
        log("[dbsync] R2 not configured -- nothing pushed")
        return 0
    ok, why = _healthy(DB)
    if not ok:
        # The whole point of the guard: an object store has no history, so a
        # bad push is unrecoverable in a way a bad commit never was.
        log(f"[dbsync] REFUSING to push: {why}")
        return 1
    cli = _client(cfg)
    try:                                            # keep one version back
        cli.copy_object(Bucket=cfg["R2_BUCKET"], Key=PREV,
                        CopySource={"Bucket": cfg["R2_BUCKET"], "Key": KEY})
        log(f"[dbsync] previous version kept as {PREV}")
    except Exception as e:                          # noqa: BLE001
        if not ("404" in str(e) or "NoSuchKey" in str(e)):
            log(f"[dbsync] could not snapshot the previous version: {type(e).__name__}")
    cli.upload_file(str(DB), cfg["R2_BUCKET"], KEY)
    head = cli.head_object(Bucket=cfg["R2_BUCKET"], Key=KEY)
    if head["ContentLength"] != DB.stat().st_size:
        log(f"[dbsync] size mismatch after upload: "
            f"{head['ContentLength']} != {DB.stat().st_size}")
        return 1
    log(f"[dbsync] pushed {KEY} ({why}, sha {_sha(DB)[:12]})")
    return 0


# ---------------------------------------------------------------- full text
# docs/ft/ is the parsed full text of every paper GROBID has read -- ~70,000
# characters each, 2,382 of them, and 1,999 from journal publishers. It used to
# be COMMITTED, in a public repository, which is publication rather than
# personal use whatever the intent. docs/abs/ (the abstracts) was gitignored and
# the full text was not, which is the wrong way round.
#
# It cannot simply be untracked. Nothing rebuilds it during a deploy --
# tools/fulltext.py needs GROBID and the PDFs, and pdfs/ is gitignored -- so it
# reached the site ONLY because it was committed. Dropping it from git without
# putting it somewhere would silently kill Ask's passage retrieval, the
# full-text markers and the Implement button.
#
# So it moves here, beside state.db, in the private bucket.
#
# ONE TARBALL, not per-file objects: 2,382 objects means 2,382 requests to sync,
# and fulltext.py writes each file once and never rewrites it, so a whole-
# archive push is both simpler and cheaper. Measured, it compresses to 29% --
# about 58 MB.
FT_DIR = pathlib.Path("docs/ft")
FT_KEY = "ft.tar.gz"
FT_PREV = "ft.tar.gz.prev"
FT_MIN_BYTES = 5_000_000
FT_MIN_MEMBERS = 200


def _ft_healthy(p) -> tuple[bool, str]:
    """Is this tarball worth keeping? Same contract as _healthy for state.db.

    An object store has no history, so a truncated upload replacing a good
    archive is unrecoverable in a way a bad commit never was.
    """
    import tarfile                                    # noqa: PLC0415
    if not p.exists():
        return False, "file does not exist"
    n = p.stat().st_size
    if n < FT_MIN_BYTES:
        return False, f"only {n/1e6:.1f} MB (expected >= {FT_MIN_BYTES/1e6:.0f} MB)"
    try:
        with tarfile.open(p, "r:gz") as tf:
            names = tf.getnames()
    except Exception as e:                            # noqa: BLE001
        return False, f"unreadable tar.gz: {type(e).__name__}: {e}"
    members = [x for x in names if x.endswith(".json")]
    if len(members) < FT_MIN_MEMBERS:
        return False, f"only {len(members)} passage files (expected >= {FT_MIN_MEMBERS})"
    # index.json is what the browser fetches to learn which papers have full
    # text at all; an archive without it leaves every Implement button greyed.
    if not any(x.endswith("index.json") for x in names):
        return False, "no index.json in the archive"
    return True, f"{n/1e6:.1f} MB, {len(members):,} papers"


def _ft_tar(dest) -> None:
    import tarfile                                    # noqa: PLC0415
    with tarfile.open(dest, "w:gz", compresslevel=6) as tf:
        tf.add(FT_DIR, arcname="ft")


def ftpull(args):
    cfg = _cfg()
    if not cfg:
        log("[dbsync] R2 not configured -- using the working-copy docs/ft")
        return 0
    import tarfile                                    # noqa: PLC0415
    cli = _client(cfg)
    tmp = pathlib.Path("ft.tar.gz.part")
    try:
        cli.download_file(cfg["R2_BUCKET"], FT_KEY, str(tmp))
    except Exception as e:                            # noqa: BLE001
        if "404" in str(e) or "NoSuchKey" in str(e):
            # Nothing in the bucket yet. Fine on a fresh setup and fine when the
            # working copy already has the files; the caller decides whether an
            # empty docs/ft is fatal, because only it knows if a deploy follows.
            have = len(list(FT_DIR.glob("*.json"))) if FT_DIR.exists() else 0
            log(f"[dbsync] no {FT_KEY} in the bucket yet "
                f"-- working copy has {have} passage files")
            return 0
        log(f"[dbsync] ft download FAILED: {type(e).__name__}: {str(e)[:200]}")
        return 1
    ok, why = _ft_healthy(tmp)
    if not ok:
        tmp.unlink(missing_ok=True)
        log(f"[dbsync] downloaded ft archive rejected ({why}) -- keeping the working copy")
        return 1
    # Extract over the top rather than replacing the directory: fulltext.py
    # writes each file once, so union is the correct merge and a partial
    # archive can never delete papers the working copy already holds.
    with tarfile.open(tmp, "r:gz") as tf:
        # filter="data" refuses absolute paths, "..", symlinks and device
        # nodes. This archive is one we wrote ourselves, so the traversal risk
        # is small -- but it arrives over the network from an object store, and
        # "we produced it" is an assumption rather than a check. It is also the
        # default from Python 3.14, so setting it now avoids a silent change in
        # behaviour later.
        tf.extractall(FT_DIR.parent, filter="data")
    tmp.unlink(missing_ok=True)
    log(f"[dbsync] pulled {FT_KEY} ({why})")
    return 0


def ftpush(args):
    cfg = _cfg()
    if not cfg:
        log("[dbsync] R2 not configured -- nothing pushed")
        return 0
    if not FT_DIR.exists():
        log(f"[dbsync] {FT_DIR} does not exist -- nothing to push")
        return 1
    tmp = pathlib.Path("ft.tar.gz.part")
    _ft_tar(tmp)
    ok, why = _ft_healthy(tmp)
    if not ok:
        tmp.unlink(missing_ok=True)
        log(f"[dbsync] REFUSING to push ft: {why}")
        return 1
    cli = _client(cfg)
    try:                                              # keep one version back
        cli.copy_object(Bucket=cfg["R2_BUCKET"], Key=FT_PREV,
                        CopySource={"Bucket": cfg["R2_BUCKET"], "Key": FT_KEY})
        log(f"[dbsync] previous ft archive kept as {FT_PREV}")
    except Exception as e:                            # noqa: BLE001
        if not ("404" in str(e) or "NoSuchKey" in str(e)):
            log(f"[dbsync] could not snapshot the previous ft archive: {type(e).__name__}")
    cli.upload_file(str(tmp), cfg["R2_BUCKET"], FT_KEY)
    head = cli.head_object(Bucket=cfg["R2_BUCKET"], Key=FT_KEY)
    if head["ContentLength"] != tmp.stat().st_size:
        log(f"[dbsync] ft size mismatch after upload: "
            f"{head['ContentLength']} != {tmp.stat().st_size}")
        tmp.unlink(missing_ok=True)
        return 1
    log(f"[dbsync] pushed {FT_KEY} ({why}, sha {_sha(tmp)[:12]})")
    tmp.unlink(missing_ok=True)
    return 0


def status(args):
    cfg = _cfg()
    if not cfg:
        missing = [k for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                               "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
                   if not os.environ.get(k)]
        log(f"[dbsync] R2 not configured -- missing: {', '.join(missing)}")
        if DB.exists():
            log(f"[dbsync] local: {_healthy(DB)[1]}")
        return 0
    cli = _client(cfg)
    for k in (KEY, PREV, FT_KEY, FT_PREV):
        try:
            h = cli.head_object(Bucket=cfg["R2_BUCKET"], Key=k)
            log(f"[dbsync] {k:<16} {h['ContentLength']/1e6:>6.1f} MB   "
                f"{h['LastModified']:%Y-%m-%d %H:%M} UTC")
        except Exception:                           # noqa: BLE001
            log(f"[dbsync] {k:<16} absent")
    if DB.exists():
        log(f"[dbsync] local            {_healthy(DB)[1]}")
    return 0


def rollback(args):
    cfg = _cfg()
    if not cfg:
        log("[dbsync] R2 not configured")
        return 1
    cli = _client(cfg)
    cli.copy_object(Bucket=cfg["R2_BUCKET"], Key=KEY,
                    CopySource={"Bucket": cfg["R2_BUCKET"], "Key": PREV})
    log(f"[dbsync] {PREV} promoted to {KEY} -- pull to pick it up")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("pull", "push", "status", "rollback",
                                       "ftpull", "ftpush"))
    args = ap.parse_args()
    return {"pull": pull, "push": push, "status": status, "rollback": rollback,
            "ftpull": ftpull, "ftpush": ftpush}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
