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
    for k in (KEY, PREV):
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
    ap.add_argument("action", choices=("pull", "push", "status", "rollback"))
    args = ap.parse_args()
    return {"pull": pull, "push": push,
            "status": status, "rollback": rollback}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
