#!/bin/bash
# tunnel_health_check.sh -- page a human when the public tunnel URL is not actually serving
# the fleet view, independent of whether the container/process looks healthy locally.
#
# WHY THIS EXISTS: account_health_check.sh watches the account pool (LLM calls failing).
# Neither that script nor anything else here watches whether the PUBLIC url actually
# resolves to a live backend. Failure modes it misses: cloudflared tunnel ingress pointing
# at a stale/wrong local port, the tunnel service itself down, or the origin process bound
# to a different port than the tunnel expects. All of these leave the container "Up" and
# fleet_view answering fine on localhost while the public URL 502s.
#
# CONFIRMED LIVE 2026-08-25: cloudflared ingress pointed at localhost:8561 while
# fleet_view_server was bound to :8420 (FLEET_VIEW_PORT). Public URL 502'd; nobody paged
# until a human happened to load the page. Same class of gap account_health_check.sh's
# header describes for account exhaustion -- this closes the tunnel-side half of it.
#
# WHAT IT WATCHES: curls PUBLIC_URL, checks for HTTP 200. Plain bash, host-side cron, zero
# Claude Code / LLM dependency -- same reasoning as account_health_check.sh and
# auto_deploy.sh: the thing doing the watching must not depend on the thing being watched.
#
# STATE: one marker file remembers whether we already paged for the CURRENT outage, so a
# 5-minute cron doesn't re-page every tick -- one page per outage, one recovery page after.
#
# Usage (cron, mirrors account_health_check.sh's own invocation shape):
#   */5 * * * * PUBLIC_URL=https://dino.luckymachines.co/ NTFY_TOPIC=<topic> \
#     bash scripts/tunnel_health_check.sh >> .../tunnel_health_check.cron.log 2>&1
set -uo pipefail

PUBLIC_URL="${PUBLIC_URL:?set PUBLIC_URL -- the public URL to check, e.g. https://dino.luckymachines.co/}"
NTFY_TOPIC="${NTFY_TOPIC:?set NTFY_TOPIC -- the ntfy.sh topic to page}"
STATE_FILE="${TUNNEL_HEALTH_STATE_FILE:-/home/ubuntu/fleet-kit-logs/.tunnel_health_paged.state}"

already_paged=""
[ -f "$STATE_FILE" ] && already_paged=$(cat "$STATE_FILE")

_ntfy() {
  local title="$1" msg="$2" priority="$3"
  curl -sf -o /dev/null \
    -H "Title: $title" -H "Priority: $priority" -H "Tags: warning" \
    -d "$msg" "https://ntfy.sh/$NTFY_TOPIC" \
    || echo "[tunnel_health_check] WARNING: ntfy POST failed, could not page"
}

http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$PUBLIC_URL")

if [ "$http_code" = "200" ]; then
  if [ -n "$already_paged" ]; then
    _ntfy "fleet-kit: tunnel recovered" \
      "$PUBLIC_URL is serving again (HTTP 200) after an outage flagged at $already_paged." \
      "default"
    rm -f "$STATE_FILE"
  fi
  echo "[tunnel_health_check] healthy -- $PUBLIC_URL returned 200"
  exit 0
fi

if [ -z "$already_paged" ]; then
  paged_at="$(date -u '+%Y-%m-%d %H:%M UTC')"
  _ntfy "🚨 fleet-kit: public URL down" \
    "$PUBLIC_URL returned HTTP ${http_code:-timeout}, not 200. Check cloudflared tunnel ingress port vs FLEET_VIEW_PORT." \
    "urgent"
  echo "$paged_at" > "$STATE_FILE"
  echo "[tunnel_health_check] PAGED -- $PUBLIC_URL returned $http_code"
else
  echo "[tunnel_health_check] still down (HTTP ${http_code:-timeout}), already paged at $already_paged"
fi
