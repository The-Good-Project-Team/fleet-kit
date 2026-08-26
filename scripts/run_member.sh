#!/bin/bash
# run_member.sh — the ONE runner for every fleet member. Reads members/<name>/<name>.fleet.json,
# applies live overrides, sources that member's charter, and runs it through `claude -p` with
# its own model/turns/tools/budget.
#
# WHY THIS DIDN'T EXIST UNTIL NOW: member_spec.py collapsed the fleet onto one definition store
# (schedule/model/turns/tools/prompt, all in one reviewed file -- see its own header for the
# incident that forced that). But nothing actually executed a member FROM that file: schedulers/
# and run_agent_pass.sh predate member_spec.py and point at a separate legacy `agents/*.md`
# directory (ceo/architect/builder/reviewer, not any of the 7 real members). A `.fleet.json`
# with a `tools.allow/deny` list that no script ever reads is authority written down and
# ignored -- the exact "instruction nobody obeys" failure class the whole kit exists to end.
# This script is what makes prompt_file/model/max_turns/tools the thing that actually runs,
# not just the thing a dashboard displays.
#
# EVERY MEMBER IS AN LLM (Reif, 2026-08-21) -- see member_spec.py's header. There is no
# "mechanical, just exec a script" branch here on purpose: a member with a goal reaches for its
# own helper script (roomba.py, the-fixer.sh) as one Bash-reachable tool among its allowlist,
# same as any other tool, rather than the script BEING the member's whole behavior.
#
# Usage: run_member.sh <member-name> [--dry-run] [--item <issue-number>] [--task "<instruction>"]
#   e.g.  run_member.sh dumbledore
#         run_member.sh roomba --dry-run     # print the resolved command, run nothing
#         run_member.sh minion --item 3072   # gru spawns minion this way -- see gru.md
#         run_member.sh marie --task "rescore complexity on everything opened today"
#                                            # ad-hoc: the member's full charter PLUS one instruction
set -uo pipefail

# set -a/+a around the source: a plain `.` only sets these as local shell variables, which
# `claude -p` (a separate exec, not this shell) never sees -- fleet.env's own vars were silently
# invisible to every `claude` invocation this whole time, masked because GH_TOKEN happens to
# ALSO be exported per-line in the crontab itself (entrypoint.sh) and every account
# credential lives in its own CLAUDE_CONFIG_DIR, not an env var, so nothing needed this path
# until CLAUDE_CODE_OAUTH_TOKEN did (real incident 2026-08-22: fleet-wide outage, every member
# failing "OAuth session expired" for 2+ hours -- the token was correctly in fleet.env the
# whole time, just never reached the process that needed it).
[ -f "${FLEET_ENV_FILE:-./fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-./fleet.env}"; set +a; }

# FLEET_API_KEY is the fleet-view write key -- it authorizes POST /api/run_now, which spawns
# `claude -p --dangerously-skip-permissions` on this box. Sourcing fleet.env above exports it,
# which would hand every member the ability to spawn unlimited runs (its own included) and put
# the key inside nine agents' contexts, where a single prompt-injected page or issue body could
# print it into a PR comment. No member needs it: nothing in members/ calls that endpoint, and
# a member that wants work done files an issue or reports it, by design.
#
# So: drop it before `claude -p` inherits this environment. Least privilege, and it costs
# nothing today. The key stays in fleet.env for the humans and out-of-band callers that
# actually trigger runs. If a future member legitimately needs to wake another member, grant
# it deliberately here by name -- do not re-export it for everyone.
unset FLEET_API_KEY

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${FLEET_REPO:?set FLEET_REPO in fleet.env}"
LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"
mkdir -p "$LOG_DIR"

MEMBER="${1:?usage: run_member.sh <member-name> [--dry-run] [--item <issue-number>] [--task \"<instruction>\"]}"
shift || true
DRY_RUN=0
ITEM=""
TASK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --item) ITEM="${2:?--item needs an issue number}"; shift 2 ;;
    --task) TASK="${2:?--task needs an instruction}"; shift 2 ;;
    *) shift ;;
  esac
done

LOG="$LOG_DIR/${MEMBER}.log"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

# --- master kill switch, before anything with a cost --------------------------------------
. "$KIT_DIR/scripts/fleet_enabled.sh"
fleet_enabled_or_exit "$MEMBER"

# --- resolve spec: git baseline + live overrides on top ------------------------------------
SPEC_JSON=$(python3 - "$MEMBER" "$KIT_DIR" <<'PYEOF'
import json, sys
sys.path.insert(0, sys.argv[2] + "/scripts")
import member_spec, overrides
try:
    spec = member_spec.by_name(sys.argv[1])
except member_spec.SpecError as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(1)
eff, applied = overrides.apply(spec)
print(json.dumps({"spec": eff, "applied": applied}))
PYEOF
)
RC=$?
if [ "$RC" -ne 0 ]; then
  log "FATAL: $(echo "$SPEC_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error","spec load failed"))' 2>/dev/null || echo "spec load failed")"
  exit 2
fi

SPEC=$(echo "$SPEC_JSON" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["spec"]))')
APPLIED_COUNT=$(echo "$SPEC_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["applied"]))')
[ "$APPLIED_COUNT" -gt 0 ] && log "$APPLIED_COUNT live override(s) applied on top of the git spec"

jget() { echo "$SPEC" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)"; }

ENABLED=$(jget "['enabled']")
TIMEOUT_S=$(jget "['mandate']['limits'].get('timeout_s', $(jget "['timeout_s']"))")
VISION=$(jget "['report']['vision_link']")
VISION_FLAG=""; [ "$VISION" = "required" ] && VISION_FLAG="--vision-required"
# persona_law.md #6 (worktree isolation) is LAW for any unit of work that CHANGES repo files
# when a shared box runs several concurrent members -- opt out only for a member documented
# as read-only-by-design (jefe.fleet.json sets llm.worktree=false; see jefe.md's own "shared
# checkout, not a fresh worktree -- on purpose" bounds section). Default true for everyone else,
# since the generic claude -p path below has no other isolation and gru already spawns N
# concurrent minions into this exact script.
WORKTREE_ENABLED=$(jget "['llm'].get('worktree', True)")

if [ "$ENABLED" != "True" ] && [ "${FLEET_RUN_NOW:-0}" != "1" ]; then
  log "$MEMBER: enabled=false in spec -- exiting without doing anything"
  exit 0
fi

# `-adhoc` in the run_id marks an operator-directed pass. It matters for CALIBRATION: gru
# derives what a normal pass costs from real run records, and a one-off "go rescore everything"
# is not a normal pass -- averaging it in would skew every future estimate. Filter these out
# when calibrating (`run_id NOT LIKE '%-adhoc-%'`).
RUN_ID="${MEMBER}${ITEM:+-item$ITEM}${TASK:+-adhoc}-$$-$(date +%s)"

# A member MAY declare its own runner (e.g. judge-judy's judge-judy.sh, which reviews a
# diff as untrusted TEXT with zero tools -- a shape the generic claude -p path below can't
# express safely). Default: none, every other member runs through the generic path.
CUSTOM_RUNNER=$(jget "['llm'].get('runner', '')")
if [ -n "$CUSTOM_RUNNER" ]; then
  RUNNER_PATH="$KIT_DIR/$CUSTOM_RUNNER"
  if [ ! -x "$RUNNER_PATH" ]; then
    log "FATAL: llm.runner=$CUSTOM_RUNNER not found or not executable at $RUNNER_PATH"
    exit 2
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] $RUNNER_PATH (custom runner, timeout ${TIMEOUT_S}s)"
    exit 0
  fi
  log "pass start (custom runner=$CUSTOM_RUNNER)"
  "$RUNNER_PATH"
  RC=$?
  log "pass end rc=$RC (custom runner)"
  exit "$RC"
fi

# $SPEC is piped via stdin, never string-interpolated into a python literal -- a member's
# mandate/checklist text is free-form English and WILL contain apostrophes (found live on
# dino, 2026-08-21: marie's charter has several -- "repo's own", "pass's vision" -- each one
# terminated the python triple-quote early when $SPEC was substituted inline, corrupting the
# JSON and silently emptying BEHAVIOR downstream. gru's charter happened to have zero
# apostrophes in its mandate text, so this bug shipped invisible until a second member with
# ordinary prose hit it.
BEHAVIOR=$(echo "$SPEC" | python3 -c "
import sys, json
sys.path.insert(0, '$KIT_DIR/scripts')
import member_spec
spec = json.load(sys.stdin)
print(member_spec.behavior_path(spec))
")

cd "$REPO" 2>/dev/null || { log "FATAL: repo missing at $REPO"; exit 1; }
[ -f "$KIT_DIR/scripts/account_pool.sh" ] && . "$KIT_DIR/scripts/account_pool.sh"
command -v account_pool_run >/dev/null 2>&1 || account_pool_run() { "$@"; }

# --- isolate this pass in its own worktree (#3092) -------------------------------------------
# Every prior run of this script just `cd`ed into the ONE shared $REPO checkout with no
# isolation at all -- fine for a single member ticking alone, a live race the moment gru
# backgrounds N concurrent `run_member.sh minion` processes (each fetching/merging/committing/
# pushing against the same HEAD, index, and working tree). worktree_builder.sh already proved
# the fix (fresh `git worktree add` off origin/<default>, same mkdir-lock pattern reused
# verbatim below) -- this just gives the generic member path the isolation persona_law.md #6
# already calls LAW and minion's own charter already claims it gets.
WT_PATH=""
if [ "$WORKTREE_ENABLED" = "True" ] && [ "$DRY_RUN" -ne 1 ]; then
  DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
  DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
  WT_PATH="${TMPDIR:-/tmp}/fleet-run-${MEMBER}${ITEM:+-item$ITEM}-$$"
  WT_BRANCH="member/${MEMBER}${ITEM:+-item$ITEM}-$$-$(date +%s)"
  LOCK="${TMPDIR:-/tmp}/fleet-kit-worktree-add.lock"

  create_run_worktree() {
    local attempt rc=1 waited held
    for attempt in 1 2 3; do
      waited=0; held=0
      while [ "$waited" -lt 120 ]; do
        if mkdir "$LOCK" 2>/dev/null; then held=1; break; fi
        # Steal a lock older than 5 min: a sibling killed mid-add would otherwise wedge every
        # later attempt (this script's or worktree_builder.sh's -- same lock, same hazard).
        if [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin +5 2>/dev/null)" ]; then
          rmdir "$LOCK" 2>/dev/null || true; continue
        fi
        sleep 2; waited=$((waited + 2))
      done
      if [ "$held" -ne 1 ]; then
        log "create_run_worktree: could not acquire lock within 120s (attempt $attempt)"
        sleep $((attempt * 3)); continue
      fi
      git -C "$REPO" fetch origin "$DEFAULT_BRANCH" >/dev/null 2>&1
      git -C "$REPO" worktree prune >/dev/null 2>&1
      if git -C "$REPO" rev-parse --verify --quiet "refs/heads/$WT_BRANCH" >/dev/null 2>&1; then
        git -C "$REPO" branch -D "$WT_BRANCH" >/dev/null 2>&1
      fi
      git -C "$REPO" worktree add "$WT_PATH" -b "$WT_BRANCH" "origin/$DEFAULT_BRANCH"
      rc=$?
      rmdir "$LOCK" 2>/dev/null || true   # safe: reached only when held=1
      [ "$rc" -eq 0 ] && return 0
      log "create_run_worktree: attempt $attempt failed (rc=$rc), retrying"
      sleep $((attempt * 3))
    done
    return "$rc"
  }

  if ! create_run_worktree; then
    log "FATAL: could not create isolated worktree for $MEMBER at $WT_PATH"
    exit 1
  fi
  cleanup_run_worktree() {
    git -C "$REPO" worktree remove --force "$WT_PATH" >/dev/null 2>&1 || true
    git -C "$REPO" worktree prune >/dev/null 2>&1 || true
  }
  trap cleanup_run_worktree EXIT
  cd "$WT_PATH" || { log "FATAL: worktree created but cd failed: $WT_PATH"; exit 1; }
  log "$MEMBER: isolated in worktree $WT_PATH (branch $WT_BRANCH)"
fi

# Source the charter (frontmatter stripped), build --allowedTools/--disallowedTools from the
# spec's own tools.allow/deny, run through the account pool, book real usage.
MODEL=$(jget "['llm']['model']")
# BOTH CAPS ARE OPTIONAL, and an absent one means UNCAPPED -- not a default.
#
# They used to default to 60 turns / $5, which made a cap the silent norm for every member
# whose spec simply didn't mention one. Measured on 142 real minion runs: 43 hit the 60-turn
# wall while only 8 came near $5 -- and every `stop_reason: tool_use` row sat at ~61 turns,
# i.e. the CLI cutting the pass mid-tool-call with budget still to spare. A truncated pass
# still spends everything it spent up to the cut and lands `reported_nothing`, so the cap was
# converting completed-but-expensive work into paid-for nothing.
#
# The real control is SELECTION, not truncation: gru sizes each hour's work to what maxx says
# the hour can afford (per_diem_hourly_pct, already buffered) and picks accordingly. A member
# that needs 90 turns to finish an item marie scored complexity-9 should take them.
#
# Reif, 2026-08-26: "max turns - remove it ... dumbledore should be watching every x hours,
# and it should prune prompt, etc. before it prunes turns." A turn cap MASKS a rambling
# charter instead of fixing it; dumbledore's rot hunt owns that tuning.
MAX_TURNS=$(jget "['llm'].get('max_turns') or ''")
MAX_BUDGET=$(jget "['mandate']['limits'].get('max_budget_usd') or ''")

PROMPT=$(awk 'BEGIN{d=0} /^---$/{d++; next} d>=2{print}' "$BEHAVIOR")
if [ -z "$PROMPT" ]; then
  log "FATAL: charter empty after frontmatter strip ($BEHAVIOR)"
  exit 2
fi

# --item is how gru hands a minion its pre-claimed issue number -- prepended as the very
# first thing the minion reads, before its own charter, so "which item" is never ambiguous
# even though every concurrently-spawned minion runs the exact same charter file.
if [ -n "$ITEM" ]; then
  PROMPT="Your assigned issue number for this run is #$ITEM. Do not work any other issue.

$PROMPT"
fi

# --task is the same mechanism, generalised: run any member ad-hoc with one extra instruction
# on top of its normal charter. `run_member.sh marie --task "rescore everything under 3 days
# old"` gives you marie, with all of marie's judgment and constraints, pointed at one thing.
#
# It ADDS to the charter, never replaces it -- a member's mandate, checklist and escalation
# rules still apply, so an ad-hoc run cannot be used to talk a member out of its own bounds.
# Placed after --item so a run can carry both, and BEFORE the charter for the same reason
# --item is: the first thing read frames everything after it.
if [ -n "$TASK" ]; then
  PROMPT="THIS RUN HAS AN ADDITIONAL INSTRUCTION FROM THE OPERATOR:

$TASK

Do this IN ADDITION TO your charter below, which still governs -- its mandate, checklist,
limits and escalation rules all still apply and this instruction never overrides them. If the
instruction conflicts with your charter, say so plainly in your report and follow the charter.

$PROMPT"
fi

# The report-contract literal-line block -- APPENDED to every prompt, not left as a one-line
# "see persona_law.md §10b" pointer at the bottom of each charter (that WAS the fix in PR #26,
# and it didn't work: confirmed live 2026-08-23, the-fixer and dont-shoot-the-messenger both
# ran real turns/real cost and still landed `reported_nothing` -- their actual final text was
# plain prose like "Checked, all green. No action taken.", never the literal `Outcome:`/
# `Evidence:` lines run_report.py's classify() regex-matches. A charter TELLING a model to go
# read a shared file for the exact format it must close with, after the model has already
# formed its final answer, is not the same as the model actually reading it under a tight turn
# budget -- it never did. Appending the literal block as the LAST thing in the prompt (the
# thing most present when the model composes its final message) is the fix that can't be
# skipped by not going and reading something else.
#
# Extracted from agents/persona_law.md §10b at RUN TIME (not copy-pasted here) so there is
# exactly one source of truth for the exact wording -- editing that doc's §10b changes what
# every member is told, with no second copy to fall out of sync.
REPORT_CONTRACT=$(awk '/^## 10b\./{f=1} f{print} /^## 11\./{exit}' "$KIT_DIR/agents/persona_law.md" | sed '$d')
if [ -n "$REPORT_CONTRACT" ]; then
  PROMPT="$PROMPT

---

$REPORT_CONTRACT"
fi

# Tool flags are the member's AUTHORITY (overrides.py refuses to tune these live, on purpose)
# -- built fresh from the git-reviewed spec every run, never from a cached or hand-edited list.
ALLOWED=$(jget "['llm']['tools'].get('allow', [])" | python3 -c "import ast,sys; print(' '.join(ast.literal_eval(sys.stdin.read())))")
DENIED=$(jget "['llm']['tools'].get('deny', [])" | python3 -c "import ast,sys; print(' '.join(ast.literal_eval(sys.stdin.read())))")
TOOL_ARGS=()
[ -n "$ALLOWED" ] && TOOL_ARGS+=(--allowedTools "$ALLOWED")
[ -n "$DENIED" ] && TOOL_ARGS+=(--disallowedTools "$DENIED")

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] claude -p <charter:$BEHAVIOR> --model $MODEL${MAX_TURNS:+ --max-turns $MAX_TURNS}${MAX_BUDGET:+ --max-budget-usd $MAX_BUDGET} ${TOOL_ARGS[*]}"
  exit 0
fi

log "pass start (kind=llm charter=$BEHAVIOR model=$MODEL max_turns=${MAX_TURNS:-uncapped} budget=${MAX_BUDGET:+\$}${MAX_BUDGET:-uncapped})"

# A pass killed from OUTSIDE (deploy cutover stopping the container, operator `podman stop`,
# an OOM kill) never reaches the run_report.py call ~40 lines below -- that write happens only
# after `claude -p` returns. Found live 2026-08-26: auto_deploy landed #92 mid-pass, SIGKILLed
# an ad-hoc marie run that had already scored 17 issues, and runs.jsonl got NOTHING. Not
# `killed`, not an error -- no row at all. From the dashboard the pass had never run, while ~$3
# was genuinely spent. Silent loss is the worst failure mode this kit has: every other outcome,
# including a crash, lands a row you can see.
#
# This trap closes that. It fires on SIGTERM (what `podman stop` sends first, and what the
# deploy drain gate below relies on being handled), writing the same shaped record any other
# outcome writes so the run is VISIBLE as interrupted-and-safe-to-rerun. SIGKILL still cannot
# be trapped by anyone -- that is why deploy.sh must drain rather than rely on this alone; the
# two halves are complements, not alternatives.
#
# `exit 143` (128+15) rather than letting the shell die silently: run_report.py maps 137/143 to
# status=killed, so the exit code IS the signal that classifies the row.
#
# THE SUBTLETY THAT MADE THE FIRST VERSION OF THIS USELESS (caught by mutation test, not by
# reading it): bash does not run a trap while a FOREGROUND child is still going -- it defers
# the handler until that child reaps. Measured: SIGTERM at t+1s against a `sleep 60` ran the
# handler at t+60s, not t+1s. A pass sits in `claude -p` for up to an hour, and `podman stop`
# escalates to SIGKILL after 10s, so a deferred handler would NEVER have fired in the exact
# scenario it exists for -- it would have looked correct in review and recorded nothing in
# prod. The trap therefore KILLS the pipeline's children first: that reaps the foreground job,
# bash regains control immediately, and the handler body actually runs inside the grace window.
KILLED_RECORDED=0
record_killed_pass() {
  [ "$KILLED_RECORDED" -eq 1 ] && return 0   # a trap that fires twice must not write two rows
  KILLED_RECORDED=1
  trap - TERM INT EXIT
  # Reap the foreground pipeline (see the note above) -- without this the rest of this function
  # does not run until `claude -p` exits on its own, which under a deploy cutover is never.
  pkill -TERM -P $$ 2>/dev/null || true
  log "pass KILLED by signal -- recording an interrupted run rather than vanishing"
  # No FLEET-REPORT block exists (the pass never finished), so feed empty text and let
  # exit_code alone classify it. --usage-file is omitted deliberately: the real token spend
  # lives in the CLI's unread stream, and inventing a number here would be worse than null.
  printf '' | python3 "$KIT_DIR/scripts/run_report.py" \
    --member "$MEMBER" --run-id "$RUN_ID" --kind llm --exit-code 143 \
    --pass-file - $VISION_FLAG >> "$LOG_DIR/runs.jsonl" 2>>"$LOG" || true
  exit 143
}
trap record_killed_pass TERM INT
# --dangerously-skip-permissions / --setting-sources user: same reasoning as worktree_builder.sh
# -- an unattended pass can't answer an interactive approval prompt, and a target repo's own
# CLAUDE.md/hooks would silently hijack this member's identity otherwise. See that script's
# comment for the measured incident (three full attempts that looked like no-ops).
#
# --output-format stream-json + stream_log.py: logs are cheap, and the wrapper previously only
# recorded pass-start/pass-end -- everything in between was invisible until the whole pass
# finished. This streams "thinking: ...", "tool call: Bash -- ...", "tool result: ..." lines
# into the member's own log AS THEY HAPPEN, piped straight to `log` so a human tailing the log
# (or dumbledore reading it back) sees the pass unfold, not just its outcome.
RESULT_FILE=$(mktemp "${TMPDIR:-/tmp}/fleet_result.XXXXXX")
# the-fixer's own check.sh dedups fires against ONE global state file
# (~/.cache/fleet-kit/the-fixer.state) so the same red SHA never re-fires every 2 minutes. But
# a sub-pass the-fixer spawns for a SPECIFIC stale PR (`run_member.sh the-fixer --item N`,
# charter Step 2) calls that same check.sh, sees the PARENT pass's fire already recorded as
# "already-fighting", and silently no-ops without ever touching the PR it was sent to fix.
# Confirmed live 2026-08-24/25 (PR #3118, #3127, #3149) -- self-diagnosed by the-fixer itself,
# logged as project memory the_fixer_stale_pr_subpass_check_sh_collision, and worked around by
# the-fixer's own passes ever since by refusing to use the fan-out mechanism at all and fixing
# PRs one at a time, in-process, per pass -- meaning two simultaneous fires cost two full paid
# passes in sequence instead of one pass fanning out in parallel, exactly the "take every fire
# around" behavior this override restores. check.sh already supports FIXER_STATE_FILE (built
# for manual dry-run verification, see that script's own header) -- reuse it here: a sub-pass
# scoped to one PR gets its OWN dedup file, keyed by item number, so it can never collide with
# the parent's or another sub-pass's state.
if [ "$MEMBER" = "the-fixer" ] && [ -n "$ITEM" ]; then
  export FIXER_STATE_FILE="$LOG_DIR/.the-fixer-item-${ITEM}.state"
fi

# IS_SANDBOX=1: claude CLI refuses --dangerously-skip-permissions when running as root/sudo
# (a laptop-safety guard -- root there means "someone escalated"). Inside this container root
# IS the only user, by design (see Dockerfile header) -- there's no separate human account the
# guard is protecting. IS_SANDBOX is the CLI's own documented escape hatch for exactly this
# case. Found live on dino 2026-08-21: every real member pass failed rc=1 "other" silently
# (account_pool.sh had no pattern for this error text) until traced to this guard directly.
export IS_SANDBOX=1
# Only pass a cap the spec actually set -- an empty value must not become `--max-turns ""`,
# which the CLI rejects, nor a silent default (see the MAX_TURNS/MAX_BUDGET note above).
CAP_ARGS=()
[ -n "$MAX_TURNS" ] && CAP_ARGS+=(--max-turns "$MAX_TURNS")
[ -n "$MAX_BUDGET" ] && CAP_ARGS+=(--max-budget-usd "$MAX_BUDGET")

# Backgrounded + `wait`, NOT run in the foreground -- this is what makes the SIGTERM trap
# above able to fire at all. Bash defers a trap handler while a foreground child runs, so with
# this pipeline in the foreground the handler would not execute until `claude -p` returned on
# its own (measured: SIGTERM at t+1s ran the handler at t+60s against a 60s sleep). `podman
# stop` SIGKILLs 10s in, so the record would never have been written. With `wait`, bash is
# idle-but-interruptible and the handler runs in ~1s.
#
# The inner subshell re-raises ${PIPESTATUS[0]} as its own exit status because `wait` reports
# the status of the job, which for a bare pipeline is its LAST command (the `while read` loop,
# always 0) -- not claude's. Dropping claude's real code would silently break the
# budget_declined (3) and timed_out (124) classifications that already depend on it, trading
# one fixed status for two broken ones. Verified: a subshell wrapping `(exit 42) | cat` returns
# 42 through `wait`, while the bare pipeline returns 0.
( account_pool_run timeout "$TIMEOUT_S" claude -p "$PROMPT" \
    --model "$MODEL" --dangerously-skip-permissions --setting-sources user \
    --output-format stream-json --verbose \
    "${CAP_ARGS[@]}" "${TOOL_ARGS[@]}" 2>>"$LOG" \
    | python3 "$KIT_DIR/scripts/stream_log.py" --result-out "$RESULT_FILE" \
    | while IFS= read -r line; do log "$line"; done
  exit "${PIPESTATUS[0]}" ) &
PASS_PID=$!
wait "$PASS_PID"
RC=$?

RAW=$(cat "$RESULT_FILE" 2>/dev/null)
rm -f "$RESULT_FILE"
OUT=$(printf '%s' "$RAW" | python3 "$KIT_DIR/scripts/pass_accounting.py" text)
USAGE_FILE=$(mktemp "${TMPDIR:-/tmp}/fleet_usage.XXXXXX")
printf '%s' "$RAW" | python3 "$KIT_DIR/scripts/pass_accounting.py" usage > "$USAGE_FILE" 2>/dev/null

echo "$OUT" | python3 "$KIT_DIR/scripts/run_report.py" \
  --member "$MEMBER" --run-id "$RUN_ID" --kind llm --exit-code "$RC" \
  --pass-file - --usage-file "$USAGE_FILE" $VISION_FLAG >> "$LOG_DIR/runs.jsonl" 2>>"$LOG"
rm -f "$USAGE_FILE"

SUMMARY=$(tail -c 400 <<<"$OUT" | tr '\n' ' ' | tail -c 300)
if [ "$RC" -eq 0 ]; then
  log "pass end rc=0 (account=${ACCOUNT_POOL_SELECTED:-unknown}) :: $SUMMARY"
else
  log "pass end rc=$RC (account=${ACCOUNT_POOL_SELECTED:-none} reason=${ACCOUNT_POOL_LAST_REASON:-}) :: $SUMMARY"
fi
exit "$RC"
