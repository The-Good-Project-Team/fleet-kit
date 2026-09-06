#!/bin/bash
# member_liveness_check.sh -- pages when NO fleet member has done any work within a tolerance
# derived from the slowest scheduled member's own cron cadence (gh#419).
#
# WHY THIS EXISTS: the 2026-09-03 21:00 -> 2026-09-05 12:45 outage (root cause: an invalid
# FLEET_GRU_CADENCE hour field made Vixie cron discard /etc/cron.d/fleet-kit whole, fixed in
# gh#418) ran for ~40h with every existing health signal green the entire time --
# account/tunnel/path-health watch things entrypoint starts directly (unaffected by cron
# dying), deploy_staleness_check.sh runs from the HOST crontab (also unaffected), and the
# in-container cron watchdog's own canary lives INSIDE the crontab file that got discarded, so
# it could never tell "cron is wedged" from "cron discarded this file" -- it just kill -9'd a
# live cron 500+ times against a file cron was never going to reload. Nothing asked the one
# question that actually mattered: did any member do work recently?
#
# WHY PLAIN BASH, HOST-SIDE, ZERO CLAUDE CODE DEPENDENCY: same reasoning
# account_health_check.sh's own header gives -- the failure this watches for (the whole fleet
# doing nothing) could itself take down any in-container checker. It must also run from the
# HOST crontab (schedulers/), never entrypoint.sh's in-container one -- that container crontab
# is the exact file this issue's outage discarded, so a copy of this check living inside it
# would go dark in precisely the scenario it exists to catch.
#
# WHAT IT WATCHES: the newest "ts" across runs.jsonl (written by run_member.sh for every
# scheduled member -- both a "started" row before claude -p even runs and a completion row
# after, see run_report.py), with the newest mtime among each scheduled member's own
# $LOG_DIR/<member>.log as a fallback signal for whenever runs.jsonl itself is missing or
# reads stale (an empty/fresh box, or the jsonl write path itself broken while cron still
# ticks and redirects stdout into the per-member log). Per this issue's own PRD, "recommend
# runs.jsonl with log-mtime as a fallback ... but this wasn't resolved from the repo" -- this
# takes that recommendation, not a hand-picked alternative.
#
# TOLERANCE: MEMBER_LIVENESS_TOLERANCE_MINUTES defaults to 840 (14h) = 2x dumbledore's 7h
# cadence (entrypoint.sh: "13 1,8,15,22 * * *"), the slowest of the 9 ALL_CRON_MEMBERS entries
# as of 2026-09-06. Documented invariant (issue AC2): this must stay >= 2x the slowest
# configured member cadence, or a healthy slow-cadence member on its own normal schedule would
# false-page. scripts/selftest.py re-derives the slowest cadence directly from entrypoint.sh's
# own crontab-generation block and asserts this default still satisfies that invariant, so a
# future cadence change that breaks it fails loud instead of drifting quiet.
#
# NON-GOAL (per this issue's PRD): this is the fleet-wide "is anything happening at all"
# signal, not a per-member staleness pager -- it never names which member is quiet, only
# whether every one of them has gone silent together. Per-member staleness is #220's
# dashboard-facing concern.
#
# STATE: one marker file, same shape/reasoning as account_health_check.sh's own STATE_FILE --
# pages once per outage, once on recovery, and re-pages on a fixed cadence while the outage
# stays open (gh#266's fix, same class of bug: a pager that goes silent for the rest of a
# multi-hour+ outage is indistinguishable from a dead pager).
#
# Usage (HOST crontab, mirrors account_health_check.sh's own invocation shape):
#   */5 * * * * FLEET_LOG_DIR=/home/ubuntu/fleet-kit-logs NTFY_TOPIC=<topic> \
#     bash scripts/member_liveness_check.sh >> .../member_liveness_check.cron.log 2>&1
set -uo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:?set FLEET_LOG_DIR -- same dir run_member.sh writes runs.jsonl/<member>.log into}"
RUNS_FILE="$LOG_DIR/runs.jsonl"
NTFY_TOPIC="${NTFY_TOPIC:-}"   # optional: fleet_alert.sh emails regardless
TOLERANCE_MINUTES="${MEMBER_LIVENESS_TOLERANCE_MINUTES:-840}"
REPAGE_MINUTES="${MEMBER_LIVENESS_REPAGE_MINUTES:-120}"
STATE_FILE="${MEMBER_LIVENESS_STATE_FILE:-$LOG_DIR/.member_liveness_paged.state}"
# Same class of guard as account_health_check.sh's own MAX_PLAUSIBLE_OUTAGE_MINUTES -- a
# timestamp this old cannot be a real outage, only a synthetic/corrupt one (a selftest
# fixture, a clock jump, a hand-edited state file), and a pager that cries wolf from its own
# test suite is worse than no pager at all.
MAX_PLAUSIBLE_MINUTES="${MEMBER_LIVENESS_MAX_PLAUSIBLE_MINUTES:-10080}"  # 7 days

# Mirrors entrypoint.sh's own ALL_CRON_MEMBERS array -- used ONLY to compute the log-mtime
# fallback signal below (see header). scripts/selftest.py asserts the two lists stay in sync,
# same reasoning as the tolerance drift-guard above: a future member added/removed from
# entrypoint.sh's crontab must be reflected here too, not drift silently apart.
ALL_CRON_MEMBERS=(the-fixer judge-judy gru jefe roomba marie datta dumbledore sentry)

_ntfy() {
  # mode "page" (default) records a NEW critical in alert_store; "resolve" closes the alarm a
  # prior "page" opened; "repage" bypasses alert_store's own dedupe so a still-open outage
  # keeps paging instead of going silent (same three-mode shape as account_health_check.sh's
  # own _ntfy, and the same reasoning -- see that script's header for the gh#266 incident).
  local title="$1" msg="$2" mode="${3:-page}"
  if [ -n "${NTFY_CALLS_FILE:-}" ]; then
    if [ -n "${NTFY_TOPIC:-}" ]; then
      curl -s -H "Title: $title" -d "$msg" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1
    fi
    return 0
  fi
  case "$mode" in
    resolve)
      bash "$KIT_DIR/scripts/fleet_alert.sh" --resolve --check member_liveness "$title" "$msg" ;;
    repage)
      bash "$KIT_DIR/scripts/fleet_alert.sh" "$title" "$msg" ;;
    *)
      bash "$KIT_DIR/scripts/fleet_alert.sh" \
        --check member_liveness --problem no_work --severity critical "$title" "$msg" ;;
  esac || echo "[alert] fleet_alert.sh failed" >&2
}

now_epoch=$(date +%s)

# Primary signal: newest "ts" among runs.jsonl's lines. Scans the last 200 lines rather than
# just the last one -- concurrent minions/nerds can finish (and append) out of the order they
# started in, so the newest line BY FILE POSITION is not always the newest by timestamp; a
# single dropped-order line would otherwise read as a false liveness gap.
runs_ts=""
if [ -f "$RUNS_FILE" ]; then
  runs_ts=$(tail -n 200 "$RUNS_FILE" | python3 -c '
import json, sys
best = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        rec = json.loads(line)
    except ValueError:
        continue
    ts = rec.get("ts")
    if isinstance(ts, (int, float)) and (best is None or ts > best):
        best = ts
print(int(best) if best is not None else "")
' 2>/dev/null)
fi

# Fallback signal: newest mtime among each scheduled member's own log file (see header).
log_mtime=""
for m in "${ALL_CRON_MEMBERS[@]}"; do
  f="$LOG_DIR/$m.log"
  [ -f "$f" ] || continue
  mt=$(stat -c%Y "$f" 2>/dev/null || stat -f%m "$f" 2>/dev/null) || continue
  [ -n "$mt" ] || continue
  if [ -z "$log_mtime" ] || [ "$mt" -gt "$log_mtime" ]; then
    log_mtime="$mt"
  fi
done

last_activity_epoch=""
signal_source=""
if [[ "$runs_ts" =~ ^[0-9]+$ ]]; then
  last_activity_epoch="$runs_ts"
  signal_source="runs.jsonl"
fi
if [[ "$log_mtime" =~ ^[0-9]+$ ]] && { [ -z "$last_activity_epoch" ] || [ "$log_mtime" -gt "$last_activity_epoch" ]; }; then
  last_activity_epoch="$log_mtime"
  signal_source="member log mtime"
fi

if [ -z "$last_activity_epoch" ]; then
  echo "[member_liveness_check] no runs.jsonl and no member log found under $LOG_DIR yet -- nothing to check"
  exit 0
fi

age_minutes=$(( (now_epoch - last_activity_epoch) / 60 ))

# STATE_FILE's first line is the ORIGINAL page time (raw epoch seconds, not a formatted
# string -- avoids needing a GNU/BSD `date` parser to read it back, same reasoning
# sync_health_check.sh's own state file documents), a second line `last_repage=<epoch>` is
# added once re-pages start.
already_paged_epoch=""
last_repage_epoch=""
if [ -f "$STATE_FILE" ]; then
  _repage_line=""
  { read -r already_paged_epoch; read -r _repage_line; } < "$STATE_FILE" || true
  last_repage_epoch="${_repage_line#last_repage=}"
fi

if [ "$age_minutes" -gt "$MAX_PLAUSIBLE_MINUTES" ]; then
  echo "[member_liveness_check] implausible age ${age_minutes}m (>${MAX_PLAUSIBLE_MINUTES}m, source=$signal_source)" \
       "-- treating this as a synthetic/corrupt timestamp rather than paging"
  exit 0
fi

if [ "$age_minutes" -lt "$TOLERANCE_MINUTES" ]; then
  if [ -n "$already_paged_epoch" ]; then
    _ntfy "fleet-kit: member activity recovered" \
      "runs.jsonl/member logs show fresh activity again (${age_minutes}m old, source=$signal_source) after an outage flagged at $(date -u -d "@$already_paged_epoch" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || date -u -r "$already_paged_epoch" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || echo "epoch $already_paged_epoch")." \
      "resolve"
    rm -f "$STATE_FILE"
  fi
  echo "[member_liveness_check] healthy -- most recent member activity ${age_minutes}m ago (threshold ${TOLERANCE_MINUTES}m, source=$signal_source)"
  exit 0
fi

# age_minutes >= TOLERANCE_MINUTES: no member has done any work in at least that long.
if [ -z "$already_paged_epoch" ]; then
  _ntfy "🚨 fleet-kit: NO member has done any work in ${age_minutes}+ minutes" \
    "Newest member activity (${signal_source}) is ${age_minutes}+ minutes old, past the ${TOLERANCE_MINUTES}m tolerance (2x the slowest scheduled member's own cadence). Every account/tunnel/path/sync-health check can stay green during this -- none of them watch whether a member actually ran. Check whether the fleet's crontab was discarded (gh#419/gh#418: an invalid cron field can make Vixie cron drop /etc/cron.d/fleet-kit whole) before assuming it's a slow day." \
    "urgent"
  echo "$now_epoch" > "$STATE_FILE"
  echo "[member_liveness_check] PAGED -- no member activity in ${age_minutes}m (threshold ${TOLERANCE_MINUTES}m, source=$signal_source)"
else
  reference_epoch="${last_repage_epoch:-$already_paged_epoch}"
  since_last_page_minutes=999999
  [[ "$reference_epoch" =~ ^[0-9]+$ ]] && since_last_page_minutes=$(( (now_epoch - reference_epoch) / 60 ))

  if [ "$since_last_page_minutes" -ge "$REPAGE_MINUTES" ]; then
    _ntfy "🚨🚨 fleet-kit: fleet STILL silent (re-page)" \
      "Still no member work, ${age_minutes}+ minutes now (source=$signal_source, threshold ${TOLERANCE_MINUTES}m). This is a re-page -- the outage has not resolved since it was first flagged." \
      "repage"
    printf '%s\nlast_repage=%s\n' "$already_paged_epoch" "$now_epoch" > "$STATE_FILE"
    echo "[member_liveness_check] RE-PAGED -- outage still open (${age_minutes}m), next re-page in ${REPAGE_MINUTES}m"
  else
    echo "[member_liveness_check] no member activity in ${age_minutes}m (threshold ${TOLERANCE_MINUTES}m), already paged (next re-page in $(( REPAGE_MINUTES - since_last_page_minutes ))m)"
  fi
fi
