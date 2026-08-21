#!/bin/bash
# the-fixer.sh — incident response for red CI/deploy and (optionally) a dark prod.
#
# Provenance: genericized from nonprofit-atlas's scripts/lucky2/firefighter.sh (real incident
# #2323, 2026-08-14: an 8-hour outage where CI and deploy stayed GREEN the entire time — "did
# the build pass" is not "is the site up". The double-probe prod check and fire-once-per-SHA
# dedup below are load-bearing, not decoration; keep both if you touch this file).
#
# WHY DETERMINISTIC-FIRST: one cheap `gh run list` per tick, ZERO LLM spend unless something is
# actually red. On lucky2 (no inbound network path) a tight poll like this ONE the webhook a
# real incident-response system would use elsewhere.
#
# Contract:
#   - Fires ONCE per failing head SHA (state file) -- a new SHA that's also red is a new fire.
#   - PROD_DOWN (if PROD_HEALTH_URL is set) outranks a build-red fire: a dark site costs every
#     visitor in the hour; a red build costs nobody until someone tries to deploy it.
#   - On fire: opens a fresh worktree off origin/main, runs ONE claude -p pass (opus) whose job
#     is fix-or-revert -- PR-backed only, never pushes to main directly.
set -uo pipefail

REPO="${FLEET_REPO:?set FLEET_REPO}"
LOG="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}/the-fixer.log"
STATE="$HOME/.cache/fleet-kit/the-fixer.state"
HB_STAMP="$HOME/.cache/fleet-kit/the-fixer.hb"
MAX_TURNS="${FIXER_MAX_TURNS:-80}"
PASS_TIMEOUT="${FIXER_PASS_TIMEOUT:-1800}"
MODEL="${FIXER_MODEL:-opus}"

mkdir -p "$(dirname "$LOG")" "$(dirname "$STATE")"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

cd "$REPO" 2>/dev/null || { log "FATAL: repo missing at $REPO"; exit 1; }

# --- deterministic red-check: one gh call per workflow, no LLM --------------------------------
read_latest() { # <workflow> [branch] -> "conclusion sha"
  local wf="$1" branch="${2:-}"
  gh run list --workflow "$wf" ${branch:+--branch "$branch"} \
    --limit 5 --json conclusion,headSha \
    -q '[.[] | select(.conclusion != null and .conclusion != "" and .conclusion != "cancelled")][0] | (.conclusion // "none") + " " + (.headSha // "none")' \
    2>>"$LOG" || echo "unreadable none"
}

CI_STATE=$(read_latest "${FIXER_CI_WORKFLOW:-ci.yml}" "${FIXER_DEFAULT_BRANCH:-main}")
DEPLOY_STATE=$(read_latest "${FIXER_DEPLOY_WORKFLOW:-deploy.yml}")
CI_CONC="${CI_STATE%% *}";     CI_SHA="${CI_STATE#* }"
DEP_CONC="${DEPLOY_STATE%% *}"; DEP_SHA="${DEPLOY_STATE#* }"

# --- optional: is the deployed product actually serving? --------------------------------------
# Set FIXER_HEALTH_URL/FIXER_PAGE_URL to enable. Both must fail before this counts as a fire --
# a single timeout is a blip or a deploy mid-flight, not an outage.
PROD_DOWN=""
if [ -n "${FIXER_HEALTH_URL:-}" ] && [ -n "${FIXER_PAGE_URL:-}" ]; then
  probe() {
    local code
    code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0 (fleet-kit the-fixer)' "$1" 2>/dev/null)
    [ -z "$code" ] && code=000
    printf '%s' "$code"
  }
  P_HEALTH=$(probe "$FIXER_HEALTH_URL")
  case "$P_HEALTH" in 2*|3*)
    P_PAGE=$(probe "$FIXER_PAGE_URL")
    case "$P_PAGE" in 2*|3*) ;; *)
      P_PAGE2=$(probe "$FIXER_PAGE_URL")   # confirm: never fire on one sample
      case "$P_PAGE2" in 2*|3*) ;; *) PROD_DOWN="health=$P_HEALTH page=$P_PAGE,$P_PAGE2" ;; esac
    esac ;;
  *) PROD_DOWN="health=$P_HEALTH" ;;
  esac
fi

FIRE_SHA=""
FIRE_WHAT=""
if [ "$DEP_CONC" = "failure" ]; then FIRE_SHA="$DEP_SHA"; FIRE_WHAT="${FIXER_DEPLOY_WORKFLOW:-deploy.yml}"; fi
if [ "$CI_CONC" = "failure" ]; then FIRE_SHA="$CI_SHA"; FIRE_WHAT="${FIRE_WHAT:+$FIRE_WHAT+}${FIXER_CI_WORKFLOW:-ci.yml}(${FIXER_DEFAULT_BRANCH:-main})"; fi
if [ -n "$PROD_DOWN" ]; then
  # No single commit is necessarily guilty (an outage can be a resource threshold crossed, not
  # a bad deploy) -- bucket by time so dedup has something stable while a live outage re-fires.
  FIRE_SHA="prod-$(( $(date +%s) / 1800 ))"
  FIRE_WHAT="PROD DOWN ($PROD_DOWN)"
fi

# 6-hourly liveness so a ghost/deadman audit can tell "quiet" from "dead".
if [ ! -f "$HB_STAMP" ] || [ -n "$(find "$HB_STAMP" -mmin +360 2>/dev/null)" ]; then
  touch "$HB_STAMP"
  log "alive -- last check ci=$CI_CONC deploy=$DEP_CONC (quiet ticks emit nothing else)"
fi

LAST=$(cat "$STATE" 2>/dev/null || echo "")

if [ -z "$FIRE_SHA" ]; then
  [ -n "$LAST" ] && [ "${LAST%% *}" = "red" ] && log "EXTINGUISHED: green again (ci=$CI_CONC deploy=$DEP_CONC)"
  echo "green $CI_SHA" > "$STATE"
  exit 0
fi

if [ "$LAST" = "red $FIRE_SHA" ]; then
  log "still red at $FIRE_SHA -- already fought this head, waiting for the fix PR / a new sha"
  exit 0
fi

log "FIRE: $FIRE_WHAT failure at ${FIRE_SHA:0:12} -- launching fix-or-revert pass"

# shellcheck source=/dev/null
[ -f "$(dirname "${BASH_SOURCE[0]}")/account_pool.sh" ] && . "$(dirname "${BASH_SOURCE[0]}")/account_pool.sh"

WDT="$HOME/.cache/fleet-kit/the-fixer-wt-$(date +%s)"
git fetch -q origin "${FIXER_DEFAULT_BRANCH:-main}" 2>>"$LOG" || true
git worktree add "$WDT" "origin/${FIXER_DEFAULT_BRANCH:-main}" >>"$LOG" 2>&1 || { log "FATAL: worktree failed"; exit 1; }
cleanup() { git -C "$REPO" worktree remove --force "$WDT" >/dev/null 2>&1 || true; git -C "$REPO" worktree prune >/dev/null 2>&1 || true; }
trap cleanup EXIT

if [ -n "$PROD_DOWN" ] && [ -n "${FIXER_PROD_DIAG_DRIVER:-}" ] && [ -x "$FIXER_PROD_DIAG_DRIVER" ]; then
  # Prod-authority path: a pluggable driver script (your own, never shipped by this kit --
  # every real prod-restore mechanism is platform-specific by construction). Contract: read-only
  # diagnosis first, restore-oriented fix second, and it must NEVER be handed destructive DDL or
  # a direct push to the default branch -- the instinct to "do it properly via a PR" is what
  # keeps an outage going; this path exists so speed beats elegance while still being PR-backed.
  PROMPT="PROD IS DOWN RIGHT NOW ($PROD_DOWN). You are the on-call incident responder.
Restore service FIRST; the tidy permanent fix comes after, as a normal PR.
Diagnose with: $FIXER_PROD_DIAG_DRIVER
Never run destructive DDL. Never push directly to ${FIXER_DEFAULT_BRANCH:-main} -- PR only."
else
  PROMPT="$FIRE_WHAT is failing at commit ${FIRE_SHA:0:12} on ${FIXER_DEFAULT_BRANCH:-main}.
Read the failing run's log (gh run view --log-failed). Open a fix PR from this worktree, or --
if the fix is not obvious within your turn budget -- open a REVERT PR of the breaking merge
instead. PR-backed only; never push directly to ${FIXER_DEFAULT_BRANCH:-main}. Arm auto-merge
on whichever PR you open, same as gru does."
fi

cd "$WDT"
claude -p "$PROMPT" --model "$MODEL" --max-turns "$MAX_TURNS" \
  --setting-sources user --dangerously-skip-permissions \
  --allowedTools "Read,Edit,Write,Bash,Grep,Glob" \
  >>"$LOG" 2>&1

echo "red $FIRE_SHA" > "$STATE"
