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
#
# TWO READ PATHS (gh#293):
#   - FLEET_SHARE_DIR set (the real in-container shape): every instance publishes just its own
#     {instance, fraction, published_at} into this shared, bind-mounted, non-secret directory
#     (see publish_share.sh) and this script sums every FRESH file there. This is the only path
#     that works from inside a container -- the old ROOT scan below can never see anything there
#     (findmnt shows no `instances/` parent tree mounted, by design: that tree also carries every
#     sibling's live CLAUDE_CODE_OAUTH_TOKEN/FLEET_MAXX_KEY).
#   - FLEET_SHARE_DIR unset (host-side / pre-fix / existing tests): falls back to the original
#     ROOT/EXTRA scan of `instances/*/fleet.env`, unchanged -- a host-side caller that already
#     sees the full instances/ tree loses nothing.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${FLEET_INSTANCES_ROOT:-$HOME/fleet-kit/instances}"
EXTRA="${FLEET_EXTRA_ENV_FILES:-}"
SHARE_DIR="${FLEET_SHARE_DIR:-}"
STALE_AFTER_S="${FLEET_SHARE_STALE_AFTER_S:-86400}"

if [ -n "$SHARE_DIR" ]; then
    # Publish (or refresh) THIS instance's own number first -- picks up a hand-edited
    # FLEET_SHARE_FRACTION with no redeploy, same "re-source right before every use" discipline
    # entrypoint.sh's own crontab lines already follow for fleet.env.
    bash "$HERE/publish_share.sh" 2>/dev/null || true

    total=0
    report=""
    found=0
    stale=0
    now=$(date +%s)
    shopt -s nullglob
    files=("$SHARE_DIR"/*.json)
    shopt -u nullglob
    for f in "${files[@]}"; do
        [ -f "$f" ] || continue
        parsed=$(python3 -c "
import json, sys
try:
    d = json.load(open('$f'))
    frac = float(d['fraction'])
    print('%s\t%s\t%s' % (d.get('instance', '?'), frac, int(d.get('published_at', 0))))
except Exception:
    sys.exit(1)
" 2>/dev/null) || { report="${report}  $(basename "$f"): UNREADABLE (skipped)"$'\n'; continue; }
        name="${parsed%%$'\t'*}"
        rest="${parsed#*$'\t'}"
        frac="${rest%%$'\t'*}"
        pub="${rest#*$'\t'}"
        age=$(( now - pub ))
        if [ "$age" -gt "$STALE_AFTER_S" ]; then
            report="${report}  ${name}: ${frac} (STALE, last published ${age}s ago, skipped)"$'\n'
            stale=$((stale + 1))
            continue
        fi
        total=$(python3 -c "print($total + $frac)" 2>/dev/null || echo "$total")
        report="${report}  ${name}: ${frac}"$'\n'
        found=$((found + 1))
    done

    printf '%s' "$report"

    if [ "$found" -eq 0 ]; then
        echo "UNKNOWN: FLEET_SHARE_DIR is set but no fresh published share was found -- cannot"
        echo "verify oversubscription (siblings may not have run this check yet, or the mount is empty)."
        exit 2
    fi

    over=$(python3 -c "print(1 if $total > 1.0 + 1e-9 else 0)" 2>/dev/null || echo 0)
    if [ "$over" = "1" ]; then
        echo "OVERSUBSCRIBED: shares total ${total} > 1.0 -- instances can collectively reserve more"
        echo "than the account has. Lower one of the fractions above."
        exit 1
    fi

    if [ "$stale" -gt 0 ]; then
        echo "PARTIAL: shares total ${total} (<= 1.0) among ${found} fresh instance(s), but ${stale}"
        echo "stale/unreadable share(s) excluded above -- the true total may be higher than shown."
        exit 2
    fi

    echo "ok: shares total ${total} (<= 1.0)"
    exit 0
fi

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
