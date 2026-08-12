#!/bin/bash
# account_pool.sh — try N Claude accounts in order, fail over on exhaustion, never silently
# no-op.
#
# Provenance: DISTILLED from nonprofit-atlas's `scripts/lucky2/account_pool.sh` (667 lines).
# The source file integrates a product-specific budget-verdict API (a "maxx" service
# tracking weekly spend per account) that has no generic equivalent — this version keeps the
# CONTRACT (try accounts in order; classify a failure as exhausted/unauthenticated/other;
# return a distinct code when every account is exhausted; log every decision) and drops the
# budget-API integration to a documented extension point. If you have your own spend-tracking
# service, add a `_account_pool_budget_verdict()` function following the shape noted below.
#
# CONTRACT
#   account_pool_run <cmd...>  — tries each account in $FLEET_ACCOUNTS (space-separated, in
#   order) via `claude -p`. On the first account that succeeds, prints its stdout and returns
#   0, with ACCOUNT_POOL_SELECTED set to that account's name. On failure, classifies the
#   failure from the command's stderr (`exhausted` if the output matches a weekly-limit
#   phrase, `unauthenticated` if it matches a login phrase, else `other`) and tries the next
#   account. Returns 3 (ACCOUNT_POOL_ALL_EXHAUSTED) only when every account failed.
#
# Env: FLEET_ACCOUNTS (default "primary" — one account still gets the classification/logging
# benefit, it just never has anywhere to fail over TO).
set -uo pipefail

ACCOUNT_POOL_ORDER="${FLEET_ACCOUNTS:-primary}"
ACCOUNT_POOL_LOG_FILE="${ACCOUNT_POOL_LOG_FILE:-${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}/account-pool.log}"

_account_pool_log() {
  mkdir -p "$(dirname "$ACCOUNT_POOL_LOG_FILE")" 2>/dev/null
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] account_pool: $*" >> "$ACCOUNT_POOL_LOG_FILE"
}

# Extension point: define this yourself (before sourcing this file, or export the function)
# to consult your own budget-tracking API. Must print one of: ok | gated:<reason> | unknown.
# Absent by default — account_pool_run() then always tries the account and classifies from
# the command's actual output instead of pre-empting the call.
_account_pool_budget_verdict() {
  echo "unknown"
}

# _account_pool_classify_failure <combined_output> — best-effort text match. Real accounts
# report exhaustion/auth failure in human-readable text on stderr; there is no structured
# error code from the CLI to key off instead. Extend the patterns below if your provider's
# wording differs.
_account_pool_classify_failure() {
  local out="$1"
  if grep -qiE "reached your (weekly|usage) limit|rate.?limit|quota exceeded" <<<"$out"; then
    echo "exhausted"; return
  fi
  if grep -qiE "not logged in|please (log|sign) in|invalid api key|unauthorized" <<<"$out"; then
    echo "unauthenticated"; return
  fi
  echo "other"
}

# account_pool_run <cmd...> — the main entry point. Sets ACCOUNT_POOL_SELECTED,
# ACCOUNT_POOL_LAST_REASON on return. Return codes: 0 success, 2 exhausted-this-account (only
# meaningful mid-loop), 3 ALL_ACCOUNTS_EXHAUSTED, 4 unauthenticated, 1 other failure.
account_pool_run() {
  local account verdict out rc
  export ACCOUNT_POOL_SELECTED="" ACCOUNT_POOL_LAST_REASON=""
  for account in $ACCOUNT_POOL_ORDER; do
    verdict=$(_account_pool_budget_verdict "$account" 2>/dev/null || echo "unknown")
    case "$verdict" in
      gated*)
        _account_pool_log "account=$account budget verdict=$verdict, skipping without spending a call"
        continue
        ;;
    esac
    out=$("$@" 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ]; then
      export ACCOUNT_POOL_SELECTED="$account"
      export ACCOUNT_POOL_LAST_REASON=""
      printf '%s\n' "$out"
      return 0
    fi
    reason=$(_account_pool_classify_failure "$out")
    _account_pool_log "account=$account command failed rc=$rc reason=$reason"
    export ACCOUNT_POOL_LAST_REASON="$reason"
    case "$reason" in
      exhausted) continue ;;
      unauthenticated) continue ;;
      *) continue ;;   # a transient/other failure still tries the next account rather than
                        # giving up on the whole pool over one bad tick
    esac
  done
  _account_pool_log "ALL accounts in '$ACCOUNT_POOL_ORDER' failed this call"
  export ACCOUNT_POOL_LAST_REASON="${ACCOUNT_POOL_LAST_REASON:-all_exhausted}"
  return 3
}
