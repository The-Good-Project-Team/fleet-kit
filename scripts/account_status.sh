#!/bin/bash
# account_status.sh -- one screen answering "why is the fleet not running?" for every
# account in the pool: credentials present, gated (and until when), and -- with
# --live -- whether it actually authenticates right now.
#
# WHY THIS EXISTS: diagnosing the 2026-09-02 outage meant hand-assembling this view
# from four places (credentials.json mtimes, account-pool-exhausted.state epochs,
# account-pool.log tails, and a manual `claude -p` per account) and getting it wrong
# twice along the way -- first reading a CORRECT weekly-cap gate as stale and
# clearing it, then reading a capped-but-authenticating account as healthy. Both
# mistakes came from the same gap: nothing shows auth state and quota state side by
# side, and they fail in ways that look identical from any single source.
#
# THE DISTINCTION THIS EXISTS TO MAKE:
#   revoked/expired credentials -> `claude -p` fails; fix with set_account_token.sh
#   weekly quota exhausted      -> `claude -p` SUCCEEDS; only real load fails. The
#                                  gate is correct and must NOT be cleared -- it is
#                                  the pool avoiding a call it knows will fail.
# An account can authenticate perfectly and still be unusable. Clearing a quota gate
# to "fix" that just burns a call per tick rediscovering the cap.
#
# Usage:
#   bash account_status.sh           # fast, no API calls: files + gates only
#   bash account_status.sh --live    # also make one real call per account
# Run inside the container, or `podman exec <instance> bash /fleet-kit/scripts/account_status.sh`.
set -uo pipefail

LIVE=0
[ "${1:-}" = "--live" ] && LIVE=1

ACCOUNTS="${FLEET_ACCOUNTS:-primary}"
LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"
POOL_LOG="${ACCOUNT_POOL_LOG_FILE:-$LOG_DIR/account-pool.log}"

# Source the pool for _account_pool_budget_verdict instead of re-deriving gate logic.
# account_readiness.sh's header records what re-deriving costs: it read a different
# default log dir than account_pool.sh and reported a false ready=2 while both
# accounts were really gated. One implementation, one source of truth.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$HERE/account_pool.sh" 2>/dev/null || {
  echo "FAIL: cannot source $HERE/account_pool.sh"; exit 1; }

now=$(date +%s)
fmt_epoch() { date -u -d "@$1" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || date -u -r "$1" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || echo "epoch=$1"; }

echo "accounts: $ACCOUNTS"
echo "state:    $ACCOUNT_POOL_STATE_FILE"
[ "$LIVE" -eq 1 ] && echo "mode:     --live (one real API call per account)" \
                  || echo "mode:     offline (pass --live to actually test auth)"
echo

usable=0
for acct in $ACCOUNTS; do
  dir="$HOME/.claude-$acct"
  creds="$dir/.credentials.json"
  echo "── $acct"

  if [ ! -f "$creds" ]; then
    echo "   credentials: MISSING ($creds)"
    echo "   fix:         bash set_account_token.sh $acct"
    echo
    continue
  fi
  echo "   credentials: present, updated $(stat -c %y "$creds" 2>/dev/null || stat -f %Sm "$creds")"

  # An empty accessToken is the 2026-08-24 failure verify_account_login.sh documents:
  # file present and well-formed, token blank, CLI reports a session expiry instead.
  tok_len=$(python3 -c '
import json, sys
try:
    print(len(json.load(open(sys.argv[1])).get("claudeAiOauth", {}).get("accessToken", "") or ""))
except Exception:
    print(0)
' "$creds" 2>/dev/null)
  if [ "${tok_len:-0}" -lt 20 ]; then
    echo "   token:       EMPTY/SHORT (len=$tok_len) -- setup-token did not save a usable value"
    echo "   fix:         bash set_account_token.sh $acct"
    echo
    continue
  fi
  echo "   token:       present (len=$tok_len)"

  verdict=$(_account_pool_budget_verdict "$acct" 2>/dev/null || echo unknown)
  case "$verdict" in
    gated:exhausted_until_*)
      epoch="${verdict##*_}"
      mins=$(( (epoch - now) / 60 ))
      echo "   quota:       GATED until $(fmt_epoch "$epoch") (${mins}m away)"
      echo "                the pool will skip it without spending a call. This is"
      echo "                normal for a hit weekly cap -- do NOT clear it to 'fix' anything."
      ;;
    *)
      echo "   quota:       no gate -- pool will try this account"
      ;;
  esac

  if [ "$LIVE" -eq 1 ]; then
    # Reproduce account_pool_run's own three-way auth resolution (account_pool.sh
    # ~line 207-218) instead of unconditionally stripping CLAUDE_CODE_OAUTH_TOKEN:
    # an override var takes it, "primary" legitimately inherits the ambient token
    # unchanged, and only every other named account has it cleared. Testing the
    # wrong path here reports a healthy primary/overridden account as FAIL, or
    # (the leak account_pool.sh's own header documents) a dead named account as
    # OK by silently authenticating as whoever the ambient token belongs to.
    #
    # No pipe into `head` here on purpose: that discarded claude's own exit status
    # (PIPESTATUS[0] doesn't survive a `$(...)` command substitution back to the
    # caller), leaving "OK" decided purely by matching a five-phrase regex against
    # text. A hang killed by `timeout` (rc=124), a network blip, or any wording not
    # in the list fell through to "live auth: OK ()" -- the exact false-healthy
    # reading this flag exists to catch. rc is now checked directly; the regex is
    # only used to annotate WHY a failure happened, same division of labor as
    # account_pool.sh's own rc-first + _account_pool_classify_failure-second split.
    var_name="CLAUDE_CODE_OAUTH_TOKEN_$(echo "$acct" | tr '[:lower:]-' '[:upper:]_')"
    token_override="${!var_name:-}"
    if [ -n "$token_override" ]; then
      out=$(CLAUDE_CONFIG_DIR="$dir" CLAUDE_CODE_OAUTH_TOKEN="$token_override" \
              timeout 90 claude -p "say ok" 2>&1)
    elif [ "$acct" = "primary" ]; then
      out=$(CLAUDE_CONFIG_DIR="$dir" timeout 90 claude -p "say ok" 2>&1)
    else
      out=$(CLAUDE_CONFIG_DIR="$dir" env -u CLAUDE_CODE_OAUTH_TOKEN \
              timeout 90 claude -p "say ok" 2>&1)
    fi
    rc=$?
    out_head=$(head -3 <<<"$out")
    if [ "$rc" -ne 0 ]; then
      why=""
      grep -qiE "revoked|unauthorized|401|not logged in|invalid api key|session expired" <<<"$out" \
        && why=" (auth)"
      echo "   live auth:   FAIL (rc=$rc)$why -- $(tr '\n' ' ' <<<"$out_head" | cut -c1-100)"
      echo "   fix:         bash set_account_token.sh $acct"
      echo
      continue
    fi
    echo "   live auth:   OK ($(tr '\n' ' ' <<<"$out_head" | cut -c1-60))"
  fi

  case "$verdict" in gated:*) ;; *) usable=$((usable + 1)) ;; esac
  echo
done

if [ -f "$POOL_LOG" ]; then
  echo "── recent pool decisions"
  tail -5 "$POOL_LOG" | sed 's/^/   /'
  echo
fi

echo "── summary"
if [ "$usable" -gt 0 ]; then
  echo "   $usable of $(wc -w <<<"$ACCOUNTS") account(s) ungated -- fleet can run."
else
  echo "   NO ungated accounts -- every pass will return rc=3 (ALL_ACCOUNTS_EXHAUSTED)."
  echo "   If a gate is a real weekly cap, wait for its reset; do not clear it."
fi
[ "$LIVE" -eq 0 ] && echo "   (auth not actually tested -- rerun with --live to be sure)"
[ "$usable" -gt 0 ]
