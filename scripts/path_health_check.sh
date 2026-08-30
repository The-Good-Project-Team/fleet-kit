#!/bin/bash
# path_health_check.sh -- pages when an instance's real Caddy-fronted dashboard path is down.
#
# WHY THIS EXISTS: tunnel_health_check.sh only checks the tunnel's ROOT hostname
# (dino.luckymachines.co/), which falls through Caddy's `handle {}` default to :8000 --
# unrelated to either fleet-kit instance. Since Caddy started routing instances by path
# (/fleet/philanthropy, /fleet/fleet-kit), neither instance's real dashboard URL has ever
# been health-checked or paged on. Confirmed live 2026-08-29: both paths return 200 right
# now, but nothing would notice or page if either broke.
#
# No self-heal (unlike tunnel_health_check.sh): there's no ingress rule to rewrite here,
# just a Caddy path -> localhost:port mapping that's either right or a Caddyfile edit away
# -- out of scope for an unattended cron script to touch. Check + page only.
#
# Usage (cron, one line per instance):
#   */5 * * * * PUBLIC_PATH_URL=https://dino.luckymachines.co/fleet/fleet-kit \
#     NTFY_TOPIC=<topic> STATE_FILE=/path/.paged.state bash scripts/path_health_check.sh
set -uo pipefail

PUBLIC_PATH_URL="${PUBLIC_PATH_URL:?set PUBLIC_PATH_URL -- e.g. https://dino.luckymachines.co/fleet/fleet-kit}"
NTFY_TOPIC="${NTFY_TOPIC:?set NTFY_TOPIC}"
STATE_FILE="${STATE_FILE:?set STATE_FILE -- per-instance, e.g. /home/ubuntu/fleet-kit-server-fleet/logs/.path_health_paged.state}"

already_paged="$(cat "$STATE_FILE" 2>/dev/null || true)"

_ntfy() {
  local title="$1" msg="$2" priority="$3"
  curl -sf -o /dev/null \
    -H "Title: $title" -H "Priority: $priority" -H "Tags: warning" \
    -d "$msg" "https://ntfy.sh/$NTFY_TOPIC" \
    || echo "[path_health_check] WARNING: ntfy POST failed, could not page"
}

http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$PUBLIC_PATH_URL")

if [ "$http_code" = "200" ]; then
  if [ -n "$already_paged" ]; then
    _ntfy "✅ fleet-kit: $PUBLIC_PATH_URL recovered" \
      "Back to HTTP 200 after an outage flagged at $already_paged." \
      "default"
    rm -f "$STATE_FILE"
    echo "[path_health_check] recovered -- cleared page state"
  fi
  echo "[path_health_check] healthy -- $PUBLIC_PATH_URL returned 200"
  exit 0
fi

if [ -z "$already_paged" ]; then
  paged_at="$(date -u '+%Y-%m-%d %H:%M UTC')"
  _ntfy "🚨 fleet-kit: $PUBLIC_PATH_URL down" \
    "Returned HTTP ${http_code:-timeout}. Check Caddyfile routing and the backing container." \
    "urgent"
  echo "$paged_at" > "$STATE_FILE"
  echo "[path_health_check] PAGED -- $PUBLIC_PATH_URL returned ${http_code:-timeout}"
else
  echo "[path_health_check] still down (HTTP ${http_code:-timeout}), already paged at $already_paged"
fi
