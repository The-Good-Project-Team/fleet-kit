#!/bin/bash
# account_readiness.sh -- how many pool accounts are actually usable RIGHT NOW, before
# committing to a claim+spawn cycle that account_pool.sh would otherwise only discover is
# doomed AFTER the minion is already spawned and burning turns.
#
# WHY THIS EXISTS: gru already reads budget PACING (maxx_reader.py, fleet_db.py spend) in its
# charter's step 1, but never checked account AUTH STATE before spawning -- confirmed live,
# 2026-08-25: multiple real gru passes claimed items, spawned minions, and only found out via
# ALL_ACCOUNTS_EXHAUSTED (account_pool.sh exit 3) after the fact, wasting the claim + the
# minion's spin-up turns on work that was never going to run. Gru's own self-critique named
# this exact gap: "no visibility into account_pool.sh's live state before spawning... no
# evidence a pre-check tool exists in my allowlist to have caught this earlier."
#
# WHAT IT CHECKS: account_pool.sh's own exhaustion-gate state file (the ground truth the pool
# itself already maintains, see account_pool.sh's _account_pool_budget_verdict) -- cheap, no
# API call, just a file read. Prints how many of FLEET_ACCOUNTS are NOT currently gated, so
# gru can size N against real spawn capacity instead of budget alone.
#
# Usage: bash account_readiness.sh
# Output (stdout, one line, machine-parseable): "ready=<N> total=<N> gated=<name,name,...>"
set -uo pipefail

ACCOUNTS="${FLEET_ACCOUNTS:-primary}"
STATE_FILE="${ACCOUNT_POOL_STATE_FILE:-${FLEET_LOG_DIR:-/var/log/fleet-kit}/account-pool-exhausted.state}"

now=$(date +%s)
ready=0
total=0
gated_names=()

for acct in $ACCOUNTS; do
  total=$((total + 1))
  is_gated=0
  if [ -f "$STATE_FILE" ]; then
    epoch=$(awk -v a="$acct" '$1==a{print $2}' "$STATE_FILE" | tail -1)
    if [ -n "$epoch" ] && [ "$epoch" -gt "$now" ]; then
      is_gated=1
    fi
  fi
  if [ "$is_gated" -eq 1 ]; then
    gated_names+=("$acct")
  else
    ready=$((ready + 1))
  fi
done

gated_str=$(IFS=,; echo "${gated_names[*]:-}")
echo "ready=$ready total=$total gated=$gated_str"

# Exit 0 if at least one account is ready, 1 if the whole pool is currently gated -- lets a
# caller do `account_readiness.sh || echo "skip this pass"` without parsing the line.
[ "$ready" -gt 0 ]
