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
#
# CREDENTIAL SWITCHING: each account name in FLEET_ACCOUNTS is used verbatim as a
# CLAUDE_CONFIG_DIR suffix — account "foo" runs under $HOME/.claude-foo. Log in each account
# once, by hand, before scheduling anything:
#   CLAUDE_CONFIG_DIR="$HOME/.claude-foo" claude setup-token
# Without this, every account in the pool shares whatever is logged into the CLI's default
# config dir, and failover is theater — the pool "tries" N names but only ever authenticates
# as one identity.
set -uo pipefail

ACCOUNT_POOL_ORDER="${FLEET_ACCOUNTS:-primary}"
ACCOUNT_POOL_LOG_FILE="${ACCOUNT_POOL_LOG_FILE:-${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}/account-pool.log}"
ACCOUNT_POOL_STATE_FILE="${ACCOUNT_POOL_STATE_FILE:-${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}/account-pool-exhausted.state}"
# gh#134: consecutive-failure counter for the 'other' (transient) reason ONLY -- kept in a
# separate file so a still-below-threshold streak never shows up in $ACCOUNT_POOL_STATE_FILE
# (the file account_readiness.sh reads for gating) until it actually trips. 'unauthenticated'
# does not use this counter; it gates on the first occurrence (see
# _account_pool_note_unauthenticated for why). REASONED DEFAULT, not measured: the issue that
# added this left the exact threshold UNKNOWN (no attempt-history data was available), so 3
# consecutive 'other' failures for the same account was picked as "more than one blip, fewer
# than an easy human tolerance for a bad string of ticks."
ACCOUNT_POOL_STREAK_FILE="${ACCOUNT_POOL_STREAK_FILE:-${ACCOUNT_POOL_STATE_FILE}.streaks}"
ACCOUNT_POOL_OTHER_FAILURE_THRESHOLD="${ACCOUNT_POOL_OTHER_FAILURE_THRESHOLD:-3}"

_account_pool_log() {
  mkdir -p "$(dirname "$ACCOUNT_POOL_LOG_FILE")" 2>/dev/null
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] account_pool: $*" >> "$ACCOUNT_POOL_LOG_FILE"
}

# _account_pool_parse_reset <output> — pull a reset time out of the CLI's own wording
# ("resets 1pm (UTC)", confirmed live wording as of 2026-08-24) and print its next epoch
# occurrence, or nothing if the output didn't carry one. `date -d`/`date -j` differ (GNU vs
# BSD) -- try GNU first, fall back to BSD (this kit runs in a Linux container, but keep the
# fallback so account_pool.sh stays portable to a Mac host running it directly).
_account_pool_parse_reset() {
  local out="$1" hh ampm epoch now mon day
  now=$(date +%s)
  # Weekly-limit form first: "resets Sep 4, 12am (UTC)" -- has a month/day the hour-only form
  # below doesn't, and must be tried first or a weekly cap falls through to the 5-minute
  # fallback and hammers a still-exhausted account every tick until the real reset (confirmed
  # live on dino 2026-09-01: gmail's real "resets Sep 4, 12am (UTC)" wasn't recognized by
  # either branch, gated 5min, re-failed, re-gated, forever -- until the true Sep4 reset).
  read -r mon day hh ampm < <(grep -ioE "resets [A-Za-z]{3} [0-9]{1,2}, [0-9]{1,2}(am|pm) \(UTC\)" <<<"$out" \
    | head -1 | grep -ioE "[A-Za-z]{3} [0-9]{1,2}, [0-9]{1,2}(am|pm)" \
    | sed -E 's/^([A-Za-z]{3}) ([0-9]{1,2}), ([0-9]{1,2})(am|pm)$/\1 \2 \3 \4/')
  if [ -n "$mon" ]; then
    [ "$ampm" = "pm" ] && [ "$hh" -ne 12 ] && hh=$((hh + 12))
    [ "$ampm" = "am" ] && [ "$hh" -eq 12 ] && hh=0
    epoch=$(TZ=UTC date -u -d "$mon $day $(TZ=UTC date -u +%Y) $hh:00:00" +%s 2>/dev/null) \
      || epoch=$(TZ=UTC date -j -u -f "%b %d %Y %H:%M:%S" "$mon $day $(TZ=UTC date -u +%Y) $(printf '%02d' "$hh"):00:00" +%s 2>/dev/null)
    if [[ "$epoch" =~ ^[0-9]+$ ]]; then
      # a date already past this year means next year, not last year.
      [ "$epoch" -le "$now" ] && epoch=$(TZ=UTC date -u -d "$mon $day $(($(TZ=UTC date -u +%Y) + 1)) $hh:00:00" +%s 2>/dev/null)
      [[ "$epoch" =~ ^[0-9]+$ ]] && { echo "$epoch"; return; }
    fi
  fi
  read -r hh ampm < <(grep -ioE "resets [0-9]{1,2}(am|pm) \(UTC\)" <<<"$out" \
    | head -1 | grep -ioE "[0-9]{1,2}(am|pm)" | sed -E 's/^([0-9]{1,2})(am|pm)$/\1 \2/')
  [ -z "$hh" ] && return 1
  [ "$ampm" = "pm" ] && [ "$hh" -ne 12 ] && hh=$((hh + 12))
  [ "$ampm" = "am" ] && [ "$hh" -eq 12 ] && hh=0
  # GNU first (the container this runs in), then BSD (a Mac host running the kit directly).
  # The BSD form needs the DATE spelled out -- `date -j -f "%H:%M" "13:00"` does not mean
  # "13:00 today" and quietly returns something else, which then fed the "+86400" branch below
  # and produced a bogus gate (caught by selftest on macOS, 2026-08-25).
  local today
  today=$(TZ=UTC date -u +%Y-%m-%d)
  epoch=$(TZ=UTC date -u -d "$today $hh:00:00" +%s 2>/dev/null) \
    || epoch=$(TZ=UTC date -j -u -f "%Y-%m-%d %H:%M:%S" "$today $(printf '%02d' "$hh"):00:00" +%s 2>/dev/null)
  # A non-numeric or empty result means neither date(1) understood us -- say so instead of
  # gating on garbage.
  [[ "$epoch" =~ ^[0-9]+$ ]] || return 1
  # "resets 1pm" said about a time already past today means tomorrow, not an hour ago.
  [ "$epoch" -le "$now" ] && epoch=$(( epoch + 86400 ))
  echo "$epoch"
}

# _account_pool_mark_exhausted <account> <output> — record that $account is gated until the
# reset time embedded in $output, so the NEXT tick's budget-verdict check skips it without
# spending a call.
#
# The no-reset-time fallback is 5 MINUTES, not an hour (Reif, 2026-08-25). A real Anthropic
# limit states its own reset ("resets 1pm (UTC)") and _account_pool_parse_reset picks it up;
# output that claims exhaustion WITHOUT naming a reset is, empirically, not a real limit at
# all -- every gate written on dino 2026-08-25 was this fallback firing on misclassified
# output, and each one blinded both accounts for a full hour. One wrong 5-minute skip costs a
# single tick; one wrong 1-hour skip costs twelve, and the fleet cannot fix anything (this bug
# included) while it is gated. Bias the failure toward re-trying too eagerly, never toward
# staying dark: a genuine limit re-reports itself on the next call and re-gates for free.
_account_pool_mark_exhausted() {
  local account="$1" out="$2" epoch
  epoch=$(_account_pool_parse_reset "$out") || epoch=$(( $(date +%s) + 300 ))
  mkdir -p "$(dirname "$ACCOUNT_POOL_STATE_FILE")" 2>/dev/null
  grep -v "^${account} " "$ACCOUNT_POOL_STATE_FILE" 2>/dev/null > "${ACCOUNT_POOL_STATE_FILE}.tmp" || true
  echo "$account $epoch" >> "${ACCOUNT_POOL_STATE_FILE}.tmp"
  mv "${ACCOUNT_POOL_STATE_FILE}.tmp" "$ACCOUNT_POOL_STATE_FILE"
  _account_pool_log "account=$account marked gated until epoch=$epoch ($(date -d "@$epoch" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || date -r "$epoch" '+%Y-%m-%d %H:%M UTC' 2>/dev/null))"
}

# _account_pool_note_unauthenticated <account> -- gh#134. account_readiness.sh's ONLY signal is
# $ACCOUNT_POOL_STATE_FILE, so before this an account stuck logged-out never appeared gated --
# readiness kept reporting it healthy for the whole outage, the exact failure this issue exists
# to close. Writes a 3rd column ("unauthenticated") so this entry never reads back as a real
# weekly-limit gate -- account_readiness.sh and _account_pool_budget_verdict both key off
# column 2 (the epoch) alone, so this stays a drop-in extension of the existing 2-column format
# rather than a format change for the exhausted branch.
#
# Gates on the FIRST occurrence, unlike the 'other' branch below -- a login failure is
# unambiguous (revoked token, logged out), not the kind of thing a retry fixes. The existing
# success path already clears an account's state-file line on its very next successful call
# regardless of tag, so a human re-authenticating self-heals this without a separate "clear the
# gate" step.
#
# Gate window (1 hour) is a REASONED DEFAULT, not measured -- the issue left the right duration
# UNKNOWN (auth outages have no natural reset time the way a weekly cap does). Long enough that
# a logged-out account doesn't burn a call and a rotation slot every single tick while waiting
# on a human; short enough to self-correct within a work day even if nobody is paged.
_account_pool_note_unauthenticated() {
  local account="$1" epoch
  epoch=$(( $(date +%s) + 3600 ))
  mkdir -p "$(dirname "$ACCOUNT_POOL_STATE_FILE")" 2>/dev/null
  grep -v "^${account} " "$ACCOUNT_POOL_STATE_FILE" 2>/dev/null > "${ACCOUNT_POOL_STATE_FILE}.tmp" || true
  echo "$account $epoch unauthenticated" >> "${ACCOUNT_POOL_STATE_FILE}.tmp"
  mv "${ACCOUNT_POOL_STATE_FILE}.tmp" "$ACCOUNT_POOL_STATE_FILE"
  _account_pool_log "account=$account marked gated (unauthenticated) until epoch=$epoch -- needs a human to re-auth"
}

# _account_pool_note_other_failure <account> -- gh#134. A single transient error (a network
# blip, a one-off 5xx) must not gate an account -- only $ACCOUNT_POOL_OTHER_FAILURE_THRESHOLD
# CONSECUTIVE 'other' failures for the SAME account do. The streak itself lives in
# $ACCOUNT_POOL_STREAK_FILE, separate from the readiness state file, so a below-threshold
# streak is invisible to account_readiness.sh until it actually trips.
#
# Trip gate window (5 minutes) mirrors the existing "unparseable exhaustion" fallback above --
# same reasoning: bias toward re-trying too eagerly on an ambiguous classification rather than
# staying dark.
_account_pool_note_other_failure() {
  local account="$1" count epoch
  mkdir -p "$(dirname "$ACCOUNT_POOL_STREAK_FILE")" 2>/dev/null
  count=$(awk -v a="$account" '$1==a{print $2}' "$ACCOUNT_POOL_STREAK_FILE" 2>/dev/null | tail -1)
  [[ "$count" =~ ^[0-9]+$ ]] || count=0
  count=$((count + 1))
  grep -v "^${account} " "$ACCOUNT_POOL_STREAK_FILE" 2>/dev/null > "${ACCOUNT_POOL_STREAK_FILE}.tmp" || true
  echo "$account $count" >> "${ACCOUNT_POOL_STREAK_FILE}.tmp"
  mv "${ACCOUNT_POOL_STREAK_FILE}.tmp" "$ACCOUNT_POOL_STREAK_FILE"
  if [ "$count" -ge "$ACCOUNT_POOL_OTHER_FAILURE_THRESHOLD" ]; then
    epoch=$(( $(date +%s) + 300 ))
    mkdir -p "$(dirname "$ACCOUNT_POOL_STATE_FILE")" 2>/dev/null
    grep -v "^${account} " "$ACCOUNT_POOL_STATE_FILE" 2>/dev/null > "${ACCOUNT_POOL_STATE_FILE}.tmp" || true
    echo "$account $epoch other" >> "${ACCOUNT_POOL_STATE_FILE}.tmp"
    mv "${ACCOUNT_POOL_STATE_FILE}.tmp" "$ACCOUNT_POOL_STATE_FILE"
    _account_pool_log "account=$account hit $count consecutive 'other' failures, marked gated (other) until epoch=$epoch"
    _account_pool_clear_streak "$account"
  else
    _account_pool_log "account=$account 'other' failure $count/$ACCOUNT_POOL_OTHER_FAILURE_THRESHOLD consecutive -- not gating yet"
  fi
}

# _account_pool_clear_streak <account> -- reset the 'other' consecutive-failure counter. Called
# on a successful call (whatever was transient has passed) and once a gate actually trips
# (the streak's job is done; the state-file gate takes over). AC: "a single transient failure
# followed by a success does NOT trip the new gated state" -- this is what makes that true.
_account_pool_clear_streak() {
  local account="$1"
  [ -f "$ACCOUNT_POOL_STREAK_FILE" ] || return 0
  grep -v "^${account} " "$ACCOUNT_POOL_STREAK_FILE" > "${ACCOUNT_POOL_STREAK_FILE}.tmp" 2>/dev/null || true
  mv "${ACCOUNT_POOL_STREAK_FILE}.tmp" "$ACCOUNT_POOL_STREAK_FILE" 2>/dev/null || true
}

# Extension point: define this yourself (before sourcing this file, or export the function)
# to consult your own budget-tracking API. Must print one of: ok | gated:<reason> | unknown.
# Default here reads the exhaustion state file this module writes on its own (see
# _account_pool_mark_exhausted) -- an account we already know is exhausted, with a reset time
# still in the future, is skipped without spending another call. Override still works: define
# your own function (e.g. to consult a real budget API) and it takes precedence via normal
# shell function redefinition semantics -- source this file first, then redefine.
_account_pool_budget_verdict() {
  local account="$1" epoch now
  [ -f "$ACCOUNT_POOL_STATE_FILE" ] || { echo "unknown"; return; }
  epoch=$(awk -v a="$account" '$1==a{print $2}' "$ACCOUNT_POOL_STATE_FILE" | tail -1)
  [ -z "$epoch" ] && { echo "unknown"; return; }
  now=$(date +%s)
  if [ "$epoch" -gt "$now" ]; then
    echo "gated:exhausted_until_$epoch"
  else
    echo "unknown"
  fi
}

# _account_pool_order — print the accounts to try, soonest-reset FIRST.
#
# WHY (Reif, 2026-09-05): quota that resets in an hour is worth less than quota that resets in
# five days, because the near-reset account loses whatever it did not spend. Draining the
# soonest-resetting account first, and only failing over once it is genuinely exhausted,
# extracts more total work from the same two subscriptions. A fixed order does the opposite:
# it always drains the SAME account first, so the other one's window can lapse with quota
# unspent.
#
# WHAT WE ACTUALLY KNOW, and what we do not. This module only learns a reset time when an
# account FAILS as exhausted and the CLI names its own reset (_account_pool_parse_reset), and
# the success path deliberately CLEARS that line the moment the account works again. So a
# healthy account has NO known reset here, and there is no API this kit can read for one. That
# is a real limit, not an oversight -- so this function must never pretend otherwise:
#
#   - accounts with a KNOWN future reset sort first, soonest epoch first (these are gated, and
#     _account_pool_budget_verdict will skip them regardless of position -- see below)
#   - accounts with a reset epoch already in the PAST ("lapsed") sort next, soonest-passed
#     epoch first
#   - accounts with NO recorded epoch at all ("never gated") keep their FLEET_ACCOUNTS order,
#     last
#
# gh#462: the first cut of this function only had two buckets -- "known future" and
# "everything else" -- which silently merged "lapsed" into "never gated" and let both fall
# back to unmodified FLEET_ACCOUNTS order. That made the whole feature a no-op in every
# reachable state: the "known future" bucket is exactly what _account_pool_budget_verdict
# gates and skips no matter where it sits, and the "lapsed" case -- the PR's own stated payoff
# scenario, a just-reset account that should be drained before a never-gated one -- was
# reordered right back to FLEET_ACCOUNTS order, identical to no fix at all. A lapsed account is
# distinguishable from a never-gated one: it still has a state-file line, just with an epoch
# that has already passed, whereas a never-gated account has no line there at all. Splitting
# that line out into its own bucket is what makes the ordering context-sensitive instead of
# reshuffling only accounts guaranteed to be skipped.
#
# The third group (never gated) is the normal steady state (every account healthy, nothing
# ever gated), so this function is still a NO-OP exactly when nothing is known -- the fixed
# order still governs and behavior is identical to before the feature existed.
#
# INTERACTION with _account_pool_budget_verdict: an account whose gate is still in the future is
# SKIPPED by the caller without spending a call. So a soonest-reset that has not arrived yet is
# not tried early -- it is skipped, exactly as before. What this ordering changes is the window
# AFTER a gate expires: the just-reset (lapsed) account is the one whose fresh quota is most
# perishable, and it is now drained ahead of a never-gated account instead of sitting behind
# whatever FLEET_ACCOUNTS listed first.
#
# An 'unauthenticated' or 'other' entry carries an epoch too (a 1h / 5m holdoff, not a real
# quota reset). Sorting on it is still correct in both the known-future and lapsed buckets: it
# is a time-to-retry, and the account that becomes (or became) retryable soonest is the right
# one to reach for first.
#
# Any malformed epoch is treated as never-gated rather than sorted as garbage -- an unreadable
# state file must never reorder the pool into nonsense.
_account_pool_order() {
  local account epoch now known="" lapsed="" unknown=""
  now=$(date +%s)
  for account in $ACCOUNT_POOL_ORDER; do
    epoch=""
    if [ -f "$ACCOUNT_POOL_STATE_FILE" ]; then
      epoch=$(awk -v a="$account" '$1==a{print $2}' "$ACCOUNT_POOL_STATE_FILE" 2>/dev/null | tail -1)
    fi
    if [[ "$epoch" =~ ^[0-9]+$ ]] && [ "$epoch" -gt "$now" ]; then
      known="${known}${epoch} ${account}"$'\n'
    elif [[ "$epoch" =~ ^[0-9]+$ ]]; then
      lapsed="${lapsed}${epoch} ${account}"$'\n'
    else
      unknown="${unknown}${account}"$'\n'
    fi
  done
  {
    [ -n "$known" ] && printf '%s' "$known" | sort -n | awk '{print $2}'
    [ -n "$lapsed" ] && printf '%s' "$lapsed" | sort -n | awk '{print $2}'
    [ -n "$unknown" ] && printf '%s' "$unknown"
  } | grep -v '^$' || true
}

# _account_pool_classify_failure <combined_output> — best-effort text match. Real accounts
# report exhaustion/auth failure in human-readable text on stderr; there is no structured
# error code from the CLI to key off instead. Extend the patterns below if your provider's
# wording differs.
#
# Patterns confirmed against REAL CLI output, live on dino 2026-08-21: the original
# "reached your (weekly|usage) limit" missed the CLI's actual wording ("You've HIT your weekly
# limit"), and neither auth pattern matched "OAuth access token has been revoked" — both real
# failures were silently misclassified as "other", which the caller then retries every account
# for every single tick instead of logging the true, actionable reason once.
#
# `rate.?limit` REMOVED as a standalone pattern, Reif 2026-08-25, after it took the whole fleet
# down for ~2h. It matched the bare words "rate limit" ANYWHERE in the captured output -- and
# this pool `tee`s the command's full stdout, not just stderr, so an agent that merely RAN
# `gh api rate_limit` (gru does exactly this, every pass, to check GitHub quota) printed the
# phrase into its own transcript and got its account gated for an hour on the strength of it.
# A real Anthropic limit always names itself ("you've hit your weekly limit"), so requiring
# that wording costs nothing and stops the fleet from gating itself on its own log text. HTTP
# 429 is kept, but anchored to the status code rather than prose. Sin #1 of a self-running
# system is running itself out of tokens: everything else, it can fix.
_account_pool_classify_failure() {
  local out="$1"
  if grep -qiE "(reached|hit) your (weekly|usage|5-hour|session) limit|quota exceeded|\b429\b|too many requests" <<<"$out"; then
    echo "exhausted"; return
  fi
  if grep -qiE "not logged in|please (log|sign) in|invalid api key|unauthorized|token (has been )?revoked|401" <<<"$out"; then
    echo "unauthenticated"; return
  fi
  echo "other"
}

# account_pool_run <cmd...> — the main entry point. Sets ACCOUNT_POOL_SELECTED,
# ACCOUNT_POOL_LAST_REASON on return. Return codes: 0 success, 2 exhausted-this-account (only
# meaningful mid-loop), 3 ALL_ACCOUNTS_EXHAUSTED, 4 unauthenticated, 1 other failure.
#
# STREAMS LIVE, still classifies failure: output goes to stdout AS THE COMMAND PRODUCES IT
# (via `tee`, not `out=$(...)` capture-then-replay) so a caller piping this into stream_log.py
# for a live-tailed log actually sees lines in real time -- capturing the whole command's
# output into a variable first (the kit's original shape) defeats that entirely, the caller
# would see nothing until the command exits. A copy still lands in a temp file for
# `_account_pool_classify_failure` to read after the command exits, same classification logic
# as before, just sourced from a file `tee` also wrote instead of a buffered variable.
account_pool_run() {
  local account verdict rc capture
  export ACCOUNT_POOL_SELECTED="" ACCOUNT_POOL_LAST_REASON=""
  capture=$(mktemp "${TMPDIR:-/tmp}/account_pool_out.XXXXXX")
  trap 'rm -f "$capture"' RETURN
  for account in $(_account_pool_order); do
    verdict=$(_account_pool_budget_verdict "$account" 2>/dev/null || echo "unknown")
    case "$verdict" in
      gated*)
        _account_pool_log "account=$account budget verdict=$verdict, skipping without spending a call"
        continue
        ;;
    esac
    : > "$capture"
    # A pool account beyond the first needs ITS OWN token, not the one CLAUDE_CODE_OAUTH_TOKEN
    # already holds globally (that's whichever account this file's fleet.env was written for --
    # sharing it across every account defeats the whole point of a pool, same failure this
    # header already warns about for a shared default CLAUDE_CONFIG_DIR). Reif, 2026-08-22:
    # look up CLAUDE_CODE_OAUTH_TOKEN_<ACCOUNT> (account name upper-cased, non-alnum -> "_",
    # e.g. account "claude-reif" -> CLAUDE_CODE_OAUTH_TOKEN_CLAUDE_REIF) and use IT for this
    # account's own CLAUDE_CONFIG_DIR if set; otherwise fall through to whatever
    # CLAUDE_CODE_OAUTH_TOKEN already is (the single-account "primary" case, unchanged).
    #
    # BUG (found live on dino 2026-09-01): the "fall through" above silently left the ambient
    # CLAUDE_CODE_OAUTH_TOKEN set for every account that has no override -- and the CLI
    # prioritizes that env var over credentials.json in CLAUDE_CONFIG_DIR. So "gmail" (no
    # CLAUDE_CODE_OAUTH_TOKEN_GMAIL configured) was silently authenticating as whichever
    # account's token fleet.env's bare CLAUDE_CODE_OAUTH_TOKEN belongs to (tgp) -- hit tgp's
    # real weekly cap, reported the failure under gmail's name. Failover was theater: it never
    # actually tried gmail's own login. Only the literal "primary" account (the
    # FLEET_ACCOUNTS-unset default, single-account case the comment above describes) may
    # legitimately inherit the ambient token; every other named account must use ITS OWN
    # credentials.json or none at all -- clear the var rather than let it leak.
    local var_name token_override
    var_name="CLAUDE_CODE_OAUTH_TOKEN_$(echo "$account" | tr '[:lower:]-' '[:upper:]_')"
    token_override="${!var_name:-}"
    if [ -n "$token_override" ]; then
      CLAUDE_CONFIG_DIR="$HOME/.claude-$account" CLAUDE_CODE_OAUTH_TOKEN="$token_override" \
        "$@" 2>&1 | tee "$capture"
    elif [ "$account" = "primary" ]; then
      CLAUDE_CONFIG_DIR="$HOME/.claude-$account" "$@" 2>&1 | tee "$capture"
    else
      CLAUDE_CONFIG_DIR="$HOME/.claude-$account" env -u CLAUDE_CODE_OAUTH_TOKEN \
        "$@" 2>&1 | tee "$capture"
    fi
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ]; then
      export ACCOUNT_POOL_SELECTED="$account"
      export ACCOUNT_POOL_LAST_REASON=""
      # The caller runs this pool inside a SUBSHELL (run_member.sh wraps the pipeline in
      # `( ... ) &` to re-raise PIPESTATUS[0] through `wait`), so the exports above die with
      # that subshell and the parent logged `account=unknown` on every pass. Write the
      # selection where the parent can read it back.
      [ -n "${ACCOUNT_POOL_SELECTED_FILE:-}" ] && printf '%s\n' "$account" > "$ACCOUNT_POOL_SELECTED_FILE" 2>/dev/null
      # Log the success. account_health_check.sh measures outage length as "time since the last
      # success" -- without this line the log holds only failures, so there is nothing to
      # measure from and the pager cannot tell a 5-minute blip from a 5-hour outage.
      _account_pool_log "account=$account call succeeded"
      # a stale gate for THIS account is now wrong (it just succeeded) -- clear it so a
      # manual credit top-up or an early reset isn't stuck honoring the old estimate.
      if [ -f "$ACCOUNT_POOL_STATE_FILE" ] && grep -q "^${account} " "$ACCOUNT_POOL_STATE_FILE" 2>/dev/null; then
        grep -v "^${account} " "$ACCOUNT_POOL_STATE_FILE" > "${ACCOUNT_POOL_STATE_FILE}.tmp" || true
        mv "${ACCOUNT_POOL_STATE_FILE}.tmp" "$ACCOUNT_POOL_STATE_FILE"
      fi
      # gh#134: a success also means whatever was tripping the 'other' streak has passed --
      # clear it so a single transient failure followed by a success never trips the gate.
      _account_pool_clear_streak "$account"
      return 0
    fi
    reason=$(_account_pool_classify_failure "$(cat "$capture")")
    _account_pool_log "account=$account command failed rc=$rc reason=$reason"
    export ACCOUNT_POOL_LAST_REASON="$reason"
    [ -n "${ACCOUNT_POOL_REASON_FILE:-}" ] && printf '%s\n' "$reason" > "$ACCOUNT_POOL_REASON_FILE" 2>/dev/null
    case "$reason" in
      exhausted) _account_pool_mark_exhausted "$account" "$(cat "$capture")"; continue ;;
      unauthenticated) _account_pool_note_unauthenticated "$account"; continue ;;
      *) _account_pool_note_other_failure "$account"; continue ;;   # a transient/other failure
                        # still tries the next account rather than giving up on the whole pool
                        # over one bad tick -- see _account_pool_note_other_failure for why it
                        # doesn't gate on a single occurrence
    esac
  done
  _account_pool_log "ALL accounts in '$ACCOUNT_POOL_ORDER' failed this call"
  export ACCOUNT_POOL_LAST_REASON="${ACCOUNT_POOL_LAST_REASON:-all_exhausted}"
  return 3
}
