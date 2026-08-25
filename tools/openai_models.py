#!/usr/bin/env python3
"""List the OpenAI models this account can actually call.

Exists because a wrong model string fails at REQUEST time, before any output,
and the failure looks like a provider outage rather than a typo. That is not
hypothetical here: the artifact backfill lost every OpenAI call to
`max_tokens is not supported with this model` and retired the one provider with
credit, because the code inferred an API shape instead of checking it.

So: ask, then set the constant. Run in CI where the key lives.

    python tools/openai_models.py            # chat + embedding models
    python tools/openai_models.py --all      # everything the account exposes
"""

import argparse
import os
import sys

import requests

_URL = "https://api.openai.com/v1/models"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="do not filter to chat/embedding families")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY not set", file=sys.stderr)
        return 1

    r = requests.get(_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if not r.ok:
        print(f"{r.status_code}: {r.text[:300]}", file=sys.stderr)
        return 1

    ids = sorted(m["id"] for m in (r.json().get("data") or []))
    if not args.all:
        # Everything else on the account -- audio, image, moderation, legacy
        # completions -- is noise for the three decisions this informs.
        ids = [m for m in ids
               if m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-"))
               or "embedding" in m]

    chat = [m for m in ids if "embedding" not in m]
    embed = [m for m in ids if "embedding" in m]

    print(f"{len(ids)} models available\n")
    print("CHAT  (pick OPENAI_ASK_MODEL and OPENAI_BULK_MODEL from these)")
    for m in chat:
        print(f"    {m}")
    print("\nEMBEDDING  (pick OPENAI_EMBED_MODEL)")
    for m in embed:
        print(f"    {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
