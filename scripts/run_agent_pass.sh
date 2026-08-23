#!/bin/bash
# run_agent_pass.sh — run one charter-driven claude -p pass (for ceo.md / architect.md /
# any other charter you write). Sources the charter (frontmatter stripped) as the prompt,
# runs it through the account pool, logs the outcome. Generic across every "deep pass"
# agent in this kit — the difference between a CEO pass and an architect pass is entirely
# in the charter file, not in how it's invoked.
#
# Provenance: distilled from nonprofit-atlas's `scripts/lucky2/dumbledore.sh` /
# `scripts/lucky2/architect.sh` pattern (charter sourced fresh every pass, never an inline
# prompt copy that can drift from the file a human actually edits).
#
# Usage: run_agent_pass.sh <charter-name-without-.md> [--model MODEL] [--max-turns N]
#   e.g.  run_agent_pass.sh ceo
#         run_agent_pass.sh architect --model opus --max-turns 200
set -uo pipefail

# set -a/+a: a plain . only sets local shell vars, invisible to claude -p (a separate
# exec) -- see run_member.sh for the full incident writeup (2026-08-22 fleet-wide auth outage).
[ -f "${FLEET_ENV_FILE:-./fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-./fleet.env}"; set +a; }

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${FLEET_REPO:?set FLEET_REPO in fleet.env}"
LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"

CHARTER_NAME="${1:?usage: run_agent_pass.sh <charter-name> [--model M] [--max-turns N]}"
shift
MODEL="sonnet"
MAX_TURNS=120
while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --max-turns) MAX_TURNS="$2"; shift 2 ;;
    *) shift ;;
  esac
done

LOG="$LOG_DIR/${CHARTER_NAME}.log"
mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

CHARTER="$KIT_DIR/agents/${CHARTER_NAME}.md"
if [ ! -f "$CHARTER" ]; then
  log "FATAL: charter not found at $CHARTER"
  exit 2
fi
PROMPT=$(awk 'BEGIN{d=0} /^---$/{d++; next} d>=2{print}' "$CHARTER")
if [ -z "$PROMPT" ]; then
  log "FATAL: charter empty after frontmatter strip"
  exit 2
fi

cd "$REPO" 2>/dev/null || { log "FATAL: repo missing at $REPO"; exit 1; }
[ -f "$KIT_DIR/scripts/account_pool.sh" ] && . "$KIT_DIR/scripts/account_pool.sh"
command -v account_pool_run >/dev/null 2>&1 || account_pool_run() { "$@"; }

log "pass start (charter=$CHARTER_NAME model=$MODEL max_turns=$MAX_TURNS)"
# --dangerously-skip-permissions: see worktree_builder.sh's comment on the same flag --
# acceptEdits alone still gates Bash execution behind an interactive approval prompt that
# nothing here can answer. A CEO/architect pass runs unattended and needs to run real
# commands (tests, self-heal, investigation), not just edit files.
# --setting-sources user: see worktree_builder.sh's comment -- a target repo that runs its
# own persona/orchestrator convention via CLAUDE.md/hooks will hijack this pass's identity
# and ignore $CHARTER_NAME's instructions entirely, with no error. `user` scope drops
# project-level CLAUDE.md/hooks so this kit's own charter actually governs the session.
OUT=$(account_pool_run timeout "$((MAX_TURNS * 60))" claude -p "$PROMPT" \
  --model "$MODEL" --dangerously-skip-permissions --setting-sources user \
  --max-turns "$MAX_TURNS" 2>>"$LOG")
RC=$?
SUMMARY=$(tail -c 400 <<<"$OUT" | tr '\n' ' ' | tail -c 300)
if [ "$RC" -eq 0 ]; then
  log "pass end rc=0 (account=${ACCOUNT_POOL_SELECTED:-unknown}) :: $SUMMARY"
else
  log "pass end rc=$RC (account=${ACCOUNT_POOL_SELECTED:-none} reason=${ACCOUNT_POOL_LAST_REASON:-}) :: $SUMMARY"
fi
exit "$RC"
