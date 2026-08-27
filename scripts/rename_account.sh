#!/bin/bash
# rename_account.sh — rename a Claude account in the pool, moving everything keyed on its name.
#
# WHY THIS IS A SCRIPT AND NOT A SED: an account name is load-bearing in four places that must
# change together, and the failure mode when one is missed is SILENT.
#
#   1. $FLEET_CREDS_DIR/.claude-<name>   — the credential dir (account_pool.sh:181)
#   2. FLEET_ACCOUNTS in the instance fleet.env — the pool's try-order (deploy.sh:46)
#   3. the exhaustion state file, keyed by name (account_pool.sh:88,103)
#   4. the host bind mount, recreated per deploy (deploy.sh:94)
#
# The trap: deploy.sh:94 does `mkdir -p "$FLEET_CREDS_DIR/.claude-$acct"` for every name in
# FLEET_ACCOUNTS. Rename the name in fleet.env WITHOUT moving the credential dir and the next
# deploy cheerfully creates an EMPTY dir -- the account comes up logged out, account_pool.sh
# reports `unauthenticated`, and nothing anywhere says "you renamed it and left the creds
# behind". Move the dir FIRST, which is what this script does.
#
# The other trap: the exhaustion state file keys on the OLD name. An account gated until epoch
# N stops matching after a rename, so the pool forgets it is exhausted and burns a real call
# to rediscover it. This script rewrites that key too -- see _migrate_state.
#
# Usage:  bash scripts/rename_account.sh <old> <new> [--apply]
#         Dry-run by default. Nothing is touched without --apply.
set -uo pipefail

OLD="${1:?usage: rename_account.sh <old> <new> [--apply]}"
NEW="${2:?usage: rename_account.sh <old> <new> [--apply]}"
APPLY=0
[ "${3:-}" = "--apply" ] && APPLY=1

CREDS_DIR="${FLEET_CREDS_DIR:-$HOME}"
INSTANCE_DIR="${FLEET_INSTANCE_DIR:?set FLEET_INSTANCE_DIR -- e.g. /home/ubuntu/fleet-kit/instances/nonprofit-atlas}"
ENV_FILE="$INSTANCE_DIR/fleet.env"
STATE_FILE="${ACCOUNT_POOL_STATE_FILE:-$INSTANCE_DIR/logs/account-pool-exhausted.state}"

say() { echo "[rename $( [ "$APPLY" = 1 ] && echo APPLY || echo DRY-RUN )] $*"; }
die() { echo "[rename] FATAL: $*" >&2; exit 1; }

# Name must be safe as both a path suffix and a bare word in a space-separated shell list.
case "$NEW" in
  *[!a-zA-Z0-9_-]*|"") die "new name '$NEW' must be [A-Za-z0-9_-] only -- it becomes a directory suffix AND a word in FLEET_ACCOUNTS" ;;
esac

[ -f "$ENV_FILE" ] || die "no fleet.env at $ENV_FILE (is FLEET_INSTANCE_DIR right? the repo-root fleet.env is NOT the live one)"
grep -qE "^FLEET_ACCOUNTS=" "$ENV_FILE" || die "no FLEET_ACCOUNTS line in $ENV_FILE"

CURRENT=$(grep -E "^FLEET_ACCOUNTS=" "$ENV_FILE" | tail -1 | sed -E 's/^FLEET_ACCOUNTS=//; s/^"//; s/"$//')
say "current FLEET_ACCOUNTS: $CURRENT"

# shellcheck disable=SC2086
set -- $CURRENT
FOUND=0; NEWLIST=()
for a in "$@"; do
  if [ "$a" = "$OLD" ]; then FOUND=1; NEWLIST+=("$NEW"); else NEWLIST+=("$a"); fi
  [ "$a" = "$NEW" ] && die "'$NEW' is already in FLEET_ACCOUNTS -- refusing to create a duplicate"
done
[ "$FOUND" = 1 ] || die "'$OLD' is not in FLEET_ACCOUNTS ($CURRENT)"

say "new FLEET_ACCOUNTS:     ${NEWLIST[*]}"

# --- 1. credential dir -------------------------------------------------------------------
SRC="$CREDS_DIR/.claude-$OLD"
DST="$CREDS_DIR/.claude-$NEW"
if [ -d "$DST" ]; then
  die "$DST already exists -- refusing to overwrite. Inspect and remove it first."
fi
if [ ! -d "$SRC" ]; then
  say "WARNING: no credential dir at $SRC -- account was already logged out; nothing to move"
else
  if [ ! -f "$SRC/.credentials.json" ] && [ ! -f "$SRC/.claude.json" ]; then
    say "WARNING: $SRC has no .credentials.json/.claude.json -- moving it anyway, but this account cannot authenticate until you run: CLAUDE_CONFIG_DIR=$DST claude setup-token"
  fi
  say "move  $SRC -> $DST"
  [ "$APPLY" = 1 ] && { mv "$SRC" "$DST" || die "mv failed"; }
fi

# --- 2. exhaustion state -----------------------------------------------------------------
# Keyed on the account NAME. Left alone, a live gate silently stops applying and the pool
# spends a real call to rediscover an exhaustion it already knew about.
_migrate_state() {
  [ -f "$STATE_FILE" ] || { say "no state file at $STATE_FILE -- nothing to migrate"; return; }
  if ! awk -v a="$OLD" '$1==a{f=1} END{exit !f}' "$STATE_FILE"; then
    say "state file has no entry for '$OLD' -- nothing to migrate"
    return
  fi
  local epoch now
  epoch=$(awk -v a="$OLD" '$1==a{print $2}' "$STATE_FILE" | tail -1)
  now=$(date +%s)
  if [ "$epoch" -gt "$now" ]; then
    say "state: '$OLD' is GATED until $(date -u -d "@$epoch" 2>/dev/null || date -u -r "$epoch" 2>/dev/null) -- carrying that gate over to '$NEW'"
  else
    say "state: '$OLD' entry is stale (expired) -- carrying it over anyway, it is inert"
  fi
  if [ "$APPLY" = 1 ]; then
    local tmp="${STATE_FILE}.tmp"
    awk -v o="$OLD" -v n="$NEW" '$1==o{$1=n} {print}' "$STATE_FILE" > "$tmp" || die "state rewrite failed"
    cat "$tmp" > "$STATE_FILE" && rm -f "$tmp"
  fi
}
_migrate_state

# --- 3. fleet.env ------------------------------------------------------------------------
# TRUNCATE-AND-REWRITE, never `sed -i` -- fleet.env is BIND-MOUNTED into the container and a
# bind mount follows the INODE. `sed -i` writes a new file and renames over it, which detaches
# the container's view: the host shows the new value while the container reads the old one
# forever. Same rule deploy.sh:194 documents.
say "rewrite FLEET_ACCOUNTS in $ENV_FILE"
if [ "$APPLY" = 1 ]; then
  tmp="${ENV_FILE}.rename.tmp"
  awk -v repl="FLEET_ACCOUNTS=\"${NEWLIST[*]}\"" \
    '/^FLEET_ACCOUNTS=/{print repl; next} {print}' "$ENV_FILE" > "$tmp" || die "env rewrite failed"
  cat "$tmp" > "$ENV_FILE" || die "truncate-and-rewrite failed"
  rm -f "$tmp"
fi

echo
if [ "$APPLY" = 1 ]; then
  say "done. NOW REDEPLOY -- the running container still has the old mount:"
  echo "    FLEET_INSTANCE_DIR=$INSTANCE_DIR bash scripts/deploy.sh"
  echo "  then verify BOTH, from the container, not the host checkout:"
  echo "    podman exec <container> grep '^FLEET_ACCOUNTS=' /fleet-kit/fleet.env"
  echo "    podman exec <container> ls -d /root/.claude-$NEW"
  echo "    bash scripts/verify_account_login.sh $NEW"
else
  say "dry run only -- nothing changed. Re-run with --apply to execute."
fi
