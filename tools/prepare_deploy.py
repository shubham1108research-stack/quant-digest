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

    # The index is required: without it Ask has nothing to search, and a
    # partial vec.bin against a full vec.json is worse than none (loadIndex
    # clamps and warns, but the papers are simply gone from retrieval).
    run(["tools/embed.py"], required=True)

    # The graph is cheap (~12s from the vector cache) and Ask traverses it.
    # cites are NOT rebuilt: they live in state.db and cost ~200 OpenAlex
    # round trips, so they travel with the checkout.
    ok = run(["tools/graph.py", "sim"], required=False)
    if ok:
        run(["tools/graph.py", "export"], required=False)

    if not args.skip_map:
        run(["tools/map.py", "--clusters", "24"], required=False)

    docs = ROOT / "docs"
    print("\n[prepare] docs/ artefacts:", flush=True)
    for name in ("vec.bin", "vec.json", "edges.bin", "map.json"):
        f = docs / name
        print(f"    {name:<12} {'%.1f MB' % (f.stat().st_size/1e6) if f.exists() else 'MISSING'}",
              flush=True)
    shards = len(list((docs / "abs").glob("*.json"))) if (docs / "abs").exists() else 0
    print(f"    abs/         {shards} shards", flush=True)


if __name__ == "__main__":
    main()
