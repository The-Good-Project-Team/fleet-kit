#!/bin/bash
# account_health_check.sh -- page a human when EVERY fleet account has been failing for a
# sustained stretch, independent of any LLM member or the accounts themselves.
#
# WHY THIS CAN'T BE AN LLM MEMBER (like dont-shoot-the-messenger): the failure mode this
# watches for IS "every account is dead" -- an LLM pass to report that would itself run
# through the same dead account pool and silently not run. This must be plain bash, host-side
# cron, zero Claude Code dependency -- same reasoning auto_deploy.sh already documents for why
# IT is a host poll and not a webhook.
#
# WHAT IT WATCHES: account-pool.log's "ALL accounts in '...' failed this call" line (written
# by account_pool.sh once per tick where every account failed). Logic: look at the most recent
# line in the log, period. If it's a failure line AND it's older than THRESHOLD_MINUTES, the
# pool has been down continuously since at least then with nothing succeeding since (a success
# would be the newest line instead -- account_pool_run's caller always logs pass end rc=0
# right after a successful call returns, and log lines are append-only in time order).
#
# CONFIRMED LIVE 2026-08-24/25: both fleet accounts died silently, ticking every ~5min for
# hours, nobody paged until a human noticed a screenshot didn't match the log's story. This
# closes that gap.
#
# STATE: one marker file remembers whether we already paged for the CURRENT outage, so a
# 5-minute cron doesn't re-page every tick -- one page per outage, one recovery page after.
#
# Usage (cron, mirrors auto_deploy.sh's own invocation shape):
#   */5 * * * * FLEET_LOG_DIR=/home/ubuntu/fleet-kit-logs NTFY_TOPIC=<topic> \
#     bash scripts/account_health_check.sh >> .../health_check.cron.log 2>&1
set -uo pipefail

LOG_DIR="${FLEET_LOG_DIR:?set FLEET_LOG_DIR -- same dir account_pool.sh writes account-pool.log into}"
POOL_LOG="$LOG_DIR/account-pool.log"
NTFY_TOPIC="${NTFY_TOPIC:?set NTFY_TOPIC -- the ntfy.sh topic to page}"
THRESHOLD_MINUTES="${ACCOUNT_HEALTH_THRESHOLD_MINUTES:-30}"
STATE_FILE="${ACCOUNT_HEALTH_STATE_FILE:-$LOG_DIR/.account_health_paged.state}"

[ -f "$POOL_LOG" ] || { echo "[account_health_check] no pool log at $POOL_LOG yet -- nothing to check"; exit 0; }

last_line=$(tail -1 "$POOL_LOG")
already_paged=""
[ -f "$STATE_FILE" ] && already_paged=$(cat "$STATE_FILE")

_ntfy() {
  local title="$1" msg="$2" priority="$3"
  curl -sf -o /dev/null \
    -H "Title: $title" -H "Priority: $priority" -H "Tags: warning" \
    -d "$msg" "https://ntfy.sh/$NTFY_TOPIC" \
    || echo "[account_health_check] WARNING: ntfy POST failed, could not page"
}

if [[ "$last_line" != *"ALL accounts in"*"failed this call"* ]]; then
  # newest line in the log is not a failure -- pool is healthy (or has never failed).
  if [ -n "$already_paged" ]; then
    _ntfy "fleet-kit: accounts recovered" \
      "Fleet account pool is succeeding again after an outage flagged at $already_paged." \
      "default"
    rm -f "$STATE_FILE"
  fi
  echo "[account_health_check] healthy -- newest pool-log line is not a failure"
  exit 0
fi

# Newest line IS a failure. How old is it? If it's older than the threshold, nothing has
# succeeded in at least that long -- a sustained outage, not a blip.
line_ts=$(grep -oE '^\[[0-9-]+ [0-9:]+' <<<"$last_line" | tr -d '[')
line_epoch=$(date -u -d "$line_ts" +%s 2>/dev/null)
now_epoch=$(date +%s)

if [ -z "$line_epoch" ]; then
  echo "[account_health_check] WARNING: could not parse timestamp from last line: $last_line"
  exit 0
fi

age_minutes=$(( (now_epoch - line_epoch) / 60 ))

if [ "$age_minutes" -ge "$THRESHOLD_MINUTES" ] && [ -z "$already_paged" ]; then
  paged_at="$(date -u '+%Y-%m-%d %H:%M UTC')"
  _ntfy "🚨 fleet-kit: ALL accounts exhausted" \
    "No fleet account has succeeded in ${age_minutes}+ minutes (threshold ${THRESHOLD_MINUTES}m). Last pool-log line: $last_line" \
    "urgent"
  echo "$paged_at" > "$STATE_FILE"
  echo "[account_health_check] PAGED -- last success was ${age_minutes}m ago"
else
  echo "[account_health_check] failing but only ${age_minutes}m old (threshold ${THRESHOLD_MINUTES}m), or already paged"
fi
