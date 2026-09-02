#!/bin/bash
# set_account_token.sh -- paste a `claude setup-token` value into an account's
# credentials.json, then prove it authenticates. The WRITE half that
# verify_account_login.sh (the read half) has always assumed someone else did.
#
# WHY THIS EXISTS: `claude setup-token` prints a token and, often, does not persist
# it into $CLAUDE_CONFIG_DIR at all. verify_account_login.sh already documents the
# adjacent failure (2026-08-24: file written, accessToken empty). Confirmed again
# live on dino 2026-09-02, a different shape: setup-token reported "token created
# successfully", printed the value, and .credentials.json kept its 5-day-old mtime
# -- nothing was written, and the account went on 401ing with "OAuth access token
# has been revoked". Recovery both times was a hand-built credentials.json, from
# memory, under pressure. This script is that recovery, written down.
#
# WHAT IT DOES: reads the token from a hidden prompt (never an argv, never shell
# history), writes ONLY the accessToken field into the account's existing
# credentials.json -- preserving scopes/subscriptionType/expiry that the CLI
# needs -- then hands off to verify_account_login.sh for the live check.
#
# Usage:  bash set_account_token.sh <account-name>
#   e.g.  bash set_account_token.sh gmail
# Run INSIDE the container (or `podman exec -it <instance> bash ...`) -- it needs a
# TTY for the hidden prompt, so `podman exec` WITHOUT -it will fail at the read.
set -uo pipefail

ACCOUNT="${1:?usage: set_account_token.sh <account-name>}"
CONFIG_DIR="$HOME/.claude-$ACCOUNT"
CREDS="$CONFIG_DIR/.credentials.json"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== setting token for account '$ACCOUNT' at $CONFIG_DIR =="

if [ ! -d "$CONFIG_DIR" ]; then
  echo "FAIL: no config dir $CONFIG_DIR."
  echo "      Account names come from FLEET_ACCOUNTS and map to \$HOME/.claude-<name>."
  echo "      Create it first (mkdir -p $CONFIG_DIR) if this is a genuinely new account."
  exit 1
fi

if [ -f "$CREDS" ]; then
  echo "credentials now: $(stat -c %y "$CREDS" 2>/dev/null || stat -f %Sm "$CREDS")"
else
  echo "credentials now: (absent -- will be created)"
fi

# -s: the token never echoes to the terminal and never enters shell history. Taking
# it on argv instead would leak it into `ps`, the shell's history file, and -- when
# run through podman/ssh -- the calling host's history too.
if [ ! -t 0 ]; then
  echo "FAIL: stdin is not a TTY -- the token prompt needs one."
  echo "      Use 'podman exec -it <instance> bash $0 $ACCOUNT' (note -it)."
  exit 1
fi
read -rsp "paste token (input hidden), then Enter: " TOKEN
echo
[ -n "$TOKEN" ] || { echo "FAIL: empty input, nothing written."; exit 1; }

BEFORE=""
[ -f "$CREDS" ] && BEFORE=$(stat -c %Y "$CREDS" 2>/dev/null || stat -f %m "$CREDS")

TOKEN="$TOKEN" CREDS="$CREDS" python3 <<'PY'
import json, os, sys, time

tok = os.environ["TOKEN"].strip()
creds = os.environ["CREDS"]

# Fail on a mispaste rather than writing garbage that 401s an hour later, when the
# connection to "I pasted the wrong thing" is long gone.
if not tok.startswith("sk-ant-oat01-"):
    sys.exit("FAIL: not a setup-token value (expected an sk-ant-oat01- prefix). Nothing written.")

# Preserve the existing document. scopes/subscriptionType/expiresAt all matter to the
# CLI, and a from-scratch file that omits them authenticates inconsistently -- swap
# ONLY accessToken. The defaults below are the shape of a known-good account on dino,
# used solely when no file exists yet.
if os.path.exists(creds):
    with open(creds) as fh:
        doc = json.load(fh)
else:
    doc = {"claudeAiOauth": {}}

oauth = doc.setdefault("claudeAiOauth", {})
oauth["accessToken"] = tok
oauth.setdefault("refreshToken", "")
oauth.setdefault("scopes", [
    "user:file_upload", "user:inference", "user:mcp_servers",
    "user:profile", "user:sessions:claude_code",
])
oauth.setdefault("subscriptionType", "max")
# Always overwrite, never setdefault: a freshly pasted token invalidates whatever
# expiresAt (0, or a stale multi-day-old value) was already on file, and
# verify_account_login.sh gates on this field *before* the live call -- a stale
# value here makes verification falsely report the token as already-expired right
# after a successful write. We don't know the token's real expiry (setup-token
# only prints the value, not its lifetime), so use a long-lived placeholder; the
# live call that follows is the actual ground truth, not this timestamp.
oauth["expiresAt"] = int((time.time() + 365 * 24 * 3600) * 1000)

# Atomic replace: a crash mid-write must not leave a truncated credentials file that
# authenticates as nobody and takes the whole pool down.
os.umask(0o077)
tmp = creds + ".tmp"
with open(tmp, "w") as fh:
    json.dump(doc, fh)
os.replace(tmp, creds)
print("wrote %d chars of accessToken" % len(tok))
PY
[ $? -eq 0 ] || exit 1

chmod 600 "$CREDS"

# The check that would have caught today's outage at the moment it happened rather
# than at the next gru tick: setup-token can claim success and write NOTHING.
AFTER=$(stat -c %Y "$CREDS" 2>/dev/null || stat -f %m "$CREDS")
if [ -n "$BEFORE" ] && [ "$BEFORE" = "$AFTER" ]; then
  echo "FAIL: mtime did not change -- the write did not land. Check permissions on $CREDS."
  exit 1
fi
echo "credentials after: $(stat -c %y "$CREDS" 2>/dev/null || stat -f %Sm "$CREDS")"
echo

# Ground truth is a live call, not a well-formed file. Delegate to the existing
# verifier rather than growing a second, subtly-different copy of that logic.
if [ -x "$HERE/verify_account_login.sh" ] || [ -f "$HERE/verify_account_login.sh" ]; then
  exec bash "$HERE/verify_account_login.sh" "$ACCOUNT"
fi
echo "WARN: verify_account_login.sh not found next to this script -- token written but NOT verified."
echo "      Verify by hand: CLAUDE_CONFIG_DIR=$CONFIG_DIR claude -p 'say ok'"
