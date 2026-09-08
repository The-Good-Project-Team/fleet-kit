#!/usr/bin/env bash
# vp_due.sh -- every 15 minutes (entrypoint.sh crontab): spawn a VP review for every
# quality:world-class item whose newest merged PR is newer than its newest VP verdict.
# Deterministic on purpose: the loop that gets a spec up to par must not depend on a
# charter remembering to run it (2026-09-08: gru did not, for 2.5h after the redo merged).
# See scripts/vp_due.py for the rule; members/vp/vp.md for what the review does.
set -u
KIT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${FLEET_REPO:?set FLEET_REPO (env or fleet.env)}"
LOG_DIR="${FLEET_LOG_DIR:-/var/log/fleet-kit}"
log() { echo "[$(date -u '+%F %T UTC')] vp_due: $*"; }

out="$(python3 "$KIT/scripts/vp_due.py" --repo-dir "$REPO" 2>&1)" || { log "vp_due.py failed: ${out:0:200}"; exit 0; }
due="$(printf '%s' "$out" | python3 -c 'import sys,json; print(" ".join(str(n) for n in json.load(sys.stdin)["due"]))' 2>/dev/null)"
redo="$(printf '%s' "$out" | python3 -c 'import sys,json; print(" ".join(str(n) for n in json.load(sys.stdin).get("redo", [])))' 2>/dev/null)"
if [ -z "$due" ] && [ -z "$redo" ]; then
  log "nothing due"
  exit 0
fi
# setsid + nohup: the pass must outlive this cron tick and never die with a parent (a minion
# spawned from inside a vp pass was killed the minute vp ended, 2026-09-08 16:53Z).
for n in $due; do
  log "spawning vp --item $n"
  FLEET_RUN_NOW=1 setsid nohup bash "$KIT/scripts/run_member.sh" vp --item "$n" >> "$LOG_DIR/vp.log" 2>&1 < /dev/null &
done
for n in $redo; do
  log "spawning redo minion --item $n (Not yet verdict is the spec)"
  FLEET_RUN_NOW=1 setsid nohup bash "$KIT/scripts/run_member.sh" minion --item "$n" >> "$LOG_DIR/minion.log" 2>&1 < /dev/null &
done
