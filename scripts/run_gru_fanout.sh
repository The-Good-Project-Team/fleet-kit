#!/bin/bash
# run_gru_fanout.sh — cron's entry point for gru, in place of a single run_member.sh call.
# Computes N via fanout.py (headroom -> Fibonacci ladder), then runs N gru passes in parallel,
# each independently claiming its own backlog item (gru.md's own concurrency contract already
# assumes "one of possibly several concurrent instances").
#
# WHY THIS EXISTS: fanout.py had real sizing logic (n_from_headroom) but nothing in the kit
# ever called it -- cron fired exactly one run_member.sh gru per hour, hardcoded, regardless of
# how much headroom was actually available. Found live on dino, 2026-08-21.
#
# HEADROOM: fleet-kit has no real weekly-usage/rate-limit API wired in (the source fleet's
# "maxx" budget-verdict service is a documented extension point account_pool.sh drops on
# purpose -- see that script's header). Until you wire a real one, FLEET_GRU_HEADROOM_USD is a
# conservative fixed dollar assumption, override it in fleet.env once you have a real number
# to plan against. Both sides of fanout.py's division must be the SAME unit -- dollars here,
# matched against gru's own real per-pass dollar cost below, never a token count against a
# dollar figure.
set -uo pipefail

[ -f "${FLEET_ENV_FILE:-./fleet.env}" ] && . "${FLEET_ENV_FILE:-./fleet.env}"
KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/gru.log"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

HEADROOM_USD="${FLEET_GRU_HEADROOM_USD:-50}"

# Real spend-per-build from this box's own run history (fleet_db.py's own accounting, not an
# estimate) -- falls back to gru's own configured max_budget_usd if there's no run history yet
# (first-ever pass, fresh box). Both in dollars, same unit as HEADROOM_USD above.
AVG_COST=$(python3 "$KIT_DIR/scripts/fleet_db.py" spend --member gru --hours 168 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['avg_cost'] if d and d[0].get('avg_cost') else '')" 2>/dev/null)
if [ -z "$AVG_COST" ] || [ "$AVG_COST" = "None" ]; then
  AVG_COST=$(python3 -c "
import sys; sys.path.insert(0, '$KIT_DIR/scripts')
import member_spec
print(member_spec.by_name('gru')['mandate']['limits'].get('max_budget_usd', 5))
")
fi

N=$(python3 "$KIT_DIR/scripts/fanout.py" "$HEADROOM_USD" "$AVG_COST" 1)
RC=$?
if [ "$RC" -ne 0 ] || [ -z "$N" ]; then
  log "fanout: FAILED to compute N (rc=$RC) -- falling back to N=1"
  N=1
fi

log "fanout: N=$N (headroom=\$$HEADROOM_USD avg_cost=\$$AVG_COST)"

PIDS=()
for i in $(seq 1 "$N"); do
  bash "$KIT_DIR/scripts/run_member.sh" gru &
  PIDS+=($!)
done
FAIL=0
for pid in "${PIDS[@]}"; do
  wait "$pid" || FAIL=$((FAIL + 1))
done
log "fanout: spawned $N gru instance(s) this pass, $FAIL failed"
exit 0
