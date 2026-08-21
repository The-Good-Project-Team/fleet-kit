#!/usr/bin/env bash
# fleet_enabled.sh — the master kill switch. Source this AFTER fleet.env is loaded, then call
# fleet_enabled_or_exit at the very top of any script that would spend money or touch the
# board — before a worktree is created, before a claim happens, before anything with a cost.
#
# WHY A FILE FLAG, not a DB row or a server call: the fleet runs off cron with no server
# guaranteed up (this is the same reasoning as the whole kit's board-on-GitHub-Issues choice —
# the control surface must survive the thing it controls being broken). FLEET_ENABLED in
# fleet.env is read fresh every invocation, so toggling it takes effect on the VERY NEXT cron
# tick with no restart, no signal, nothing to forget to bounce.
#
# The dashboard's on/off button (fleet_view_server.py's /api/fleet_enabled) writes this exact
# line into fleet.env via sed -- same file, same variable, so a human editing fleet.env by hand
# and a click on the page are the SAME action, never two mechanisms that can disagree.
set -u

fleet_enabled_or_exit() {
  local who="${1:-fleet member}"
  # FLEET_RUN_NOW=1 is set only by fleet_view_server.py's /api/run_now -- an explicit human
  # click, not a cron tick. The kill switches gate the AUTOMATIC loop; a manual "run this one
  # right now so I can watch it" is a different action on purpose, and would be useless for
  # testing a member you specifically have toggled off to keep it off cron.
  if [ "${FLEET_RUN_NOW:-0}" = "1" ]; then
    return 0
  fi
  if [ "${FLEET_ENABLED:-true}" = "false" ]; then
    if declare -F log >/dev/null 2>&1; then
      log "$who: FLEET_ENABLED=false -- exiting without doing anything"
    fi
    exit 0
  fi
}
