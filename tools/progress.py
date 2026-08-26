#!/usr/bin/env python3
"""Progress with a percentage and an ETA, visible WHILE a job runs.

WHY IT NEEDS TO EXIST. Several tools here run for hours -- resolving 25,628
PDFs, dripping references at one request per three seconds, parsing PDFs
through GROBID -- and until now none of them said how far along they were.
Worse, `gh run view --log` returns nothing for an in-progress run, so even
tools that DID log progress were invisible until they finished. Estimating
completion meant timing a small sample and extrapolating, which is guessing
with extra steps: the sample was papers in table order, the archive is not.

Two outputs, because the two audiences differ:

  stdout        the run log, read afterwards
  ::notice::    a GitHub Actions annotation, which appears on the run and is
                readable through the API WHILE the job is still going. This
                is the half that solves the actual problem.

ETA is computed from observed rate, not from a constant, and it is reported
as a range once there is enough signal -- a job whose work is heterogeneous
(a paper resolving on ladder step 1 versus step 8) has no single rate, and
quoting one number implies a precision that is not there.

    p = Progress(len(todo), "resolve")
    for item in todo:
        ...
        p.tick()
    p.done()
"""

import os
import sys
import time


def _in_actions():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _fmt(secs):
    if secs < 0 or secs != secs:                    # negative or NaN
        return "?"
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 5400:
        return f"{secs/60:.0f}m"
    return f"{secs/3600:.1f}h"


class Progress:
    """Counts work, reports percentage and ETA, and does not spam.

    `every` and `every_s` are both honoured: whichever comes first triggers a
    line. A job doing 4 items a second and one doing one item every three
    seconds should not need different call sites to be legible.
    """

    def __init__(self, total, label="work", every=None, every_s=60,
                 out=None, notice=True):
        self.total = max(int(total or 0), 0)
        self.label = label
        self.n = 0
        self.t0 = time.monotonic()
        self.last_t = self.t0
        self.last_n = 0
        self.every = every or max(1, self.total // 100)   # ~1% steps
        self.every_s = every_s
        self.out = out or (lambda m: print(m, flush=True))
        self.notice = notice and _in_actions()
        if self.total:
            self.out(f"[{self.label}] 0/{self.total:,} starting")

    def _emit(self, msg):
        self.out(msg)
        if self.notice:
            # Annotations are the only channel readable from outside while the
            # job is still running, so the percentage goes there too.
            print(f"::notice title={self.label}::{msg}", flush=True)
            sys.stdout.flush()

    def tick(self, n=1):
        self.n += n
        now = time.monotonic()
        due = (self.n - self.last_n) >= self.every or \
              (now - self.last_t) >= self.every_s
        if not due or not self.total:
            return
        el = now - self.t0
        pct = 100.0 * self.n / self.total
        overall = self.n / el if el > 0 else 0
        recent = ((self.n - self.last_n) / (now - self.last_t)
                  if now > self.last_t else overall)
        left = self.total - self.n
        # Two rates, so the ETA is a range. Work here is heterogeneous -- a
        # paper resolving on the first ladder step costs one round trip, one
        # falling through to CORE costs eight -- and a single number would
        # imply a precision that does not exist.
        etas = sorted(left / r for r in (overall, recent) if r > 0)
        eta = (f"{_fmt(etas[0])}-{_fmt(etas[-1])}" if len(etas) == 2 and
               etas[-1] > etas[0] * 1.25 else
               (_fmt(etas[0]) if etas else "?"))
        self._emit(f"[{self.label}] {self.n:,}/{self.total:,} ({pct:.0f}%) "
                   f"· {overall:.1f}/s · elapsed {_fmt(el)} · ETA {eta}")
        self.last_t, self.last_n = now, self.n

    def done(self):
        el = time.monotonic() - self.t0
        rate = self.n / el if el > 0 else 0
        self._emit(f"[{self.label}] {self.n:,}/{self.total:,} complete "
                   f"in {_fmt(el)} ({rate:.1f}/s)")
