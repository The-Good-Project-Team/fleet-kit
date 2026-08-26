#!/usr/bin/env python3
"""fanout — real budget headroom -> minion count N, as arithmetic instead of a judgment call.

RESTORED 2026-08-26. This module existed, was deleted in 4daeed8 (Reif, 2026-08-21: "gru
itself should decide runway/priority/N, not have N handed to it by a bash script it can't see
the reasoning of"), and is back for a reason the deletion could not have known yet: 69 real
fanouts later, N wanders 1-4 with no pattern, because a fresh `claude -p` pass re-derives it
every hour from prose and cannot see its own history. The original directive this file was
built around is still the one we want, and is still in force:

  "N should be the number of builders you can safely spawn given current token availability
   -- never limit it."

WHAT CHANGED SO THIS ISN'T JUST A REVERT. The 2026-08-21 objection was legitimate: a bash
script handed gru a number with no visible reasoning. So this module does not hand gru a
number -- it hands gru a number PLUS the full derivation (`explain()`, every input and the
rung it landed on), which gru quotes in its own report. The judgment gru actually owns
(WHICH items, whether the tier is worth spending on at all) stays gru's; only the arithmetic
that a model cannot do reliably from prose -- multiply, divide, compare -- moves here.

WHAT THE HISTORY SAYS, and why the ladder is still uncapped:
  N=1: 27 passes, 48% declined, 0.44 PRs/pass
  N=2: 30 passes, 55% declined, 0.63 PRs/pass
  N=3:  9 passes, 19% declined, 0.44 PRs/pass
  N=4:  3 passes, 17% declined, 0.67 PRs/pass
Decline rate FALLS as N rises, so concurrent minions are not exhausting the account pool --
whatever causes a decline is upstream of N, and clamping N never addressed it. That is the
evidence for keeping the ladder unclamped.

The counter-evidence, recorded honestly because it is the strongest argument against this
file: on 2026-08-22, while THIS module was still driving fanout, eight consecutive passes ran
at N=3-4 with zero declines and zero successes. Unlimited ambition produced nothing that day.
N is a throughput ceiling, not a throughput cause -- raising it cannot fix a step that fails
for its own reasons. Read `spend_per_build` from real history (fleet_db.py spend) so this
stays anchored to what a minion actually costs, and watch PRs/pass, not N, to judge it.

Kept pure -- no network, no filesystem, no env reads at import -- so it is unit-testable and
so a caller can never get a different answer than the one selftest checks.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator

# The fraction of reported headroom to plan against; the rest is reserve for every OTHER
# consumer of the same token pool (gru's own pass, judge-judy, marie, the-fixer). Spending
# 100% of headroom on minions starves the members that decide WHAT minions should build.
DEFAULT_HEADROOM_FRACTION = 0.5


def fibonacci_ladder() -> Iterator[int]:
    """Yields 1, 2, 3, 5, 8, 13, 21, 34, ... forever (deduplicated Fibonacci).

    A ladder rather than raw division so N moves in meaningful steps: going 5 -> 6 minions is
    noise, 5 -> 8 is a decision. It also means a small headroom misread cannot swing N wildly.
    """
    a, b = 1, 2
    yield a
    while True:
        yield b
        a, b = b, a + b


def n_from_headroom(
    headroom_usd: float,
    spend_per_build: float,
    floor: int = 1,
    ceiling: int | None = None,
    headroom_fraction: float = DEFAULT_HEADROOM_FRACTION,
) -> int:
    """Largest Fibonacci rung <= (headroom_usd * headroom_fraction) / spend_per_build.

    Never below `floor` -- a pass that can afford nothing still tries one item, because the
    alternative is a fleet that silently stops building the moment a meter reads low.

    `ceiling` is None by default, honoring "never limit it." It exists only so an operator can
    impose a deliberate cap from fleet.env; it is not a safety mechanism and nothing in this
    kit sets it.

    Raises ValueError on a non-positive spend_per_build -- that is caller misconfiguration,
    not a budget state, and must never be swallowed into a silent 0 or 1.
    """
    if spend_per_build <= 0:
        raise ValueError(f"spend_per_build must be > 0, got {spend_per_build!r}")
    if not 0.0 < headroom_fraction <= 1.0:
        raise ValueError(f"headroom_fraction must be in (0, 1], got {headroom_fraction!r}")

    units = max(0, int((max(0.0, headroom_usd) * headroom_fraction) // spend_per_build))
    n = floor
    for rung in fibonacci_ladder():
        if rung > units:
            break
        n = rung
    n = max(n, floor)
    if ceiling is not None:
        n = min(n, ceiling)
    return n


def explain(
    headroom_usd: float,
    spend_per_build: float,
    claimable: int | None = None,
    floor: int = 1,
    ceiling: int | None = None,
    headroom_fraction: float = DEFAULT_HEADROOM_FRACTION,
) -> dict:
    """n_from_headroom plus every input that produced it, for gru to quote verbatim.

    This is the whole answer to the 2026-08-21 objection: the caller is never handed a bare
    integer whose reasoning it cannot see. `binding` names WHY N is what it is -- budget, the
    backlog, the floor, or an operator ceiling -- which is the one thing a reader of gru's
    report actually wants to know.
    """
    budget_n = n_from_headroom(headroom_usd, spend_per_build, floor=floor,
                               ceiling=ceiling, headroom_fraction=headroom_fraction)
    n = budget_n
    binding = "budget"
    # Never pad N with items that don't exist: spawning a minion with nothing claimable to
    # hand it burns a spin-up to do nothing. gru still owns WHICH items -- this only refuses
    # to let arithmetic outrun the backlog.
    if claimable is not None and claimable < n:
        n = max(claimable, 0)
        binding = "claimable_items"
    if n <= floor and binding == "budget" and budget_n <= floor:
        binding = "floor"
    if ceiling is not None and n == ceiling and budget_n >= ceiling:
        binding = "operator_ceiling"

    return {
        "n": n,
        "binding": binding,
        "headroom_usd": round(float(headroom_usd), 4),
        "headroom_fraction": headroom_fraction,
        "planned_usd": round(max(0.0, float(headroom_usd)) * headroom_fraction, 4),
        "spend_per_build": round(float(spend_per_build), 4),
        "budget_affords": budget_n,
        "claimable_items": claimable,
        "floor": floor,
        "ceiling": ceiling,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Compute how many minions this pass can afford, and show the derivation.")
    ap.add_argument("--headroom-usd", type=float, required=True,
                    help="dollars of budget this pass may plan against")
    ap.add_argument("--spend-per-build", type=float, required=True,
                    help="real cost of one minion pass (fleet_db.py spend --member minion)")
    ap.add_argument("--claimable", type=int, default=None,
                    help="how many genuinely claimable items exist; N is never padded past it")
    ap.add_argument("--floor", type=int, default=1)
    ap.add_argument("--ceiling", type=int, default=None,
                    help="deliberate operator cap; omitted by default ('never limit it')")
    ap.add_argument("--headroom-fraction", type=float, default=DEFAULT_HEADROOM_FRACTION)
    ap.add_argument("--json", action="store_true", help="print the full derivation, not just N")
    a = ap.parse_args(argv)

    try:
        result = explain(a.headroom_usd, a.spend_per_build, claimable=a.claimable,
                         floor=a.floor, ceiling=a.ceiling,
                         headroom_fraction=a.headroom_fraction)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2) if a.json else result["n"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
