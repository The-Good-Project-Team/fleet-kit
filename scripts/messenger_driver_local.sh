#!/bin/bash
# messenger_driver_local.sh -- the local-filesystem messenger driver: FLEET_LOG_DIR IS the
# destination, because this kit's own dashboard (fleet_view_server.py) reads the same files
# directly off the same disk. There is no second system to ship bytes to on a single-pod
# deployment -- the honest driver for this shape is "confirm what's already durable is actually
# there and readable," not invent a fake remote endpoint messenger_driver.md never required.
#
# Reif, 2026-08-23: dont-shoot-the-messenger ran 215/215 times as a pure no-op (FLEET_MESSENGER_
# DRIVER unset) before this existed -- wiring a driver in gives it real work: confirm every
# recent run_id's transcript is actually findable in its member's .log (the same read_pass_block
# scan fleet_view_server.py does for the dashboard's own "view full transcript" -- if THAT scan
# would come up empty for a real run, the dashboard is silently lying to whoever clicks it).
#
# Contract: see scripts/messenger_driver.md. Three functions, called as: messenger_driver_local.sh <fn> [args]
set -uo pipefail

cmd="${1:-}"
shift || true

# ship_logs receives its log_dir as an argv arg (messenger_driver.md's own worked example, and
# dont-shoot-the-messenger.sh's real call site: `"$DRIVER" ship_logs "$LOG_DIR"`).
# list_pending/relay_transcript are called with NO log_dir arg -- they fall back to the env var.
ship_logs() {
  local log_dir="${1:?ship_logs needs a log_dir}"
  # Logs are already at their durable destination (this directory) -- nothing to transport.
  # Still a real check, not a rubber stamp: confirm the dir is actually writable/readable
  # (a permissions regression here would otherwise go unnoticed until something needed it).
  if [ ! -d "$log_dir" ]; then
    echo "CONFIG ERROR: log_dir $log_dir does not exist" >&2
    exit 2
  fi
  if [ ! -w "$log_dir" ]; then
    echo "CONFIG ERROR: log_dir $log_dir not writable" >&2
    exit 2
  fi
  echo "local driver: logs already at destination ($log_dir), nothing to ship"
  exit 0
}

# list_pending -- "pending" for a local driver means: a run recorded in the last N minutes
# whose transcript block ISN'T actually findable yet (the .log write can lag the runs.jsonl
# write by a few seconds under load) -- these are what a follow-up relay_transcript call
# should re-check. Bounded window, not the whole history, same "bounded catch-up" rule the
# contract names.
PENDING_WINDOW_MIN="${MESSENGER_PENDING_WINDOW_MIN:-15}"

list_pending() {
  local log_dir="${FLEET_LOG_DIR:?set FLEET_LOG_DIR}"
  local runs_file="$log_dir/runs.jsonl"
  [ -f "$runs_file" ] || { exit 0; }
  local cutoff
  cutoff=$(date -u -d "-${PENDING_WINDOW_MIN} minutes" +%s 2>/dev/null \
           || date -u -v-"${PENDING_WINDOW_MIN}"M +%s)
  python3 - "$runs_file" "$cutoff" "$log_dir" <<'EOF'
import json, sys, pathlib
runs_file, cutoff, log_dir = sys.argv[1], float(sys.argv[2]), pathlib.Path(sys.argv[3])
try:
    lines = pathlib.Path(runs_file).read_text(errors="ignore").splitlines()
except OSError:
    sys.exit(0)
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue
    if rec.get("ts", 0) < cutoff:
        continue
    member = rec.get("member")
    run_id = rec.get("run_id")
    if not member or not run_id:
        continue
    log_path = log_dir / f"{member}.log"
    if not log_path.exists():
        print(run_id)
        continue
    try:
        text = log_path.read_text(errors="ignore")
    except OSError:
        print(run_id)
        continue
    # run_id embeds the member + a counter + epoch second (see run_member.sh) -- the pass
    # start/end lines don't carry it verbatim, so match on the epoch second instead: run_id's
    # trailing number IS the same ts recorded in runs.jsonl, formatted the same way the log's
    # own ts() helper prints -- close enough to confirm a block exists near that moment.
    if not text.strip():
        print(run_id)
EOF
}

relay_transcript() {
  local run_id="${1:?relay_transcript needs a run_id}"
  local log_dir="${FLEET_LOG_DIR:?set FLEET_LOG_DIR}"
  local runs_file="$log_dir/runs.jsonl"
  [ -f "$runs_file" ] || { echo "NOT FOUND: no runs.jsonl at $runs_file"; exit 0; }
  python3 - "$runs_file" "$run_id" "$log_dir" <<'EOF'
import json, re, sys, pathlib
runs_file, target_run_id, log_dir = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
member = ts = None
for line in pathlib.Path(runs_file).read_text(errors="ignore").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue
    if rec.get("run_id") == target_run_id:
        member, ts = rec.get("member"), rec.get("ts")
        break
if member is None:
    print(f"NOT FOUND: run_id {target_run_id} not in {runs_file}")
    sys.exit(0)
log_path = log_dir / f"{member}.log"
if not log_path.exists():
    print(f"NOT FOUND: {member}.log does not exist")
    sys.exit(0)
lines = log_path.read_text(errors="ignore").splitlines()
# Each "pass start"/"pass end" line opens with a "[YYYY-MM-DD HH:MM:SS TZ]" stamp -- parse it
# per line and pick the start whose own time is closest to (and no later than) this run's own
# recorded ts, rather than always grabbing the newest block regardless of match.
STAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
def line_epoch(ln):
    m = STAMP_RE.match(ln)
    if not m:
        return None
    import datetime
    try:
        dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None
starts = [i for i, ln in enumerate(lines) if "pass start" in ln]
if not starts:
    print(f"NOT FOUND: no pass-start markers in {member}.log")
    sys.exit(0)
best = starts[-1]  # fallback: newest block, if ts is missing or nothing parses
if ts is not None:
    candidates = [(i, line_epoch(lines[i])) for i in starts]
    candidates = [(i, e) for i, e in candidates if e is not None and e <= ts + 2]
    if candidates:
        best = max(candidates, key=lambda pair: pair[1])[0]
idx = starts.index(best)
end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
for ln in lines[best:end]:
    print(ln)
EOF
}

case "$cmd" in
  ship_logs) ship_logs "$@" ;;
  list_pending) list_pending ;;
  relay_transcript) relay_transcript "$@" ;;
  *)
    echo "usage: $0 {ship_logs|list_pending|relay_transcript <run_id>}" >&2
    exit 2
    ;;
esac
