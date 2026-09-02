#!/bin/bash
# check_share_sum.sh -- every instance's FLEET_SHARE_FRACTION must sum to <= 1.0.
#
# Strict slices only hold if the slices fit. Two instances at 0.60 each reserve 120% of the
# hour: both stay inside their "own" share, both pass every local check, and the account is
# oversubscribed -- which is the exact race the slices were introduced to remove, restored
# silently by a config typo (Reif, 2026-09-02, sizing shares across philanthropy +
# fleet-kit-server-fleet).
#
# Read-only and advisory: prints one line, exits 0 when the shares fit and 1 when they do not,
# so a human or a cron caller can page on it without this script ever editing config itself.
set -uo pipefail

ROOT="${FLEET_INSTANCES_ROOT:-$HOME/fleet-kit/instances}"
EXTRA="${FLEET_EXTRA_ENV_FILES:-}"

total=0
report=""
scan() {
    local f="$1" name="$2" v
    [ -f "$f" ] || return 0
    v=$(grep -E '^FLEET_SHARE_FRACTION=' "$f" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
    [ -z "$v" ] && v="1.0"   # unset means "no cap" -- the whole pot, and that is the honest read
    total=$(python3 -c "print($total + $v)" 2>/dev/null || echo "$total")
    report="${report}  ${name}: ${v}"$'\n'
}

for d in "$ROOT"/*/; do
    [ -d "$d" ] && scan "$d/fleet.env" "$(basename "$d")"
done
for f in $EXTRA; do
    scan "$f" "$(basename "$(dirname "$f")")"
done

printf '%s' "$report"
over=$(python3 -c "print(1 if $total > 1.0 + 1e-9 else 0)" 2>/dev/null || echo 0)
if [ "$over" = "1" ]; then
    echo "OVERSUBSCRIBED: shares total ${total} > 1.0 -- instances can collectively reserve more"
    echo "than the account has. Lower one of the fractions above."
    exit 1
fi
echo "ok: shares total ${total} (<= 1.0)"
