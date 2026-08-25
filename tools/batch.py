#!/usr/bin/env python3
"""OpenAI Batch API: submit a pile of chat requests now, collect them later.

Half price, for jobs that do not care about latency. Artifact extraction and
re-scoring are both unattended backfills where 24-hour turnaround costs
nothing; Ask, Build and Council are interactive and stay synchronous.

The shape is submit -> poll -> collect, which does NOT fit inside one workflow
run. So it is split across runs, which suits a pipeline that already runs
daily: submit today, collect tomorrow. The batch id and the custom_id -> uid
map live in `store.kv`, next to the inbox's seen-message ids, which is the same
"remember one thing between runs" problem already solved there.

What this deliberately does NOT do is change what is trusted. Results come back
through the caller's own validator untouched. Batching changes how a request is
delivered, not whether its answer can be believed -- a batched hallucination is
still a hallucination.
"""

import json
import time

import requests

_BASE = "https://api.openai.com/v1"
# Terminal states. `completed` may still contain per-request errors -- the
# batch succeeding and every request in it succeeding are different claims.
_DONE = {"completed", "failed", "expired", "cancelled"}


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def submit(key: str, requests_: list[dict], log,
           endpoint: str = "/v1/chat/completions") -> str | None:
    """Upload a JSONL of chat requests and open a batch. Returns the batch id.

    `requests_` is [{custom_id, body}, ...] -- body is the chat completion
    payload the synchronous path would have sent, unchanged, so the two paths
    cannot drift in what they ask for.
    """
    if not requests_:
        return None
    lines = "\n".join(json.dumps({
        "custom_id": r["custom_id"],
        "method": "POST",
        "url": endpoint,
        "body": r["body"],
    }, ensure_ascii=False) for r in requests_)

    up = requests.post(
        f"{_BASE}/files",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": ("batch.jsonl", lines.encode("utf-8"), "application/jsonl")},
        data={"purpose": "batch"},
        timeout=180,
    )
    up.raise_for_status()
    file_id = up.json()["id"]
    log(f"[batch] uploaded {len(requests_)} requests "
        f"({len(lines) / 1e6:.1f} MB) as {file_id}")

    made = requests.post(f"{_BASE}/batches", headers=_headers(key), json={
        "input_file_id": file_id,
        "endpoint": endpoint,
        "completion_window": "24h",
    }, timeout=60)
    made.raise_for_status()
    bid = made.json()["id"]
    log(f"[batch] opened {bid} (24h window, half price)")
    return bid


def status(key: str, batch_id: str) -> dict:
    r = requests.get(f"{_BASE}/batches/{batch_id}", headers=_headers(key), timeout=60)
    r.raise_for_status()
    return r.json()


def collect(key: str, batch_id: str, log) -> tuple[str, dict[str, dict]]:
    """(state, {custom_id: parsed_body}).

    A non-terminal state returns no results and the caller should leave the
    pending marker in place and try again next run. A TERMINAL state always
    returns -- including failed/expired/cancelled with nothing -- so the caller
    can clear the marker. A batch that will never complete must not wedge the
    daily digest behind it forever.
    """
    info = status(key, batch_id)
    state = info.get("status", "unknown")
    counts = info.get("request_counts") or {}
    log(f"[batch] {batch_id}: {state} "
        f"({counts.get('completed', 0)}/{counts.get('total', 0)} done, "
        f"{counts.get('failed', 0)} failed)")
    if state not in _DONE:
        return state, {}
    out_id = info.get("output_file_id")
    if not out_id:
        log(f"[batch] {state} with no output file; nothing to collect")
        return state, {}

    r = requests.get(f"{_BASE}/files/{out_id}/content",
                     headers={"Authorization": f"Bearer {key}"}, timeout=300)
    r.raise_for_status()
    out: dict[str, dict] = {}
    for line in r.text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            # Per-request errors sit INSIDE a completed batch. Skipping them
            # here is what keeps "the batch finished" from being mistaken for
            # "every request in it worked".
            body = (rec.get("response") or {}).get("body")
            if not body:
                continue
            out[rec["custom_id"]] = body
        except Exception:                              # noqa: BLE001
            continue
    log(f"[batch] retrieved {len(out)} results")
    return state, out


def wait(key: str, batch_id: str, log, timeout_s: int = 0,
         poll_s: int = 60) -> tuple[str, dict[str, dict]]:
    """Poll until terminal or `timeout_s` elapses. timeout_s=0 polls once.

    Only useful for a small batch inside a single run; the daily submit/collect
    split is the normal path and does not use this.
    """
    deadline = time.monotonic() + timeout_s if timeout_s else 0
    while True:
        state, out = collect(key, batch_id, log)
        if state in _DONE or not deadline or time.monotonic() >= deadline:
            return state, out
        time.sleep(poll_s)


def cancel(key: str, batch_id: str, log) -> None:
    try:
        requests.post(f"{_BASE}/batches/{batch_id}/cancel",
                      headers=_headers(key), timeout=60).raise_for_status()
        log(f"[batch] cancelled {batch_id}")
    except Exception as e:                             # noqa: BLE001
        log(f"[batch] cancel failed: {type(e).__name__}: {e}")
