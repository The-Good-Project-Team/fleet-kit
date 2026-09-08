#!/usr/bin/env python3
"""prod_health_check.py -- the fleet's FIFTH outage pager, and the first that watches
philanthropy.org from OFF the box it watches (gh#4898).

WHY THIS EXISTS: all 76 monitor/canary/deadman jobs in philanthropy's own
scripts/box/crontab (smoke_box, pg_health, app_error_canary, the deadmen, the search
canaries) run ON atlas-serve, the same box they watch. Box down, cron daemon down, or
disk full silences all 76 at once, and from outside that is indistinguishable from a
calm night. gh#4363 was exactly this: a 38.5h cron-restart loop discovered only by
reading logs after it had already ended. This script runs from dino instead, sharing
no failure domain with atlas-serve.

WHAT IT PROBES (gh#4898 AC3/AC4): three real philanthropy.org pages -- the home/search
page, the site search, and one report page. The report page sits behind a Cloudflare
managed-challenge rule that 403s any non-interactive client (philanthropy repo's own
docs/ops/cloudflare-waf.md, "Known Gap"), so it also carries the documented bypass
header (`x-atlas-test`, same doc's "Test Bypass Header" section) and the required
explicit User-Agent (that doc's "Default urllib User-Agent gets 403'd" section) --
without both, the report probe cannot tell "prod is down" from "Cloudflare is doing its
job", which would make it a false pager, not a true one. EIN 530196605 (American Red
Cross) is that same doc's own worked example of a report page under this rule, reused
here rather than picked fresh.

WHAT IT PAGES ON, INDEPENDENTLY (gh#4898 AC5): philanthropy.org's own
`GET /990/health/canary` (added alongside this script in the philanthropy repo) reports
how long app_error_canary.py has been silent. An app that keeps serving 200s while the
cron watching IT has died (gh#4363's actual failure) would pass every HTTP probe here
and still be an outage -- this is the one check in this script that can catch that.

DEBOUNCE IS NOT REIMPLEMENTED HERE. Every observed condition is reported to
alert_store.py (via fleet_alert.sh's --check/--problem/--severity gate) on every tick;
alert_store's own `degraded` severity already pages only once a condition has persisted
across 2 consecutive runs (see fleet-kit's test_alert_store.py,
test_degraded_pages_only_after_it_persists) and re-pages on its own cadence during a
sustained outage. A second debounce clock in this script could only disagree with that
one, never improve on it.

CONFIG (env vars):
  PROD_HEALTH_BASE_URL             default https://philanthropy.org
  PROD_HEALTH_TIMEOUT_S            default 10
  PHILANTHROPY_CF_TEST_HEADER_VALUE  the Cloudflare bypass header's value -- a secret
                                    provisioned on dino by a human (see philanthropy
                                    repo's docs/ops/monitoring.md), NOT in either repo.
                                    Unset means the report probe will 403 and page --
                                    loud, not silent.
  FLEET_LOG_DIR                    default /home/ubuntu/fleet-kit-logs -- where the
                                    verdict line lands (status_data.py's "Prod
                                    (philanthropy.org)" component reads it from
                                    prod_health_check.cron.log).

Usage (host cron on dino, every 5 minutes):
    */5 * * * * FLEET_LOG_DIR=/home/ubuntu/fleet-kit-logs \\
      PHILANTHROPY_CF_TEST_HEADER_VALUE=<provisioned by a human> \\
      python3 /home/ubuntu/fleet-kit/scripts/prod_health_check.py \\
      >> /home/ubuntu/fleet-kit-logs/prod_health_check.cron.log 2>&1

Not installed by this change -- no build sandbox has host access to dino's crontab.
See philanthropy repo's docs/ops/monitoring.md for what's live vs. blocked.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
CHECK = "prod_external"

BASE_URL = os.environ.get("PROD_HEALTH_BASE_URL", "https://philanthropy.org").rstrip("/")
TIMEOUT_S = float(os.environ.get("PROD_HEALTH_TIMEOUT_S", "10"))

# (name, path). philanthropy repo's docs/ops/cloudflare-waf.md: /990 and /990/?q=... are
# NOT challenged (the search clause is exempted), only /990/report/* is -- but the bypass
# header is sent on all three regardless, since it is a documented no-op on an unchallenged
# path and this keeps the probe list from silently depending on that exemption staying true.
PROBES = [
    ("home", "/990"),
    ("search", "/990/?q=cancer"),
    ("report", "/990/report/530196605/american-national-red-cross-its-constituent-chapters-and-branches"),
]

HEARTBEAT_PATH = "/990/health/canary"

CF_BYPASS_HEADER = "x-atlas-test"
CF_BYPASS_VALUE = os.environ.get("PHILANTHROPY_CF_TEST_HEADER_VALUE", "")


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str


@dataclass
class HeartbeatResult:
    reachable: bool
    stale: bool
    detail: str


@dataclass
class AlertCall:
    problem: str
    severity: str
    title: str
    body: str


def _headers() -> dict:
    # docs/ops/cloudflare-waf.md (philanthropy repo): the default urllib User-Agent gets
    # 403'd on its own, independent of the challenge rule -- every request needs an explicit
    # one regardless of whether the bypass header is set.
    h = {"User-Agent": "atlas-ci/1.0"}
    if CF_BYPASS_VALUE:
        h[CF_BYPASS_HEADER] = CF_BYPASS_VALUE
    return h


def _get(url: str, timeout: float = TIMEOUT_S):
    """One GET. Never raises -- a probe that cannot reach the network IS the failure this
    script exists to report, not a bug in the reporter. Returns (status_code_or_None, body,
    detail_on_failure)."""
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), None
    except urllib.error.HTTPError as e:
        return e.code, b"", None
    except Exception as e:  # noqa: BLE001 -- DNS, timeout, connection refused, TLS, etc.
        return None, b"", f"{type(e).__name__}: {e}"


def run_probes() -> list[ProbeResult]:
    results = []
    for name, path in PROBES:
        code, _body, err = _get(BASE_URL + path)
        if err is not None:
            results.append(ProbeResult(name, False, err))
        elif code == 200:
            results.append(ProbeResult(name, True, "200"))
        else:
            results.append(ProbeResult(name, False, f"http {code}"))
    return results


def check_heartbeat() -> HeartbeatResult:
    code, body, err = _get(BASE_URL + HEARTBEAT_PATH)
    if err is not None or code != 200:
        # Unreachable (not "stale") is deliberately not alerted on its own -- see evaluate()'s
        # docstring for why this and the home probe failing together must not become two
        # alerts naming two different causes.
        return HeartbeatResult(False, False, err or f"http {code}")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return HeartbeatResult(False, False, f"unparseable response: {e}")
    ok = bool(data.get("ok"))
    return HeartbeatResult(True, not ok, f"age={data.get('age_minutes')}min ok={ok}")


def evaluate(probes: list[ProbeResult], heartbeat: HeartbeatResult) -> list[AlertCall]:
    """Pure: given one tick's probe + heartbeat results, return the alerts to report to
    alert_store this tick (gh#4898 AC2/AC4/AC5's attribution requirement). Debounce timing
    is NOT this function's job -- see module docstring.

    A failure of any one probe is reported as ITS OWN alert (AC4: "a failure of any one of
    them is attributable in the alert body to *which* probe failed"), never collapsed into
    one "prod is down" line -- the other probes' results ride along in the body so a reader
    isn't left wondering whether the rest of the site is also affected.
    """
    alerts = []
    for p in probes:
        if p.ok:
            continue
        others = ", ".join(f"{o.name}={'ok' if o.ok else o.detail}" for o in probes if o is not p)
        alerts.append(AlertCall(
            problem=f"http_probe:{p.name}",
            severity="degraded",
            title=f"prod: {p.name} probe failing ({p.detail})",
            body=(f"External check from dino: {BASE_URL}{dict(PROBES)[p.name]} returned "
                  f"{p.detail}. Other probes this tick: {others}. "
                  "See philanthropy repo's docs/ops/monitoring.md for the verify command."),
        ))
    if heartbeat.reachable and heartbeat.stale:
        alerts.append(AlertCall(
            problem="canary_heartbeat_stale",
            severity="degraded",
            title="prod: app_error_canary heartbeat stale",
            body=(f"{BASE_URL}{HEARTBEAT_PATH} reports {heartbeat.detail} -- "
                  "app_error_canary.py has not ticked in 3+ of its 5-minute intervals. "
                  "The app may still be serving 200s (gh#4363: a cron/box outage HTTP "
                  "probes alone would miss)."),
        ))
    return alerts


def verdict_line(alerts: list[AlertCall]) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    if not alerts:
        return f"[prod_health_check {ts} UTC] ok all probes healthy, canary heartbeat fresh"
    return f"[prod_health_check {ts} UTC] FAILED {', '.join(a.problem for a in alerts)}"


def page(problem: str, severity: str, title: str, body: str) -> bool:
    """Fire via fleet_alert.sh -- a local call, no ssh bridge needed: unlike philanthropy's
    own claim_queue_age_alert.py (which bridges FROM atlas-serve), this script already runs
    on the fleet-kit host that owns fleet_alert.sh. Never raises; returns False on failure
    so the caller can surface it without the whole run crashing."""
    argv = ["--check", CHECK, "--problem", problem, "--severity", severity, title, body]
    cmd = ["bash", str(KIT_DIR / "scripts" / "fleet_alert.sh"), *argv]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if p.returncode != 0:
            print(f"prod_health_check: fleet_alert.sh failed rc={p.returncode} "
                  f"{(p.stderr or p.stdout or '')[:300]}", file=sys.stderr)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"prod_health_check: could not page: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def main() -> int:
    probes = run_probes()
    heartbeat = check_heartbeat()
    alerts = evaluate(probes, heartbeat)

    line = verdict_line(alerts)
    log_dir = Path(os.environ.get("FLEET_LOG_DIR", "/home/ubuntu/fleet-kit-logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "prod_health_check.cron.log").open("a") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"prod_health_check: could not write verdict log: {exc}", file=sys.stderr)
    print(line)

    if not alerts:
        return 0

    all_reported = True
    for a in alerts:
        if page(a.problem, a.severity, a.title, a.body):
            print(f"prod_health_check: reported -- {a.problem}")
        else:
            print(f"prod_health_check: REPORT FAILED (see stderr) -- {a.problem}", file=sys.stderr)
            all_reported = False
    return 0 if all_reported else 1


if __name__ == "__main__":
    raise SystemExit(main())
