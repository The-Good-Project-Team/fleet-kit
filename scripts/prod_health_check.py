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

WHAT IT PROBES (gh#727 AC3, gh#4898 AC3/AC4): three real philanthropy.org pages -- the
home/search page, the site search, and one report page, each against an 8s latency
budget (gh#727 AC3): a 200 that takes longer is recorded as a `slow` failure, distinct
from a non-200 status, so a reader isn't left assuming a timeout was a normal down. The
report page sits behind a Cloudflare managed-challenge rule that 403s any
non-interactive client (philanthropy repo's own docs/ops/cloudflare-waf.md, "Known
Gap"), so it also carries the documented bypass header (`x-atlas-test`, same doc's "Test
Bypass Header" section) and the required explicit User-Agent (that doc's "Default
urllib User-Agent gets 403'd" section) -- without both, the report probe cannot tell
"prod is down" from "Cloudflare is doing its job", which would make it a false pager,
not a true one. EIN 530196605 (American Red Cross) is that same doc's own worked
example of a report page under this rule, reused here rather than picked fresh.

WHEN THE BYPASS TOKEN IS MISSING (gh#727 AC9): the report probe is SKIPPED, not failed.
A missing secret cannot be told apart from a real Cloudflare challenge from the HTTP
response alone, and paging on that ambiguity would be a false page -- worse here than a
missed one, since a human who gets paged for a misconfigured secret learns to ignore
this pager. The home/search probes are unaffected (docs/ops/cloudflare-waf.md: they are
not challenged) and still run either way.

WHAT IT PAGES ON, INDEPENDENTLY (gh#4898 AC5): philanthropy.org's own
`GET /990/health/canary` (added alongside this script in the philanthropy repo) reports
how long app_error_canary.py has been silent. An app that keeps serving 200s while the
cron watching IT has died (gh#4363's actual failure) would pass every HTTP probe here
and still be an outage -- this is the one check in this script that can catch that.

DEBOUNCE IS NOT REIMPLEMENTED HERE. Every observed condition is recorded to
alert_store.py's `record()` directly (not through fleet_alert.sh's own severity gate --
this script needs the same page/no-page verdict alert_store already computes, to decide
whether to ALSO file an incident issue this tick, so it asks once and reuses the answer
rather than letting fleet_alert.sh ask a second time). alert_store's own `degraded`
severity pages only once a condition has persisted across 2 consecutive runs (see
fleet-kit's test_alert_store.py, test_degraded_pages_only_after_it_persists) -- and then
STAYS silent for that same (check, problem) key forever, `record()` has no re-paging
cadence of its own. That is why every tick also calls `alert_store.resolve_check()` with
the set of problem keys still open THIS tick (gh#727): anything not in that set gets
marked resolved, so if it recurs later it is treated as a fresh occurrence and can page
again. Skipping that call would silently turn this into a fire-once pager.

FILING TO THE PRODUCT BOARD (gh#727 AC2/AC4/AC5/AC6): the same tick that pages also
files or updates ONE incident issue on The-Good-Project-Team/philanthropy, titled
`prod down: <first failing url>` and labelled `fleet:backlog, lane:devops,
fleet:priority-high, incident`. Dedup is by open title -- a further failure while that
issue is still open comments on it rather than filing a second one. Recovery comments
on the same issue but never closes it (closing is a human decision).

CONFIG (env vars):
  PROD_HEALTH_BASE_URL             default https://philanthropy.org
  PROD_HEALTH_TIMEOUT_S            default 10
  PHILANTHROPY_CF_TEST_HEADER_VALUE  the Cloudflare bypass header's value -- a secret
                                    provisioned on dino by a human (see philanthropy
                                    repo's docs/ops/monitoring.md), NOT in either repo.
                                    Unset means the report probe is SKIPPED (gh#727 AC9),
                                    not treated as a site outage.
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
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alert_store  # noqa: E402
import prod_incident  # noqa: E402 -- shared with fixer_fire_path.py (gh#728 VP fix 3): one
                       # marker, one filing helper, one explicit repo for the "a probe is
                       # actually down" incident class both members can independently observe.

KIT_DIR = Path(__file__).resolve().parent.parent
CHECK = "prod_external"

BASE_URL = os.environ.get("PROD_HEALTH_BASE_URL", "https://philanthropy.org").rstrip("/")
TIMEOUT_S = float(os.environ.get("PROD_HEALTH_TIMEOUT_S", "10"))

# gh#727 AC3: a 200 that takes longer than this is a failure in its own right, distinct
# from a non-200 status -- not configurable, the PRD names this number specifically.
SLOW_BUDGET_S = 8.0

# gh#727 AC4/AC6: where and how the incident issue is filed/deduped/labelled. Same repo
# fixer_fire_path.py pins (prod_incident.INCIDENT_REPO) -- one source of truth (gh#728 VP fix 3).
INCIDENT_REPO = prod_incident.INCIDENT_REPO
INCIDENT_TITLE_PREFIX = "prod down: "
INCIDENT_LABELS = ["fleet:backlog", "lane:devops", "fleet:priority-high", "incident"]

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
    skipped: bool = False


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
    reason: str = ""


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
    detail_on_failure, elapsed_seconds)."""
    req = urllib.request.Request(url, headers=_headers())
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), None, time.monotonic() - start
    except urllib.error.HTTPError as e:
        return e.code, b"", None, time.monotonic() - start
    except Exception as e:  # noqa: BLE001 -- DNS, timeout, connection refused, TLS, etc.
        return None, b"", f"{type(e).__name__}: {e}", time.monotonic() - start


def run_probes() -> list[ProbeResult]:
    results = []
    for name, path in PROBES:
        if name == "report" and not CF_BYPASS_VALUE:
            # gh#727 AC9: without the bypass token this probe cannot tell "prod is down"
            # from "Cloudflare is doing its job" -- skip rather than page on that guess.
            results.append(ProbeResult(name, True, "SKIPPED (no bypass token)", skipped=True))
            continue
        code, _body, err, elapsed = _get(BASE_URL + path)
        if err is not None:
            results.append(ProbeResult(name, False, err))
        elif code != 200:
            results.append(ProbeResult(name, False, f"http {code}"))
        elif elapsed > SLOW_BUDGET_S:
            # gh#727 AC3: distinguishable from a non-200 status, both in this reason string
            # and in evaluate()'s alert title/body.
            results.append(ProbeResult(name, False, f"slow ({elapsed:.1f}s > {SLOW_BUDGET_S}s budget)"))
        else:
            results.append(ProbeResult(name, True, "200"))
    return results


def check_heartbeat() -> HeartbeatResult:
    code, body, err, _elapsed = _get(BASE_URL + HEARTBEAT_PATH)
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
            reason=p.detail,
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
            reason=heartbeat.detail,
        ))
    return alerts


def verdict_line(alerts: list[AlertCall], probes: list[ProbeResult] | None = None) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    skipped = [p.name for p in (probes or []) if p.skipped]
    skip_note = f" (skipped: {', '.join(skipped)})" if skipped else ""
    if not alerts:
        return f"[prod_health_check {ts} UTC] ok all probes healthy, canary heartbeat fresh{skip_note}"
    parts = ", ".join(f"{a.problem} ({a.reason})" if a.reason else a.problem for a in alerts)
    return f"[prod_health_check {ts} UTC] FAILED {parts}{skip_note}"


def page(problem: str, severity: str, title: str, body: str) -> bool | None:
    """Ask alert_store's own debounce whether THIS condition should page now, then deliver
    through fleet_alert.sh if so. Decided here (not inside fleet_alert.sh's own
    --check/--severity gate) because the caller needs the same page/no-page verdict to
    decide whether to ALSO file an incident issue this tick (gh#727 AC2) -- asking twice
    could disagree with itself, and would double-count this observation in alert_store.

    Returns True if paged this tick, False if correctly suppressed (too young, or already
    paged for this open condition), None if delivery itself failed."""
    try:
        verdict = alert_store.record(CHECK, problem, severity, detail=body)
    except Exception as exc:  # noqa: BLE001 -- alert_store fails open, but stay defensive
        verdict = {"page": True, "reason": f"alert_store.record raised: {exc}"}

    if not verdict.get("page"):
        print(f"prod_health_check: suppressed ({verdict.get('reason')}) -- {problem}")
        return False

    cmd = ["bash", str(KIT_DIR / "scripts" / "fleet_alert.sh"), title, body]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if p.returncode != 0:
            print(f"prod_health_check: fleet_alert.sh failed rc={p.returncode} "
                  f"{(p.stderr or p.stdout or '')[:300]}", file=sys.stderr)
            return None
        print(f"prod_health_check: reported -- {problem}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"prod_health_check: could not page: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _run_gh(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def first_failing_url(probes: list[ProbeResult]) -> str | None:
    """gh#727 AC4: the incident title is keyed on the FIRST failing probe, in PROBES order,
    not whichever one evaluate() happens to list first."""
    by_name = {p.name: p for p in probes}
    for name, path in PROBES:
        p = by_name.get(name)
        if p is not None and not p.ok:
            return BASE_URL + path
    return None


def incident_title(url: str) -> str:
    return f"{INCIDENT_TITLE_PREFIX}{url}"


def _list_open_incidents(runner=_run_gh) -> list[dict]:
    code, out = runner(["gh", "issue", "list", "--repo", INCIDENT_REPO, "--label", "incident",
                         "--state", "open", "--json", "number,title", "--limit", "50"])
    if code != 0:
        print(f"prod_health_check: could not list incidents: {out[:300]}", file=sys.stderr)
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


def find_open_incident(title: str, runner=_run_gh) -> int | None:
    for issue in _list_open_incidents(runner):
        if issue.get("title") == title:
            return issue.get("number")
    return None


def find_all_open_incidents(runner=_run_gh) -> list[int]:
    return [issue["number"] for issue in _list_open_incidents(runner)
            if (issue.get("title") or "").startswith(INCIDENT_TITLE_PREFIX)]


def build_file_cmd(title: str, body: str) -> list[str]:
    # Deliberately NOT prod_incident.build_create_cmd: this builder is used only for the
    # heartbeat-stale ticket (see file_or_update_incident below), which must NEVER carry the
    # shared INCIDENT_MARKER -- a probe-down event's shared-marker search would otherwise find
    # and silently comment onto an unrelated heartbeat ticket instead of filing its own.
    cmd = ["gh", "issue", "create", "--repo", INCIDENT_REPO, "--title", title, "--body", body]
    for label in INCIDENT_LABELS:
        cmd += ["--label", label]
    return cmd


def build_comment_cmd(number: int, body: str) -> list[str]:
    return ["gh", "issue", "comment", str(number), "--repo", INCIDENT_REPO, "--body", body]


def file_or_update_incident(probes: list[ProbeResult], alerts: list[AlertCall],
                             runner=_run_gh) -> dict:
    """Called only on the tick where page() actually paged (gh#727 AC2's debounce). Files
    one incident issue on the product board, or comments the existing open one -- never a
    second issue for the same open incident (AC4).

    A probe that is ACTUALLY unreachable (`url` below is not None) is the same class of
    "site unreachable" event the-fixer's own rollback fire path (gh#728, fixer_fire_path.py)
    can independently detect and file for -- that branch goes through prod_incident's SHARED
    marker + helper, the same one fixer_fire_path.py uses, so whichever member notices first
    keeps the only open ticket instead of both filing one (gh#728 VP fix 3: "a single outage
    could file two open incident issues"). A stale heartbeat with every HTTP probe healthy is
    a signal only this script watches (gh#4363) -- it keeps its own separate title-based
    ticket, unmarked, so it can never be silently absorbed into an unrelated rollback ticket.
    """
    detail = "\n".join(f"- {a.title}: {a.body}" for a in alerts)
    body = (f"Detected by `prod_health_check.py` running on dino, outside atlas-serve "
            f"(gh#727).\n\n{detail}")

    url = first_failing_url(probes)
    if url is not None:
        title = incident_title(url)
        number, created = prod_incident.file_or_update_incident(
            INCIDENT_REPO, title, body, INCIDENT_LABELS, run=runner)
        return {"action": "filed" if created else "commented", "issue": number,
                "ok": number is not None}

    title = incident_title(BASE_URL + HEARTBEAT_PATH)
    existing = find_open_incident(title, runner)
    if existing is not None:
        code, out = runner(build_comment_cmd(existing, f"Still failing:\n\n{detail}"))
        return {"action": "commented", "issue": existing, "ok": code == 0}
    code, out = runner(build_file_cmd(title, body))
    m = re.search(r"/issues/(\d+)\s*$", out.strip())
    return {"action": "filed", "issue": int(m.group(1)) if m else None, "ok": code == 0}


def report_recovery(runner=_run_gh) -> list[dict]:
    """gh#727 AC5: comment recovery on every still-open incident issue -- never close one.
    Commenting on ALL of them (not just the first match) matters because a probe incident
    and a later, separate heartbeat incident can both be open at once; leaving either one
    without a recovery note would misleadingly look still-active to a human."""
    results = []
    for number in find_all_open_incidents(runner):
        code, _out = runner(build_comment_cmd(
            number,
            "Recovered: all probes healthy and the canary heartbeat is fresh on this tick "
            "(prod_health_check.py, from dino). Leaving open for a human to confirm and close.",
        ))
        results.append({"issue": number, "ok": code == 0})
    return results


def main() -> int:
    probes = run_probes()
    heartbeat = check_heartbeat()
    alerts = evaluate(probes, heartbeat)

    line = verdict_line(alerts, probes)
    log_dir = Path(os.environ.get("FLEET_LOG_DIR", "/home/ubuntu/fleet-kit-logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "prod_health_check.cron.log").open("a") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"prod_health_check: could not write verdict log: {exc}", file=sys.stderr)
    print(line)

    # alert_store.record()'s own per-key debounce pages a condition ONCE and then stays
    # "already paged" forever for that key -- it has no re-paging cadence of its own (despite
    # this module's earlier docstring claim; verified against alert_store.py directly). This
    # check must therefore declare, every tick, which problem keys are STILL open (`keep`) so
    # alert_store can resolve everything else -- without this, a condition that pages, clears,
    # and recurs later would never page again.
    still_open = {a.problem for a in alerts}
    resolved = alert_store.resolve_check(CHECK, keep=still_open)

    if not alerts:
        if any(r["was_paged"] for r in resolved):
            for rec in report_recovery():
                print(f"prod_health_check: recovery comment on #{rec['issue']} ok={rec['ok']}")
        return 0

    all_reported = True
    any_paged = False
    for a in alerts:
        paged = page(a.problem, a.severity, a.title, a.body)
        if paged is None:
            all_reported = False
        elif paged:
            any_paged = True

    if any_paged:
        result = file_or_update_incident(probes, alerts)
        print(f"prod_health_check: incident {result['action']} "
              f"#{result.get('issue')} ok={result['ok']}")
        if not result["ok"]:
            all_reported = False

    return 0 if all_reported else 1


if __name__ == "__main__":
    raise SystemExit(main())
