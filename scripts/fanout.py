#!/usr/bin/env python3
"""fanout — pure headroom-and-queue -> builder-count N mapping.

Provenance: genericized from nonprofit-atlas's `scripts/mac/mbuild_fanout.py` (source
directive: "N should be the number of builders you can safely spawn given current token
availability — never limit it," later revised to add gate-throughput backpressure after a
real incident where fanout kept spawning builders into a merge-gate queue nothing was
draining).

Two decisions, composed:
  1. n_from_headroom  — token budget -> ambition, via a Fibonacci ladder (never a hard cap;
     more headroom always maps to a higher rung, however far the ladder has to walk).
  2. n_with_backpressure — the open-PR queue subtracts from that ambition. NOT a ceiling on
     ambition itself: with a drained queue this returns n_budget unchanged; it only accounts
     for gate capacity the queue has not yet absorbed. Zero is a legitimate answer (queue at
     cap) — that pass should build nothing, not silently build one anyway.

Kept pure (no network, no filesystem) so both are unit-testable without touching your CI
or GitHub. The caller (`worktree_builder.sh`'s orchestrator, or your own script) supplies
real headroom/queue numbers.

Env (read at import; see fleet.env.example): FLEET_HEADROOM_FRACTION (default 0.5) — the
fraction of reported token headroom to plan against; the rest is reserve for other
concurrent consumers of the same account (a reviewer pass, a CEO pass, an architect pass).
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator

DEFAULT_HEADROOM_FRACTION = float(os.environ.get("FLEET_HEADROOM_FRACTION", "0.5"))


def fibonacci_ladder() -> Iterator[int]:
    """Yields 1, 2, 3, 5, 8, 13, 21, 34, ... forever (deduplicated Fibonacci)."""
    a, b = 1, 2
    yield a
    while True:
        yield b
        a, b = b, a + b


def n_from_headroom(
    headroom_tokens: float,
    spend_per_build: float,
    floor: int = 1,
    headroom_fraction: float | None = None,
) -> int:
    """Largest Fibonacci rung <= floor(headroom_tokens * headroom_fraction / spend_per_build)
    "build units", never below `floor`. No upper clamp — the ladder is walked until it
    exceeds the available units, however far that goes.

    headroom_fraction defaults to DEFAULT_HEADROOM_FRACTION (FLEET_HEADROOM_FRACTION env,
    0.5) — the reserve held back for other concurrent consumers of the same token pool. Pass
    headroom_fraction=1.0 explicitly to plan against the full reported headroom.

    Raises ValueError for a non-positive spend_per_build (a caller misconfiguration, not a
    budget state — must not be swallowed into a silent 0 or 1).
    """
    if spend_per_build <= 0:
        raise ValueError(f"spend_per_build must be > 0, got {spend_per_build!r}")
    fraction = DEFAULT_HEADROOM_FRACTION if headroom_fraction is None else headroom_fraction
    effective_headroom = headroom_tokens * fraction
    units = max(0, int(effective_headroom // spend_per_build))
    n = floor
    for rung in fibonacci_ladder():
        if rung > units:
            break
        n = rung
    return max(n, floor)


def n_with_backpressure(n_budget: int, open_pr_count: int, queue_cap: int) -> int:
    """Gate-throughput backpressure: budget sets the AMBITION, the merge-gate queue sets
    what the pipe can absorb. NOT an upper clamp on ambition — with a drained queue this
    returns n_budget unchanged, however large the ladder walked; the next tick re-walks the
    ladder as gates drain. Subtracts the work the gates have not yet absorbed:
    min(n_budget, queue_cap - open_pr_count), floored at 0. Zero is a legitimate answer (a
    queue at cap builds nothing this pass and says so), distinct from the budget floor of 1
    (which applies when the QUEUE has room but the budget itself is thin).
    """
    if queue_cap <= 0:
        raise ValueError(f"queue_cap must be > 0, got {queue_cap!r}")
    return max(0, min(n_budget, queue_cap - max(0, open_pr_count)))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags: dict[str, int] = {}
    it = iter(sys.argv[1:])
    for a in it:
        if a in ("--queue-len", "--queue-cap"):
            try:
                flags[a] = int(next(it))
            except (StopIteration, ValueError):
                print(f"ERROR: {a} needs an integer value", file=sys.stderr)
                return 2
    if len(args) not in (2, 3):
        print("usage: fanout.py <headroom_tokens> <spend_per_build> [floor] "
              "[--queue-len N --queue-cap M]", file=sys.stderr)
        return 2
    headroom_tokens = float(args[0])
    spend_per_build = float(args[1])
    floor = int(args[2]) if len(args) == 3 else 1
    try:
        n = n_from_headroom(headroom_tokens, spend_per_build, floor=floor)
        if "--queue-len" in flags and "--queue-cap" in flags:
            n = n_with_backpressure(n, flags["--queue-len"], flags["--queue-cap"])
        print(n)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
