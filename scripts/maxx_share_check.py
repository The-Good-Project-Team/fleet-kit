#!/usr/bin/env python3
"""maxx_share_check.py <fraction> <base_budget_usd> -- this instance's share of a member's
per-pass spend ceiling.

Thin wrapper around maxx_reader.get_headroom() (NOT a second Maxx client -- same env vars,
same fail-open law). Built for run_member.sh: FLEET_GRU_ALLOWANCE_FRACTION already scales
gru's OWN sizing this exact way (allowance_pct = headroom * fraction, see gru.md) -- this
generalizes the same multiplier to every member's max_budget_usd, since get_headroom()'s
fraction is a FLEET-WIDE quantity (both instances on one maxx account see the same number),
not per-instance consumption. A hard skip/no-skip gate on that shared number can't express
"this instance's share" -- scaling the spend ceiling can, the same way gru already does it.

<fraction>         this instance's FLEET_SHARE_FRACTION (e.g. 0.25)
<base_budget_usd>  the member's own mandate.limits.max_budget_usd from its spec, or an
                    empty string if the member is uncapped (run_member.sh's own MAX_BUDGET
                    variable, verbatim -- empty is a real, deliberate, documented state, see
                    that script's own comment on why per-member caps were removed)

Prints exactly one number to stdout: the effective max_budget_usd to actually pass to
`claude -p --max-budget-usd`. Nothing else on stdout -- run_member.sh captures it directly.

Behavior:
  - fraction is None (unreadable meter) -> print <base_budget_usd> unchanged (or nothing, if
    the member was already uncapped). FAILS OPEN: a meter outage must never tighten a
    member's spend, per maxx_reader.py's own documented law.
  - base_budget_usd is set -> effective = base_budget_usd * FLEET_SHARE_FRACTION * fraction.
    Only ever SHRINKS an existing cap, never bypasses a member's own tighter one.
  - base_budget_usd is empty (member uncapped by its own spec) -> a SYNTHESIZED ceiling
    kicks in ONLY because the operator explicitly asked for FLEET_SHARE_FRACTION < 1.0 on
    this instance (run_member.sh only calls this script at all in that case -- see its own
    guard). Uses UNCAPPED_FALLBACK_USD as the base, same reasoning as the kit's old removed
    default: a floor to scale down from, not a claim that this is the "right" size for any
    given member. An instance that never sets FLEET_SHARE_FRACTION never reaches this branch
    -- every member stays exactly as uncapped as before this change.
"""
from __future__ import annotations

import sys

from maxx_reader import get_headroom

# Same value as the kit's OLD removed per-member default (see run_member.sh's own comment on
# why it was removed as a blanket default) -- reused here only as a scaling floor for members
# an operator has explicitly opted into capping via FLEET_SHARE_FRACTION, never applied
# unless that opt-in is active.
UNCAPPED_FALLBACK_USD = 5.0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: maxx_share_check.py <fraction> <base_budget_usd>", file=sys.stderr)
        return 2

    try:
        share = float(argv[1])
    except ValueError:
        print(f"bad fraction: {argv[1]!r}", file=sys.stderr)
        return 2

    base_str = argv[2].strip()
    if base_str:
        try:
            base_budget = float(base_str)
        except ValueError:
            print(f"bad base_budget_usd: {base_str!r}", file=sys.stderr)
            return 2
        was_uncapped = False
    else:
        base_budget = UNCAPPED_FALLBACK_USD
        was_uncapped = True

    fraction, label, _budget = get_headroom()

    if fraction is None:
        # Unreadable meter -- FAILS OPEN. Same law as maxx_reader.py's own header: a bad
        # reading must only ever be usable to CONSERVE, never to invent a new cap. A member
        # that was already capped keeps its own cap unchanged; a member that was uncapped
        # stays uncapped (print nothing) rather than face-value-applying the synthesized
        # fallback -- that fallback is only ever meant to be SCALED by a readable fraction,
        # never applied at full size on a meter outage.
        if not was_uncapped:
            print(f"{base_budget:.4f}")
        return 0

    effective = base_budget * share * fraction
    print(f"{effective:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
