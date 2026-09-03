#!/bin/bash
# resolve_maxx_handle.sh -- print the maxx handle (and its key) for the account this
# pass will actually spend from, so the budget meter tracks the account doing the
# spending.
#
# WHY THIS EXISTS: FLEET_ACCOUNTS is a pool of N; FLEET_MAXX_HANDLE is a single
# value. Whichever account is not named by that one handle spends unmetered.
# Confirmed live on dino 2026-09-02: philanthropy ran FLEET_ACCOUNTS="gmail tgp"
# while FLEET_MAXX_HANDLE=reif_tgp, so gru paced against tgp -- an account gated
# until Sep 4 whose anchor had been frozen since Aug 30 -- while gmail did all the
# real spending, untracked. The meter read verdict=stale off that dead anchor and
# gru fell back to a hardcoded Aug 30 constant (per_diem_hourly_pct=0.04) instead
# of the live 0.441 its own account was reporting.
#
# WHY NOT ACCOUNT_POOL_SELECTED: that is set AFTER a call succeeds, but the budget
# read happens in gru's step 1, BEFORE any pool call -- there is nothing to read
# yet. The handle therefore resolves from which account the pool WOULD pick: the
# first in FLEET_ACCOUNTS order that is not currently gated, which is exactly
# account_pool_run's own selection rule (see its budget-verdict skip loop).
#
# HANDLE AND KEY MOVE TOGETHER. maxx resolves a per-handle secret and falls back to
# none, so a handle queried with another handle's key is rejected -- confirmed live
# 2026-09-02: handle=reif with tgp's key returned {"isError":true,"text":"unauthorized"}
# over /mcp, which maxx_reader.py reports as maxx_unexpected_shape. That is an
# UNREADABLE meter, and an unreadable meter fails open to a stale constant -- the
# exact outage this script exists to end. Emitting a handle without its key would
# therefore make things worse, not better.
#
# MAPPING: FLEET_MAXX_HANDLE_<ACCOUNT> and FLEET_MAXX_KEY_<ACCOUNT> (account
# upper-cased, non-alnum -> "_"), the same convention CLAUDE_CODE_OAUTH_TOKEN_<ACCOUNT>
# already uses in account_pool.sh. An account with no mapping resolves nothing, so an
# instance that never sets one keeps exactly its current behaviour.
#
# Usage:
#   read -r handle key < <(bash resolve_maxx_handle.sh)
# Prints "<handle> <key>" (key possibly empty) on one line, or nothing at all when
# no mapping applies -- in which case the caller keeps whatever FLEET_MAXX_HANDLE
# and FLEET_MAXX_KEY it already had (fail open, same law as maxx_reader.py: a bad
# reading may only ever be usable to CONSERVE, never to invent headroom).
set -uo pipefail

# Strip surrounding quotes. When fleet.env is SOURCED by a shell, `FLEET_ACCOUNTS="tgp gmail"`
# arrives unquoted -- but a caller that parses the file itself (fleet_view_server's
# subprocess_env, which reads KEY=value as text so a key added after startup is still visible)
# passes the literal `"tgp gmail"`, quotes included. The first account then became `"tgp`, this
# script built `FLEET_MAXX_HANDLE_"TGP`, bash rejected it as an invalid variable name, and the
# loop fell through to its no-mapping path -- exiting 0 with EMPTY output. Every such caller
# silently got no resolution and kept metering whatever static handle it already had, which is
# precisely the wrong-account bug this script exists to prevent. Found 2026-09-03 wiring the
# resolved account into the Settings page, which could not name the account for this reason.
ACCOUNTS="${FLEET_ACCOUNTS:-primary}"
ACCOUNTS="${ACCOUNTS%\"}"; ACCOUNTS="${ACCOUNTS#\"}"
ACCOUNTS="${ACCOUNTS%\'}"; ACCOUNTS="${ACCOUNTS#\'}"
STATE_FILE="${ACCOUNT_POOL_STATE_FILE:-${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}/account-pool-exhausted.state}"

_suffix_for() {
  # account "claude-reif" -> CLAUDE_REIF
  echo "$1" | tr '[:lower:]-' '[:upper:]_'
}

now=$(date +%s)
for acct in $ACCOUNTS; do
  # Skip accounts the pool itself would skip. Reading the same state file
  # account_pool.sh writes keeps one source of truth -- account_readiness.sh's
  # header records what re-deriving this costs (it read a different default log
  # dir and reported a false ready=2 while both accounts were really gated).
  if [ -f "$STATE_FILE" ]; then
    epoch=$(awk -v a="$acct" '$1==a{print $2}' "$STATE_FILE" | tail -1)
    if [ -n "$epoch" ] && [ "$epoch" -gt "$now" ] 2>/dev/null; then
      continue
    fi
  fi

  sfx="$(_suffix_for "$acct")"
  hvar="FLEET_MAXX_HANDLE_$sfx"
  kvar="FLEET_MAXX_KEY_$sfx"
  mapped="${!hvar:-}"
  if [ -n "$mapped" ]; then
    echo "$mapped ${!kvar:-}"
    exit 0
  fi

  # No per-account mapping for the account that will actually run. Falling back to
  # the instance-wide handle is correct ONLY when it really is this account's
  # handle -- which is unknowable here -- so say nothing and let the caller keep
  # its existing value rather than silently meter the wrong account.
  break
done

# Nothing resolved: every account gated, or the winner has no mapping. Print
# nothing; the caller keeps its existing handle/key.
exit 0
