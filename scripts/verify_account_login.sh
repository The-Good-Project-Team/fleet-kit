#!/bin/bash
# verify_account_login.sh -- after `claude setup-token`, confirm it actually saved AND that
# the saved credential authenticates, instead of trusting the setup-token exit code.
#
# WHY: setup-token can exit 0 while writing nothing useful (confirmed live 2026-08-24: primary
# ran through a token/env-var path that "succeeded" upstream but left .credentials.json's
# accessToken empty, expiresAt=0 -- CLI still reported "OAuth session expired" on next use).
# The only trustworthy check is: does the file have a real, non-empty accessToken with a
# future expiresAt, AND does a real `claude -p` call against it actually return output.
#
# Usage: verify_account_login.sh <account-name>
#   e.g. verify_account_login.sh primary
#        verify_account_login.sh claude-reif
# Run this INSIDE the philanthropy container (or via `podman exec philanthropy ...`).
set -uo pipefail

ACCOUNT="${1:?usage: verify_account_login.sh <account-name>}"
CONFIG_DIR="$HOME/.claude-$ACCOUNT"
CREDS="$CONFIG_DIR/.credentials.json"

echo "== verifying account '$ACCOUNT' at $CONFIG_DIR =="

if [ ! -f "$CREDS" ]; then
  echo "FAIL: no credentials file at $CREDS -- setup-token did not run against this CLAUDE_CONFIG_DIR"
  exit 1
fi

# Pull accessToken/expiresAt out with python3 (already a dependency elsewhere in this kit) --
# more robust than grep against JSON that may reformat.
read -r has_token expires_at <<<"$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    o = d.get("claudeAiOauth", {})
    tok = o.get("accessToken", "")
    print("yes" if tok else "no", o.get("expiresAt", 0))
except Exception as e:
    print("no", 0)
' "$CREDS")"

if [ "$has_token" != "yes" ]; then
  echo "FAIL: $CREDS exists but accessToken is empty -- setup-token did NOT actually save a usable credential."
  echo "      (This is the exact failure mode that caused the 2026-08-24/25 outage: file present, token blank.)"
  exit 1
fi

now_ms=$(( $(date +%s) * 1000 ))
if [ "${expires_at:-0}" -le "$now_ms" ]; then
  echo "FAIL: accessToken present but already expired (expiresAt=$expires_at, now=$now_ms)."
  exit 1
fi

echo "OK: credentials file has a non-empty token, expires in the future ($(( (expires_at - now_ms) / 1000 / 60 )) min from now)."

# Ground truth: don't just trust the file -- prove it authenticates with a real, cheap call.
echo "-- making a live test call to confirm it actually authenticates --"
OUT=$(CLAUDE_CONFIG_DIR="$CONFIG_DIR" timeout 30 claude -p "say ok" --model haiku 2>&1)
RC=$?
if [ "$RC" -eq 0 ] && ! grep -qiE "not logged in|session expired|please.*log.*in|401|unauthorized" <<<"$OUT"; then
  echo "PASS: account '$ACCOUNT' is live-authenticated. Response: $(tr '\n' ' ' <<<"$OUT" | cut -c1-120)"
  exit 0
else
  echo "FAIL: live call did not authenticate (rc=$RC). Output: $OUT"
  exit 1
fi
