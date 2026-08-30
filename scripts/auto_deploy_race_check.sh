#!/bin/bash
# auto_deploy_race_check.sh -- surfaces the failure gh#255 found: a raw, uncaught git error in
# $FLEET_LOG_DIR/auto_deploy.cron.log that structurally cannot have come from auto_deploy.sh's
# own guarded code path (its dirty-check runs before any fetch, and its git fetch/pull are both
# scoped to exactly one ref, origin/main -- neither call can produce a multi-branch fast-forward
# error or a ref-lock race). That means some OTHER, unidentified process is running unscoped git
# operations against the same host deploy checkout, on its own schedule, with none of
# auto_deploy.sh's safety checks.
#
# WHY THIS RUNS INSIDE THE CONTAINER (entrypoint.sh's crontab), NOT ON THE HOST like
# auto_deploy.sh itself: this only needs to read/append plain log files under $FLEET_LOG_DIR,
# which is the same directory up.sh bind-mounts from the host (auto_deploy.cron.log is written
# by the HOST crontab's own `>> ... 2>&1` redirect around auto_deploy.sh, but lands in the
# identical bind-mounted path) -- no podman/host access needed. Same shape as
# deploy_staleness_check.sh (gh#201), an independent watchdog with no git/podman needs of its
# own.
#
# WHY DETECTION, NOT A FIX: identifying/killing the second process needs host-level access
# (crontab -l, process list) this pass could not reach -- that is the issue's own named `Ask`
# for a human/host-access member, left fully open. This only makes the symptom loud instead of
# silent, per auto_deploy.sh's own established rule (STALE LOCK, ABORT:): a loud alert over a
# guess, never an auto-remediation attempt.
#
# Cursor-based, not content-hash dedup: auto_deploy.cron.log is append-only and grows forever
# across every 5-minute tick, so "already scanned up to byte N" is both simpler and more
# correct here than postflight_dirty_check.sh's content-hash approach (built for a working
# tree that can revert to the SAME dirty state repeatedly) -- it still satisfies the same rule
# ("key off the actual dirt, not just have-we-ever-seen-dirt-before", scripts/selftest.py:1049):
# bytes already scanned are never rescanned (no duplicate alert), and a genuinely NEW
# occurrence past the cursor always alerts even if identical text already alerted before.
set -uo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "${FLEET_ENV_FILE:-$KIT_DIR/fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-$KIT_DIR/fleet.env}"; set +a; } || true

LOG_DIR="${FLEET_LOG_DIR:?set FLEET_LOG_DIR}"
CRON_LOG="$LOG_DIR/auto_deploy.cron.log"
DEPLOY_LOG="$LOG_DIR/auto_deploy.log"
ALERT_LOG="$LOG_DIR/auto_deploy_race_alerts.log"
STATE_FILE="$LOG_DIR/.auto_deploy_race_check.cursor"

# No cron log yet (auto_deploy.sh has never ticked on this box, or FLEET_LOG_DIR is fresh) --
# nothing to scan.
[ -f "$CRON_LOG" ] || exit 0

# Known-sanctioned lines auto_deploy.sh's own `log()` can legitimately write. An allowlist to
# EXTEND, not a closed set -- auto_deploy.sh could grow new log prefixes later, and a stale
# allowlist here would start false-alerting on its own script (gh#255's own PRD caveat).
SANCTIONED_REGEX='ABORT: working tree dirty|ABORT: local HEAD is not an ancestor|DEPLOY FAILED|STALE LOCK'

# Raw git-failure signatures that structurally cannot come from auto_deploy.sh's own
# single-ref-scoped fetch/pull (see header). Also an allowlist to extend, not a closed set --
# these three are gh#255's own reproduced evidence, not an exhaustive taxonomy of every race
# shape.
SUSPECT_REGEX="cannot lock ref|Cannot fast-forward to multiple branches|would be overwritten by merge"

last_offset=0
if [ -f "$STATE_FILE" ]; then
    last_offset="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
fi
case "$last_offset" in (''|*[!0-9]*) last_offset=0 ;; esac

total_size="$(wc -c < "$CRON_LOG" | tr -d ' ')"

# Log rotated/truncated under us (last_offset now past EOF) -- rescan from the top rather than
# silently skip whatever is there now.
[ "$last_offset" -gt "$total_size" ] && last_offset=0

if [ "$last_offset" -ge "$total_size" ]; then
    exit 0  # nothing appended since the last tick
fi

new_content="$(tail -c "+$((last_offset + 1))" "$CRON_LOG")"
echo "$total_size" > "$STATE_FILE"

matches="$(printf '%s\n' "$new_content" | grep -E "$SUSPECT_REGEX" | grep -vE "$SANCTIONED_REGEX")"
[ -z "$matches" ] && exit 0

now="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# "Did the next tick's deploy subsequently succeed" (AC2) -- auto_deploy.cron.log carries no
# per-line timestamps of its own (git's raw stderr has none), so exact time-correlation isn't
# possible from the matched text alone; report the most recent status auto_deploy.log has
# recorded, same manual cross-reference this issue's own evidence used by hand.
next_tick_status="no 'deploy OK'/'DEPLOY FAILED'/'ABORT:' line found yet in auto_deploy.log"
if [ -f "$DEPLOY_LOG" ]; then
    last_status_line="$(grep -E 'deploy OK|DEPLOY FAILED|ABORT:' "$DEPLOY_LOG" | tail -1)"
    [ -n "$last_status_line" ] && next_tick_status="most recent auto_deploy.log status: $last_status_line"
fi

while IFS= read -r line; do
    [ -z "$line" ] && continue
    {
        echo "[$now] UNRECOGNIZED git failure outside auto_deploy.sh's own guarded path (gh#255): $line"
        echo "  $next_tick_status"
    } >> "$ALERT_LOG"
done <<< "$matches"
