#!/bin/bash
# sync_health_check.sh -- pages when fleet_view_server.py's tail_runs_forever sync loop has
# fallen behind runs.jsonl for a sustained stretch (gh#273).
#
# WHY THIS EXISTS: tail_runs_forever is the ONLY thing keeping fleet.db in sync with
# runs.jsonl. Three personas read fleet.db directly for real decisions and never go through
# the HTTP API: nerd's dedup history (nerd.md:70-74), gru's allowance calibration
# (gru.md:185), dumbledore's prediction-accuracy history (dumbledore.md:135,169). If that
# thread dies, each of them keeps getting a normal-looking, silently-truncated result set with
# zero error signal -- exactly the "fleet that looks busy" failure class this kit exists to
# catch (README.md:31-32). Nothing else watches for it: account_health_check.sh,
# tunnel_health_check.sh and path_health_check.sh watch the account pool, tunnel reachability,
# and per-instance dashboard routing -- none of them ever look at fleet.db's own freshness.
#
# WHAT IT WATCHES: sync_state.offset (fleet.db) against runs.jsonl's current on-disk byte
# size. Read via `fleet_db.py offset` -- a read-only accessor that does NOT call sync() itself
# (calling sync() from the watchdog would close the very gap it exists to detect, and this
# issue's own PRD is explicit that fleet_db.sync()'s logic is not to be touched here). A gap
# can only open while runs.jsonl is actively growing -- an idle fleet with no new runs stays
# caught up by construction, so a persistent gap can only mean the sync loop itself stopped
# ticking, never idle time.
#
# STATE: two marker files, same shape as account_health_check.sh -- one remembers when the
# CURRENT gap was first observed (offset != size), so a momentary gap between two 2-second
# sync ticks can't false-page; the other remembers whether we already paged for it (and, once
# a re-page has fired, when that was) so a 5-minute cron doesn't re-page every tick. Both store
# a raw epoch second, not a formatted string -- account_health_check.sh's own state file needs
# a GNU/BSD `date` parser to turn its formatted timestamp back into an epoch for arithmetic;
# storing the epoch directly here avoids needing that parser at all, and formats to
# human-readable only when building an alert message.
#
# RE-PAGES on a fixed interval while the gap stays open, same fix and same reasoning as
# account_health_check.sh's own gh#266: paging once and then going silent for the rest of a
# multi-hour outage is indistinguishable from a dead pager at a glance. SYNC_HEALTH_REPAGE_MINUTES
# defaults to 120, matching ACCOUNT_HEALTH_REPAGE_MINUTES's own default -- no data in this repo
# argues for this class of pager needing a different cadence.
#
# THRESHOLD: UNKNOWN per this issue's own PRD comment -- "the staleness threshold (minutes)
# before paging" is an open question marie flagged as unresolved from the repo (the sync loop
# ticks every 2s, so this is a much faster-onset failure than account_health_check.sh's
# 30-minute account-outage threshold, but no number is proposed anywhere in the issue either).
# SYNC_HEALTH_THRESHOLD_MINUTES defaults to 15 below as a conservative placeholder -- long
# enough that a slow tick or a momentary sqlite lock can't false-page, short enough to catch a
# dead thread same-day -- but this default is NOT a resolved human decision, only a starting
# point; an operator should tune it (see gh#273's own open-questions section).
#
# Usage (cron, mirrors account_health_check.sh's own invocation shape):
#   */5 * * * * FLEET_LOG_DIR=/home/ubuntu/fleet-kit-logs NTFY_TOPIC=<topic> \
#     bash scripts/sync_health_check.sh >> .../sync_health_check.log 2>&1
set -uo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:?set FLEET_LOG_DIR -- same dir fleet_view_server.py writes fleet.db/runs.jsonl into}"
RUNS_FILE="$LOG_DIR/runs.jsonl"
NTFY_TOPIC="${NTFY_TOPIC:-}"   # optional: fleet_alert.sh emails regardless
THRESHOLD_MINUTES="${SYNC_HEALTH_THRESHOLD_MINUTES:-15}"
REPAGE_MINUTES="${SYNC_HEALTH_REPAGE_MINUTES:-120}"
GAP_STATE_FILE="${SYNC_HEALTH_GAP_STATE_FILE:-$LOG_DIR/.sync_health_gap_since.state}"
PAGED_STATE_FILE="${SYNC_HEALTH_PAGED_STATE_FILE:-$LOG_DIR/.sync_health_paged.state}"
# account_health_check.sh added this guard after a 2020-01-01 selftest fixture leaked a
# synthetic timestamp into a real check and sent a REAL page claiming a 6.7-year outage
# (confirmed live 2026-09-04, see that script's own header). GAP_STATE_FILE here is an
# epoch this script itself writes, but the same class of corruption -- a stale fixture, a
# clock jump, a hand-edited state file -- is just as possible, so the same bound applies.
MAX_PLAUSIBLE_GAP_MINUTES="${SYNC_HEALTH_MAX_PLAUSIBLE_MINUTES:-10080}"  # 7 days

[ -f "$RUNS_FILE" ] || { echo "[sync_health_check] no runs.jsonl at $RUNS_FILE yet -- nothing to check"; exit 0; }

_ntfy() {
  local title="$1" msg="$2" mode="${3:-page}"
  # Under the selftest, send through the stubbed `curl` on PATH instead of the real helper --
  # fleet_alert.sh runs by ABSOLUTE path, so a PATH stub cannot intercept it (same reasoning
  # account_health_check.sh's own _ntfy documents).
  if [ -n "${NTFY_CALLS_FILE:-}" ]; then
    if [ -n "${NTFY_TOPIC:-}" ]; then
      curl -s -H "Title: $title" -d "$msg" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1
    fi
    return 0
  fi
  case "$mode" in
    resolve)
      bash "$KIT_DIR/scripts/fleet_alert.sh" --resolve --check sync_health "$title" "$msg" ;;
    repage)
      # Bypasses alert_store's --check/--severity gate on purpose, same as
      # account_health_check.sh's own "repage" mode: the FIRST page already recorded this
      # (check, problem) key as an open critical, and alert_store's own dedupe would silently
      # suppress every later call for that key until it resolves -- which is exactly the
      # silent-for-the-rest-of-the-outage bug gh#266 fixed for account_health_check.sh.
      bash "$KIT_DIR/scripts/fleet_alert.sh" "$title" "$msg" ;;
    *)
      bash "$KIT_DIR/scripts/fleet_alert.sh" \
        --check sync_health --problem offset_gap --severity critical "$title" "$msg" ;;
  esac || echo "[alert] fleet_alert.sh failed" >&2
}

offset=$(FLEET_LOG_DIR="$LOG_DIR" python3 "$KIT_DIR/scripts/fleet_db.py" offset 2>/dev/null)
if ! [[ "$offset" =~ ^[0-9]+$ ]]; then
  echo "[sync_health_check] could not read sync_state.offset (fleet_db.py offset returned '$offset') -- fleet.db may not exist yet"
  exit 0
fi

size=$(stat -c%s "$RUNS_FILE" 2>/dev/null || stat -f%z "$RUNS_FILE" 2>/dev/null)
if [ -z "$size" ]; then
  echo "[sync_health_check] could not stat $RUNS_FILE"
  exit 0
fi

# PAGED_STATE_FILE's first line is the ORIGINAL page epoch, written once and never touched
# again; a second line, `last_repage=<epoch>`, is added/updated only once re-pages start (same
# two-line shape account_health_check.sh's gh#266 fix uses -- one state file, not a new store).
already_paged_epoch=""
last_repage_epoch=""
if [ -f "$PAGED_STATE_FILE" ]; then
  _repage_line=""
  { read -r already_paged_epoch; read -r _repage_line; } < "$PAGED_STATE_FILE" || true
  last_repage_epoch="${_repage_line#last_repage=}"
fi

if [ "$offset" -ge "$size" ]; then
  # Caught up. (offset > size only across a rotation the sync loop hasn't noticed yet --
  # self-corrects within one 2s tick, per fleet_db.sync()'s own truncation handling, so this
  # is never worth a gap timer.)
  rm -f "$GAP_STATE_FILE"
  if [ -n "$already_paged_epoch" ]; then
    _ntfy "fleet-kit: fleet.db sync recovered" \
      "sync_state.offset is caught up with runs.jsonl again after a gap flagged at $(date -u -d "@$already_paged_epoch" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || date -u -r "$already_paged_epoch" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || echo "epoch $already_paged_epoch")." \
      "resolve"
    rm -f "$PAGED_STATE_FILE"
  fi
  echo "[sync_health_check] healthy -- offset=$offset size=$size"
  exit 0
fi

# offset < size: a real gap. Track how long it's been open -- the newest observation alone
# (like account_health_check.sh's original "youngest failure line" bug) would always read as
# "just now" and never cross a threshold.
now_epoch=$(date +%s)
gap_since=""
[ -f "$GAP_STATE_FILE" ] && gap_since=$(cat "$GAP_STATE_FILE")
if ! [[ "$gap_since" =~ ^[0-9]+$ ]]; then
  echo "$now_epoch" > "$GAP_STATE_FILE"
  gap_since="$now_epoch"
fi

age_minutes=$(( (now_epoch - gap_since) / 60 ))

if [ "$age_minutes" -gt "$MAX_PLAUSIBLE_GAP_MINUTES" ]; then
  echo "[sync_health_check] implausible gap age ${age_minutes}m (>${MAX_PLAUSIBLE_GAP_MINUTES}m)" \
       "-- treating $GAP_STATE_FILE as corrupt/synthetic rather than paging; resetting it"
  echo "$now_epoch" > "$GAP_STATE_FILE"
  exit 0
fi

gap_bytes=$(( size - offset ))

if [ "$age_minutes" -ge "$THRESHOLD_MINUTES" ] && [ -z "$already_paged_epoch" ]; then
  _ntfy "🚨 fleet-kit: fleet.db sync stalled" \
    "sync_state.offset ($offset) has trailed runs.jsonl's size ($size, gap ${gap_bytes} bytes) for ${age_minutes}+ minutes (threshold ${THRESHOLD_MINUTES}m). tail_runs_forever has likely either died or is stuck retrying the same failing sync tick -- nerd/gru/dumbledore are all reading a silently-truncated fleet.db. Check fleet_view.log: a single repeating traceback means restarting fleet_view_server.py alone won't fix it (the same record will fail again); no recent traceback there means the thread is simply dead and a restart is enough." \
    "urgent"
  echo "$now_epoch" > "$PAGED_STATE_FILE"
  echo "[sync_health_check] PAGED -- gap has persisted ${age_minutes}m (offset=$offset size=$size)"
elif [ "$age_minutes" -ge "$THRESHOLD_MINUTES" ] && [ -n "$already_paged_epoch" ]; then
  # Gap still open and already paged once -- re-page every REPAGE_MINUTES instead of staying
  # silent for the rest of the outage (gh#266's fix, mirrored here).
  reference_epoch="${last_repage_epoch:-$already_paged_epoch}"
  since_last_page_minutes=999999
  [[ "$reference_epoch" =~ ^[0-9]+$ ]] && since_last_page_minutes=$(( (now_epoch - reference_epoch) / 60 ))

  if [ "$since_last_page_minutes" -ge "$REPAGE_MINUTES" ]; then
    _ntfy "🚨🚨 fleet-kit: fleet.db sync STILL stalled (re-page)" \
      "Still stalled, ${age_minutes}m since the gap opened -- sync_state.offset ($offset) trails runs.jsonl's size ($size, gap ${gap_bytes} bytes). Check fleet_view.log for a repeating traceback before assuming a plain restart will fix it." \
      "repage"
    printf '%s\nlast_repage=%s\n' "$already_paged_epoch" "$now_epoch" > "$PAGED_STATE_FILE"
    echo "[sync_health_check] RE-PAGED -- gap still open (${age_minutes}m), next re-page in ${REPAGE_MINUTES}m"
  else
    echo "[sync_health_check] gap open ${age_minutes}m (threshold ${THRESHOLD_MINUTES}m, offset=$offset size=$size), already paged (next re-page in $(( REPAGE_MINUTES - since_last_page_minutes ))m)"
  fi
else
  echo "[sync_health_check] gap open ${age_minutes}m (threshold ${THRESHOLD_MINUTES}m, offset=$offset size=$size)"
fi
