#!/usr/bin/env python3
"""Build everything docs/ needs that is NOT in git, then let the caller deploy.

A Cloudflare Pages deployment is a full snapshot of the uploaded directory, and
several artefacts under docs/ are gitignored because they are derived and large:

    docs/vec.bin, docs/vec.json, docs/abs/   the embedding index   (tools/embed.py)
    docs/edges.bin                           the paper graph       (tools/graph.py)
    docs/map.json                            the knowledge map     (tools/map.py)

A checkout therefore does not have them. Any workflow that runs
`wrangler pages deploy docs` without rebuilding them first publishes a portal
missing those files -- and the browser does not fail loudly: fetch() on a 404
still resolves, so Ask reads past the end of a truncated buffer and the map
tabs simply render nothing.

Six workflows deploy docs/ and only one of them rebuilt anything, which is how
a deploy shipped a portal with no graph at all. This exists so there is ONE
place that knows the list, instead of six that have to remember it.

Failures are per-artefact and non-fatal: a missing map is worth deploying
around, a missing index is not, so embed failing is the only hard error.

  python tools/prepare_deploy.py
  python tools/prepare_deploy.py --skip-map     # when only Ask changed
"""

import argparse
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run(args, *, required):
    label = " ".join(args[1:])
    print(f"[prepare] {label}", flush=True)
    r = subprocess.run([sys.executable, *args], cwd=ROOT)
    if r.returncode == 0:
        return True
    if required:
        print(f"[prepare] FAILED (required): {label}", flush=True)
        sys.exit(r.returncode)
    print(f"[prepare] failed, continuing: {label}", flush=True)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-map", action="store_true")
    args = ap.parse_args()

    # data.json / archive.json are what the browser actually reads, and they
    # are derived from state.db -- which lives in R2 now, not in the checkout.
    # portal.build() was called from exactly one place (main.py, inside the
    # daily digest), so every OTHER deploy republished whatever data.json
    # happened to be committed. A re-score that ranked 6,078 papers went live
    # nowhere. Rebuilding here is required, not best-effort: shipping last
    # week's scores while reporting success is the failure being fixed.
    # The portal is a JavaScript program held in a Python string, so a syntax
    # error in it is invisible to every check Python can make and fatal in the
    # browser. Cheap, and first: there is no point rebuilding a 2 MB index for
    # a page that will load blank.
    run(["tools/check_js.py"], required=True)

    # docs/ft/ -- the parsed full text -- used to reach the site by being
    # COMMITTED to a public repo. It now lives in R2 beside state.db, so it has
    # to be fetched here or the deploy ships without it. Nothing else rebuilds
    # it: fulltext.py needs GROBID and the PDFs, and pdfs/ is gitignored.
    #
    # required=True, and the reason is the shape of the failure rather than its
    # likelihood. A missing docs/ft does not error anywhere -- loadFtIndex()
    # catches, FT_SET becomes empty, and Ask stops quoting papers by section
    # while every Implement button greys out. It looks like a portal that never
    # had the feature. dbsync.ftpull already exits 0 when the bucket simply has
    # no archive yet, so this only fires on a real failure.
    run(["tools/dbsync.py", "ftpull"], required=True)
    ft = ROOT / "docs" / "ft"
    n_ft = len(list(ft.glob("*.json"))) if ft.exists() else 0
    if n_ft:
        print(f"[prepare] docs/ft: {n_ft:,} parsed papers", flush=True)
    elif os.environ.get("ALLOW_EMPTY_FT"):
        print("[prepare] docs/ft is empty and ALLOW_EMPTY_FT is set -- "
              "continuing without passages", flush=True)
    else:
        # FATAL, and it was a warning for exactly one deploy before this.
        #
        # That deploy shipped without the corpus. ftpull ran in a step carrying
        # no R2 credentials, said "R2 not configured" and returned 0, so
        # required=True never fired -- and the warning scrolled past in a green
        # build. A portal that had full text yesterday and none today is a
        # regression, and it is invisible in the browser: loadFtIndex catches,
        # FT_SET goes empty, and the feature simply is not there any more.
        #
        # A genuinely fresh setup with an empty bucket is the only legitimate
        # case, and it is a one-off, so it sets ALLOW_EMPTY_FT=1 and says so out
        # loud rather than every other deploy inheriting its leniency.
        print("[prepare] FAILED: docs/ft is EMPTY after ftpull.\n"
              "    Ask cannot quote passages and every Implement button will be\n"
              "    greyed -- and nothing in the browser reports it, so this\n"
              "    would ship as a silently smaller portal.\n"
              "    Usually the R2 credentials are missing from THIS step: the\n"
              "    ones on the 'Fetch state.db' step do not carry over.\n"
              "    Set ALLOW_EMPTY_FT=1 only for a first run against an empty "
              "bucket.", flush=True)
        sys.exit(1)

    sys.path.insert(0, str(ROOT))
    import portal, store                                  # noqa: E402
    con = store.connect()
    try:
        print(f"[prepare] portal.build -> docs/data.json ({portal.build(con)} items)",
              flush=True)
    finally:
        con.close()

    # The index is required: without it Ask has nothing to search, and a
    # partial vec.bin against a full vec.json is worse than none (loadIndex
    # clamps and warns, but the papers are simply gone from retrieval).
    run(["tools/embed.py"], required=True)

    # Positioning for the For You briefing. NOT required: a CFTC outage, or
    # Socrata rate-limiting a runner, must not stop a portal deploy. The panel
    # degrades to the papers underneath it, and cot.json is committed so the
    # last good copy is still there.
    run(["tools/cot.py"], required=False)

    # The graph is cheap (~12s from the vector cache) and Ask traverses it.
    # cites are NOT rebuilt: they live in state.db and cost ~200 OpenAlex
    # round trips, so they travel with the checkout.
    # REQUIRED. This was best-effort, and that is how the graph being built from
    # a retired embedding model went unnoticed: three features depend on
    # edges.bin -- the graph hop in Ask retrieval, Build's "Adjacent work", and
    # the map tabs -- and every one of them degrades to empty rather than to an
    # error. A silent optional step guarding three silent failures is no guard.
    run(["tools/graph.py", "sim"], required=True)
    run(["tools/graph.py", "export"], required=True)

    if not args.skip_map:
        run(["tools/map.py", "--clusters", "24"], required=False)

    docs = ROOT / "docs"
    print("\n[prepare] docs/ artefacts:", flush=True)
    for name in ("vec.bin", "vec.json", "edges.bin", "map.json", "artifacts.json"):
        f = docs / name
        print(f"    {name:<12} {'%.1f MB' % (f.stat().st_size/1e6) if f.exists() else 'MISSING'}",
              flush=True)
    shards = len(list((docs / "abs").glob("*.json"))) if (docs / "abs").exists() else 0
    print(f"    abs/         {shards} shards", flush=True)


if __name__ == "__main__":
    main()
