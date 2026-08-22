#!/bin/bash
# run_gru_fanout.sh — cron's entry point for gru.
#
# HISTORY: this script used to compute N (via fanout.py's Fibonacci ladder over headroom) and
# spawn N independent gru instances, each racing to claim its own item -- see git history for
# that version if you need it. Reif, 2026-08-21: gru itself should decide runway/priority/N,
# not have N handed to it by a bash script it can't see the reasoning of (docs/gru-minions.md
# is the full PRD). gru is the orchestrator now -- it reads maxx/fleet_db itself (gru.md steps
# 1-3), claims its own chosen items, and spawns MINION instances itself via `--item <n>` (see
# run_member.sh's --item flag). This script's only remaining job is being cron's one call.
set -uo pipefail

[ -f "${FLEET_ENV_FILE:-./fleet.env}" ] && . "${FLEET_ENV_FILE:-./fleet.env}"
KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

exec bash "$KIT_DIR/scripts/run_member.sh" gru "$@"
