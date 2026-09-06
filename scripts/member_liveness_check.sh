#!/bin/bash
# member_liveness_check.sh -- page a human when NO fleet member has completed work recently.
# The dead man's switch: the work itself is the heartbeat, an outsider alarms on silence.
#
# WHY (fleet-kit#512): two outages went unpaged. Sep 3-5 (~40h): an invalid hour field made
# Vixie cron discard the whole crontab while `cron -f` sat alive, and every probe on this box
# watched a component that survives cron dying (the dashboard is started by entrypoint, the
# tunnel is the host's, auto_deploy is host cron, account_health reads a pool log). Aug 30 -
# Sep 1 (~25h): both accounts genuinely exhausted, 592 budget_declined runs, no page. Nothing
# asserted the one thing that matters: DID ANY MEMBER DO WORK. This does, and only that.
#
# WHAT IT READS: fleet.db's `runs` table (the run index every member's report lands in --
# `status='ok'` means a pass completed and reported). Newest `ok` across ALL members older
# than LIVENESS_MAX_AGE_S is silence. Every hourly member (jefe :21, marie :33, gru :03,
# judge-judy */15) lands an `ok` well inside the default 3h, so a 3h gap is not a quiet hour,
# it is a stopped fleet. Tune per instance via LIVENESS_MAX_AGE_S if a fleet runs slower.
#
# TWO DISTINCT STATES, two problem keys, so alert_store dedupes each on its own:
#   silent        -- critical, pages immediately: nothing ran and we do not know why.
#   out_of_tokens -- degraded, pages once: the pool's own exhausted-state file names a reset
#                    in the future. Legitimate, bounded, and the page carries the reset time
#                    instead of 592 declined runs saying nothing.
# A missing/unreadable fleet.db is `transient` (we are BLIND, not evidence the fleet is dead)
# and alert_store escalates it to degraded if we stay blind past its own threshold.
#
# WHY HOST-SIDE, PLAIN BASH: the failure this watches for is "the container's cron is dead"
# -- a check inside that container would die with it. Same reasoning account_health_check.sh
# documents. Zero Claude Code dependency; python3 is only used to read sqlite.
#
# Usage (host cron, one line per instance; see schedulers/README.md):
#   */5 * * * * FLEET_LOG_DIR=/home/ubuntu/fleet-kit/instances/<name>/logs \
#     FLEET_INSTANCE_NAME=<name> NTFY_TOPIC=<topic> \
#     bash /home/ubuntu/fleet-kit/scripts/member_liveness_check.sh >> .../member_liveness.cron.log 2>&1
set -uo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:?set FLEET_LOG_DIR -- the instance's log dir (host mount of the container's /var/log/fleet-kit), where fleet.db lives}"
INSTANCE="${FLEET_INSTANCE_NAME:-$(basename "$(dirname "$LOG_DIR")")}"
MAX_AGE_S="${LIVENESS_MAX_AGE_S:-10800}"
NTFY_TOPIC="${NTFY_TOPIC:-}"   # optional: fleet_alert.sh emails regardless (and falls back to anchor.env's topic)
DB="$LOG_DIR/fleet.db"
EXHAUSTED_STATE="${ACCOUNT_POOL_STATE_FILE:-$LOG_DIR/account-pool-exhausted.state}"
CHECK=member_liveness

_log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] [$INSTANCE] $*"; }

# _page <title> <body> <problem> <severity>
# Under the selftest (NTFY_CALLS_FILE set) record what the helper WOULD have received instead
# of calling it -- fleet_alert.sh runs by absolute path, so a PATH stub cannot intercept it,
# which is how the suite once emailed a human (account_health_check.sh, 2026-09-04).
_page() {
  local title="$1" body="$2" problem="$3" severity="$4"
  if [ -n "${NTFY_CALLS_FILE:-}" ]; then
    echo "page problem=$problem severity=$severity title=$title" >> "$NTFY_CALLS_FILE"
    return 0
  fi
  NTFY_TOPIC="$NTFY_TOPIC" bash "$KIT_DIR/scripts/fleet_alert.sh" \
    --check "$CHECK" --problem "$problem" --severity "$severity" --handle "$INSTANCE" "$title" "$body"
}

# _resolve <title> <body> -- closes whatever this check opened. fleet_alert.sh itself
# suppresses a recovery nobody was paged for, so calling this on every healthy tick is safe.
_resolve() {
  if [ -n "${NTFY_CALLS_FILE:-}" ]; then
    echo "resolve title=$1" >> "$NTFY_CALLS_FILE"
    return 0
  fi
  NTFY_TOPIC="$NTFY_TOPIC" bash "$KIT_DIR/scripts/fleet_alert.sh" --resolve --check "$CHECK" "$1" "$2"
}

if [ ! -f "$DB" ]; then
  _log "TRANSIENT no fleet.db at $DB -- liveness unobservable"
  _page "fleet-kit: $INSTANCE liveness unobservable (no fleet.db)" \
        "Expected $DB. Is the container up and FLEET_LOG_DIR mounted? Until it is, nothing can say whether the fleet is working." \
        no_db transient
  echo "BLIND"
  exit 0
fi

read -r NEWEST MEMBER < <(python3 - "$DB" <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    r = c.execute("select recorded_at, member from runs where status='ok' order by recorded_at desc limit 1").fetchone()
    print(f"{int(r[0])} {r[1]}" if r else "0 none")
except Exception as exc:  # noqa: BLE001 -- report, never crash the pager
    print(f"ERR {type(exc).__name__}")
PY
)

if [ "$NEWEST" = "ERR" ]; then
  _log "TRANSIENT fleet.db unreadable ($MEMBER)"
  _page "fleet-kit: $INSTANCE liveness unobservable (fleet.db unreadable: $MEMBER)" \
        "$DB exists but could not be queried. Until it can, nothing can say whether the fleet is working." \
        db_unreadable transient
  echo "BLIND"
  exit 0
fi

NOW=$(date -u +%s)
AGE=$(( NOW - NEWEST ))
AGE_H=$(( AGE / 3600 ))
NEWEST_HUMAN=$( [ "$NEWEST" -gt 0 ] && date -u -d "@$NEWEST" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || echo never )

if [ "$AGE" -le "$MAX_AGE_S" ]; then
  _log "OK newest ok run ${AGE}s ago ($MEMBER at $NEWEST_HUMAN)"
  _resolve "fleet-kit: $INSTANCE is doing work again" \
           "Newest ok run: $MEMBER at $NEWEST_HUMAN (${AGE}s ago)."
  echo "OK"
  exit 0
fi

# Silence. Is it the legitimate kind? account_pool.sh writes "<account> <reset-epoch>" per
# gated account; a reset still in the future means the pool is genuinely out of quota.
RESET_MAX=0; RESET_LIST=""
if [ -f "$EXHAUSTED_STATE" ]; then
  while read -r acct epoch _; do
    case "$epoch" in ''|*[!0-9]*) continue ;; esac
    if [ "$epoch" -gt "$NOW" ]; then
      RESET_LIST="$RESET_LIST $acct=$(date -u -d "@$epoch" '+%b %-d %H:%M UTC' 2>/dev/null || echo "$epoch")"
      [ "$epoch" -gt "$RESET_MAX" ] && RESET_MAX=$epoch
    fi
  done < "$EXHAUSTED_STATE"
fi

if [ "$RESET_MAX" -gt 0 ]; then
  RESET_HUMAN=$(date -u -d "@$RESET_MAX" '+%a %b %-d %H:%M UTC' 2>/dev/null || echo "$RESET_MAX")
  _log "PAGED out_of_tokens -- silent ${AGE_H}h, pool exhausted until $RESET_HUMAN"
  _page "fleet-kit: $INSTANCE out of tokens until $RESET_HUMAN" \
        "No member has completed work for ${AGE_H}h (newest ok: $MEMBER at $NEWEST_HUMAN). The account pool reports every account gated:$RESET_LIST. This is the legitimate kind of down -- nothing to fix, it resumes at the reset. One page, no more." \
        out_of_tokens degraded
  echo "PAGED out_of_tokens"
  exit 0
fi

_log "PAGED silent -- no ok run for ${AGE_H}h (threshold $((MAX_AGE_S/3600))h)"
_page "fleet-kit: $INSTANCE silent for ${AGE_H}h -- no member has completed work" \
      "Newest ok run: $MEMBER at $NEWEST_HUMAN. Threshold: $((MAX_AGE_S/3600))h. The account pool is NOT reporting exhaustion, so this is not a quota gap. Check, in order: podman ps (is the container up); podman exec $INSTANCE cat /etc/cron.d/fleet-kit (did the crontab render -- one invalid field discards the whole file, fleet-kit#418); $LOG_DIR/cron_watchdog.log (is the watchdog restart-looping); $LOG_DIR/runs.jsonl tail (are passes starting and dying)." \
      silent critical
echo "PAGED silent"
exit 0
