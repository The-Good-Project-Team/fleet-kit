#!/bin/bash
# auto_deploy_race_check.sh -- surfaces the failure gh#255 found: a raw, uncaught git error in
# $FLEET_LOG_DIR/auto_deploy.cron.log that structurally cannot have come from auto_deploy.sh's
# own guarded code path (its dirty-check runs before any fetch, and its git fetch/pull are both
# scoped to exactly one ref, origin/main -- neither call can produce a multi-branch fast-forward
# error or a ref-lock race). That means some OTHER, unidentified process is running unscoped git
# operations against the same host deploy checkout, on its own schedule, with none of
# auto_deploy.sh's safety checks.
#
# gh#275 added a second, independent detector below: auto_deploy.sh's own two "sanctioned"
# ABORT guards (dirty working tree, diverged HEAD) log one line and exit 1 on every poll tick
# with no escalation of their own -- SANCTIONED_REGEX below exists specifically to EXCLUDE them
# from the gh#255 unrecognized-signature path, so a KNOWN, recurring guard failure had no
# escalation path at all. Three real occurrences (gh#245, gh#255, and gh#275 itself, the third
# self-resolving under deploy_staleness_check.sh's 4h paging budget) were all found only by a
# `nerd` pass doing after-the-fact log forensics. check_sanctioned_escalation() below pages
# after N consecutive sanctioned-ABORT ticks with no intervening successful deploy, instead of
# staying silent for the full 4h staleness budget.
#
# WHY THIS RUNS INSIDE THE CONTAINER (entrypoint.sh's crontab), NOT ON THE HOST like
# auto_deploy.sh itself: this only needs to read/append plain log files under $FLEET_LOG_DIR,
# which is the same directory up.sh bind-mounts from the host (auto_deploy.cron.log is written
# by the HOST crontab's own `>> ... 2>&1` redirect around auto_deploy.sh, but lands in the
# identical bind-mounted path) -- no podman/host access needed. Same shape as
# deploy_staleness_check.sh (gh#201), an independent watchdog with no git/podman needs of its
# own.
#
# WHY DETECTION, NOT A FIX: identifying/killing the second process needs host-level access
# (crontab -l, process list) this pass could not reach -- that is the issue's own named `Ask`
# for a human/host-access member, left fully open. This only makes the symptom loud instead of
# silent, per auto_deploy.sh's own established rule (STALE LOCK, ABORT:): a loud alert over a
# guess, never an auto-remediation attempt. Same rule applies to gh#275's sanctioned-ABORT
# escalation: it only pages, it never touches the dirty tree or the diverged HEAD itself.
#
# Cursor-based, not content-hash dedup: auto_deploy.cron.log/auto_deploy.log are append-only
# and grow forever across every 5-minute tick, so "already scanned up to byte N" is both
# simpler and more correct here than postflight_dirty_check.sh's content-hash approach (built
# for a working tree that can revert to the SAME dirty state repeatedly) -- it still satisfies
# the same rule ("key off the actual dirt, not just have-we-ever-seen-dirt-before",
# scripts/selftest.py:1049): bytes already scanned are never rescanned (no duplicate alert), and
# a genuinely NEW occurrence past the cursor always alerts even if identical text already
# alerted before.
set -uo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "${FLEET_ENV_FILE:-$KIT_DIR/fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-$KIT_DIR/fleet.env}"; set +a; } || true

LOG_DIR="${FLEET_LOG_DIR:?set FLEET_LOG_DIR}"
CRON_LOG="$LOG_DIR/auto_deploy.cron.log"
DEPLOY_LOG="$LOG_DIR/auto_deploy.log"
ALERT_LOG="$LOG_DIR/auto_deploy_race_alerts.log"
STATE_FILE="$LOG_DIR/.auto_deploy_race_check.cursor"
DEPLOY_STATE_FILE="$LOG_DIR/.auto_deploy_race_check.deploy_cursor"
SANCTIONED_STATE_FILE="$LOG_DIR/.auto_deploy_race_check.sanctioned_counts"

# Known-sanctioned lines auto_deploy.sh's own `log()` can legitimately write. An allowlist to
# EXTEND, not a closed set -- auto_deploy.sh could grow new log prefixes later, and a stale
# allowlist here would start false-alerting on its own script (gh#255's own PRD caveat).
SANCTIONED_REGEX='ABORT: working tree dirty|ABORT: local HEAD is not an ancestor|DEPLOY FAILED|STALE LOCK'

# Raw git-failure signatures that structurally cannot come from auto_deploy.sh's own
# single-ref-scoped fetch/pull (see header). Also an allowlist to extend, not a closed set --
# these three are gh#255's own reproduced evidence, not an exhaustive taxonomy of every race
# shape.
SUSPECT_REGEX="cannot lock ref|Cannot fast-forward to multiple branches|would be overwritten by merge"

# gh#275 AC2: 3 consecutive ticks at auto_deploy.sh's 5-minute poll cadence, ~15 minutes --
# the issue's own suggested first-step threshold. UNKNOWN (gh#275 PRD): the exact count/window
# is a judgment call a human may want to tune against a larger recurrence sample than the 3
# occurrences known at filing time; override via FLEET_AUTO_DEPLOY_SANCTIONED_ABORT_THRESHOLD
# if that tuning happens later, rather than editing this default.
SANCTIONED_ABORT_THRESHOLD="${FLEET_AUTO_DEPLOY_SANCTIONED_ABORT_THRESHOLD:-3}"

# gh#278 (2026-09-03): the alert this function writes has always landed only in $ALERT_LOG --
# read by nobody except a fleet member that happens to look, which is why the 2026-09-03
# dirty-tree stall got FIVE separate member passes re-confirming the exact same stuck state
# over 5+ hours (jefe x3, nerd x2) with no human ever paged, while every one of them lacked the
# host access needed to actually clear it. account_health_check.sh/path_health_check.sh/
# tunnel_health_check.sh already page a human via ntfy.sh for exactly this shape of "sanctioned,
# detected, but needs a human's hands" condition -- this wires the same precedent in here.
#
# gh#815: this used to hand-roll its own curl-to-ntfy.sh call, gated on a single env var with
# no fallback and no retry -- a stuck deploy on a box with NTFY_TOPIC unset paged nobody for
# ~24h while 35 consecutive ticks wrote a perfect diagnosis into a file nobody reads. Now routes
# through fleet_alert.sh, the fleet's shared delivery helper: email + ntfy (whichever is
# configured) plus an undelivered-alarm retry queue, same as every other health check in this
# repo. fleet_alert.sh itself never exits non-zero and degrades to a log-only queue entry when
# NO channel is configured, so the "never a hard failure of the detector" guarantee still holds
# without this function re-implementing it.
_ntfy_page() {
    bash "$KIT_DIR/scripts/fleet_alert.sh" "$1" "$2" "$3" \
        || echo "[auto_deploy_race_check] WARNING: fleet_alert.sh failed, could not page: $2" >> "$ALERT_LOG"
}

# gh#255's original detector: an unrecognized (SUSPECT_REGEX) git failure in auto_deploy.cron.log.
# Unchanged by gh#275 -- AC4 requires this path's behavior to stay identical.
check_unrecognized_race() {
    # No cron log yet (auto_deploy.sh has never ticked on this box, or FLEET_LOG_DIR is fresh) --
    # nothing to scan.
    [ -f "$CRON_LOG" ] || return 0

    local last_offset=0
    if [ -f "$STATE_FILE" ]; then
        last_offset="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
    fi
    case "$last_offset" in (''|*[!0-9]*) last_offset=0 ;; esac

    local total_size
    total_size="$(wc -c < "$CRON_LOG" | tr -d ' ')"

    # Log rotated/truncated under us (last_offset now past EOF) -- rescan from the top rather
    # than silently skip whatever is there now.
    [ "$last_offset" -gt "$total_size" ] && last_offset=0

    if [ "$last_offset" -ge "$total_size" ]; then
        return 0  # nothing appended since the last tick
    fi

    local new_content
    new_content="$(tail -c "+$((last_offset + 1))" "$CRON_LOG")"
    echo "$total_size" > "$STATE_FILE"

    local matches
    matches="$(printf '%s\n' "$new_content" | grep -E "$SUSPECT_REGEX" | grep -vE "$SANCTIONED_REGEX")"
    [ -z "$matches" ] && return 0

    local now
    now="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

    # "Did the next tick's deploy subsequently succeed" (AC2) -- auto_deploy.cron.log carries no
    # per-line timestamps of its own (git's raw stderr has none), so exact time-correlation isn't
    # possible from the matched text alone; report the most recent status auto_deploy.log has
    # recorded, same manual cross-reference this issue's own evidence used by hand.
    local next_tick_status="no 'deploy OK'/'DEPLOY FAILED'/'ABORT:' line found yet in auto_deploy.log"
    if [ -f "$DEPLOY_LOG" ]; then
        local last_status_line
        last_status_line="$(grep -E 'deploy OK|DEPLOY FAILED|ABORT:' "$DEPLOY_LOG" | tail -1)"
        [ -n "$last_status_line" ] && next_tick_status="most recent auto_deploy.log status: $last_status_line"
    fi

    while IFS= read -r line; do
        [ -z "$line" ] && continue
        {
            echo "[$now] UNRECOGNIZED git failure outside auto_deploy.sh's own guarded path (gh#255): $line"
            echo "  $next_tick_status"
        } >> "$ALERT_LOG"
    done <<< "$matches"
}

# gh#275: escalate auto_deploy.sh's own sanctioned ABORT guards (dirty tree, diverged HEAD) --
# both write their status line via log() straight to auto_deploy.log (never to stdout, so
# auto_deploy.cron.log never carries them; that's why this reads DEPLOY_LOG, not CRON_LOG).
# Two independent per-reason counters (gh#275 PRD's own resolved open question: dirty-tree and
# diverged-HEAD have different underlying causes, so one guard's streak doesn't mask the
# other's) -- both reset only by an intervening "deploy OK". Edge-triggered: alerts once when a
# streak first reaches the threshold, then stays quiet on that same streak until it resolves
# (a successful deploy) so a stuck host doesn't repage every 5 minutes for hours.
check_sanctioned_escalation() {
    [ -f "$DEPLOY_LOG" ] || return 0

    local last_offset=0
    if [ -f "$DEPLOY_STATE_FILE" ]; then
        last_offset="$(cat "$DEPLOY_STATE_FILE" 2>/dev/null || echo 0)"
    fi
    case "$last_offset" in (''|*[!0-9]*) last_offset=0 ;; esac

    local total_size
    total_size="$(wc -c < "$DEPLOY_LOG" | tr -d ' ')"

    # Log rotated/truncated under us -- rescan from the top (same as check_unrecognized_race).
    # The persisted streak counts below are just as stale as the cursor in that case: they were
    # counting ticks in a log incarnation that no longer exists, so carrying them into the
    # rescanned content would understate how NEW a resumed streak actually is. Reset both
    # together, not just the cursor.
    local rotated=0
    if [ "$last_offset" -gt "$total_size" ]; then
        last_offset=0
        rotated=1
    fi

    if [ "$last_offset" -ge "$total_size" ]; then
        return 0  # nothing appended since the last tick
    fi

    local new_content
    new_content="$(tail -c "+$((last_offset + 1))" "$DEPLOY_LOG")"
    echo "$total_size" > "$DEPLOY_STATE_FILE"

    local dirty_count=0 dirty_alerted=0 diverged_count=0 diverged_alerted=0
    if [ "$rotated" -eq 0 ] && [ -f "$SANCTIONED_STATE_FILE" ]; then
        read -r dirty_count dirty_alerted diverged_count diverged_alerted \
            < "$SANCTIONED_STATE_FILE" 2>/dev/null || true
    fi
    case "$dirty_count" in (''|*[!0-9]*) dirty_count=0 ;; esac
    case "$dirty_alerted" in (''|*[!0-9]*) dirty_alerted=0 ;; esac
    case "$diverged_count" in (''|*[!0-9]*) diverged_count=0 ;; esac
    case "$diverged_alerted" in (''|*[!0-9]*) diverged_alerted=0 ;; esac

    local now
    now="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

    while IFS= read -r line; do
        [ -z "$line" ] && continue
        case "$line" in
            *'ABORT: working tree dirty'*)
                dirty_count=$((dirty_count + 1))
                ;;
            *'ABORT: local HEAD is not an ancestor'*)
                diverged_count=$((diverged_count + 1))
                ;;
            *'deploy OK'*)
                # Page the recovery too, once, only if this streak actually paged someone --
                # a human who got paged at threshold deserves an explicit all-clear instead of
                # having to keep polling $ALERT_LOG to learn it self-resolved.
                if [ "$dirty_alerted" -eq 1 ]; then
                    _ntfy_page "fleet-kit: deploy stall cleared" \
                        "auto_deploy.sh's working-tree-dirty stall recovered after $dirty_count stuck ticks -- deploy OK." "default"
                fi
                if [ "$diverged_alerted" -eq 1 ]; then
                    _ntfy_page "fleet-kit: deploy stall cleared" \
                        "auto_deploy.sh's diverged-HEAD stall recovered after $diverged_count stuck ticks -- deploy OK." "default"
                fi
                dirty_count=0; dirty_alerted=0
                diverged_count=0; diverged_alerted=0
                ;;
        esac
    done <<< "$new_content"

    # Alert decision is made once, on the FINAL state after the whole batch -- not per-line
    # mid-batch -- so a streak that crosses the threshold and then self-resolves later in the
    # same scanned batch (e.g. this detector catching up after its own downtime) reads as
    # resolved, not stuck, same as AC3's "self-resolving" case.
    if [ "$dirty_count" -ge "$SANCTIONED_ABORT_THRESHOLD" ] && [ "$dirty_alerted" -eq 0 ]; then
        echo "[$now] SANCTIONED ABORT stuck (gh#275): working tree dirty has recurred $dirty_count consecutive ticks with no intervening 'deploy OK' -- host checkout needs manual resolution." >> "$ALERT_LOG"
        _ntfy_page "fleet-kit: deploy stalled" \
            "auto_deploy.sh has hit ABORT: working tree dirty for $dirty_count consecutive ticks with no successful deploy in between. Host checkout needs manual resolution (git status/stash/reset on the host)." \
            "high"
        dirty_alerted=1
    fi
    if [ "$diverged_count" -ge "$SANCTIONED_ABORT_THRESHOLD" ] && [ "$diverged_alerted" -eq 0 ]; then
        echo "[$now] SANCTIONED ABORT stuck (gh#275): local HEAD is not an ancestor of origin/main has recurred $diverged_count consecutive ticks with no intervening 'deploy OK' -- host checkout needs manual resolution." >> "$ALERT_LOG"
        _ntfy_page "fleet-kit: deploy stalled" \
            "auto_deploy.sh has hit ABORT: local HEAD diverged from origin/main for $diverged_count consecutive ticks with no successful deploy in between. Host checkout needs manual resolution." \
            "high"
        diverged_alerted=1
    fi

    echo "$dirty_count $dirty_alerted $diverged_count $diverged_alerted" > "$SANCTIONED_STATE_FILE"
}

check_sanctioned_escalation
check_unrecognized_race
