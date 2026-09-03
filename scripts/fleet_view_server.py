#!/usr/bin/env python3
"""fleet_view_server — watch the fleet work, live, from one page. No DB, no framework.

WHY THIS SHAPE. The source product's fleet dashboard was ~2,700 lines across five files: its
own Postgres tables, its own API layer, its own auth-gated routes riding on the app it existed
to watch (superadmin.philanthropy.org/fleet). That's the anti-pattern this kit exists to avoid
repeating: fleet observability became a second product, coupled to the first one's DB and
deploy pipeline, and it went dark for 20 days once precisely because nobody noticed a page
nobody could reach without shipping the main app first.

This is the opposite bet: everything this page shows already exists as a plain file
(`runs.jsonl`, written by run_report.py -- see that module's header) or a `gh` CLI call (PRs,
issues). The server's only job is to tail one file and poll `gh` on an interval, then push both
over Server-Sent Events to a single static page. Kill the process, the fleet keeps running
untouched -- this is a WINDOW, not a component the loop depends on.

Portable to any project: nothing here reads a product-specific schema. `runs.jsonl` is this
kit's own report contract (member/run_id/kind/status/outcome/evidence/tokens/item_id/pr) and
`gh pr/issue list --json` are GitHub's own stable shape.

Run: `python3 scripts/fleet_view_server.py` (reads FLEET_REPO, FLEET_LOG_DIR from fleet.env like
every other script here). Serves on FLEET_VIEW_PORT (default 8420). Steering actions POST back
to this same process and shell out to overrides.py / gh -- no new authority, just a button on
top of commands you could already type.
"""
from __future__ import annotations

import datetime
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KIT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_DIR / "scripts"))
import fleet_db          # noqa: E402  (sqlite mirror -- search/spend queries over runs.jsonl)
import fleet_kpi         # noqa: E402  (per-member headline-count extraction from outcome prose)
import fleet_stats       # noqa: E402  (Stats page aggregation: run timeline, tokens, backlog history)
import member_spec       # noqa: E402
import overrides as ov   # noqa: E402  ('overrides' shadows nothing here; keep the module name clear)

REPO = os.environ.get("FLEET_REPO", "")
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()


def _resolve_repo_url() -> str:
    """https://github.com/<owner>/<repo> for this fleet's target repo, so the dashboard can
    link a bare PR/issue number straight to GitHub -- resolved ONCE at boot (a repo's remote
    doesn't change while this process runs) rather than shelling out on every request. Works
    whether origin is an https or git@ remote; empty string (link renders as plain text, not
    a broken href) if there's no repo, no remote, or git isn't on PATH.
    """
    try:
        p = subprocess.run(["git", "remote", "get-url", "origin"], cwd=REPO or None,
                            capture_output=True, text=True, timeout=5)
        url = p.stdout.strip()
        if not url:
            return ""
        if url.startswith("git@github.com:"):
            url = "https://github.com/" + url[len("git@github.com:"):]
        return url[:-4] if url.endswith(".git") else url
    except (subprocess.TimeoutExpired, OSError):
        return ""


REPO_URL = _resolve_repo_url()


def _resolve_siblings() -> list[dict]:
    """Other fleet-kit instances this one's Settings page can link out to -- e.g. two
    instances sharing a box, each its own container/port. Optional: FLEET_SIBLINGS is
    "name=url,name=url" in fleet.env; absent or empty means single-instance (the common
    case for a template deployment) and the dropdown simply doesn't render. This process
    never talks to a sibling -- it's a plain link, same "no new authority" spirit as every
    other button on this page."""
    raw = os.environ.get("FLEET_SIBLINGS", "")
    out = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, url = pair.partition("=")
        name, url = name.strip(), url.strip()
        if name and url:
            out.append({"name": name, "url": url})
    return out


SIBLINGS = _resolve_siblings()
RUNS_FILE = LOG_DIR / "runs.jsonl"
PORT = int(os.environ.get("FLEET_VIEW_PORT", "8420"))
GH_POLL_S = int(os.environ.get("FLEET_VIEW_GH_POLL_S", "20"))
MAX_RUNS = 500  # bound memory; this is a window, not an archive -- runs.jsonl on disk is the archive

# The ONE file the master switch and every per-member switch live in -- same file a human
# would hand-edit, same file every cron script sources. This server's toggle button and a
# human's text editor are the same mechanism, never two that can disagree.
ENV_FILE = Path(os.environ.get("FLEET_ENV_FILE", KIT_DIR / "fleet.env"))

def _load_members() -> dict:
    """Build the toggle/run-now table from the real members/*.fleet.json files instead of a
    hand-maintained dict -- that dict drifted to 2 of 8 real members (builder/judge-judy only)
    the moment run_member.sh + the other 6 charters landed 2026-08-21, silently 400ing every
    fleet_toggle/run_now call for gru/marie/dumbledore/roomba/the-fixer/jefe/messenger. Every
    member except judge-judy (a custom non-agentic runner, see its own .fleet.json) runs via
    the one generic run_member.sh <name> entry point.
    """
    out = {}
    try:
        specs = member_spec.load_all()
    except Exception:
        return out
    for spec in specs:
        name = spec["name"]
        script = (f"../members/{name}/{name}.sh" if name == "judge-judy"
                  else "run_member.sh")
        out[name] = {
            "script": script,
            "args": [] if name == "judge-judy" else [name],
            "schedule": spec.get("schedule", {}),
        }
    return out


MEMBERS = _load_members()


def next_fires() -> list[dict]:
    """When does each enabled member fire next -- pure math off each spec's own schedule (one
    of interval_s / hourly_at_minute / daily_at, see member_spec.py's validation), no cron
    daemon queried, because there isn't one to query: entrypoint.sh's crontab lines and these
    schedule fields are meant to agree by construction, not by a second source of truth. A
    disabled member (or one with an override applied to disable it) shows here too, marked
    inactive, so a click-off is visibly reflected rather than just vanishing from the list.
    """
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    out = []
    try:
        specs = member_spec.load_all()
    except Exception:
        return out
    for spec in specs:
        eff, _ = ov.apply(spec)
        sched = eff.get("schedule", {})
        enabled = bool(eff.get("enabled"))
        next_at = None
        if "interval_s" in sched:
            secs = int(sched["interval_s"])
            # next boundary of a fixed-interval tick since epoch -- matches cron's own "every
            # N minutes/seconds" semantics (aligned to :00, not to whenever this request runs)
            epoch = int(now.timestamp())
            next_at = now + _dt.timedelta(seconds=(secs - epoch % secs))
        elif "hourly_at_minute" in sched:
            minute = int(sched["hourly_at_minute"])
            candidate = now.replace(minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += _dt.timedelta(hours=1)
            next_at = candidate
        elif "daily_at" in sched:
            hh, mm = (int(x) for x in str(sched["daily_at"]).split(":"))
            candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if candidate <= now:
                candidate += _dt.timedelta(days=1)
            next_at = candidate
        out.append({
            "member": spec["name"],
            "enabled": enabled,
            "schedule": sched,
            "next_at": next_at.isoformat() if next_at else None,
            "in_s": int((next_at - now).total_seconds()) if next_at else None,
        })
    out.sort(key=lambda m: (m["in_s"] is None, m["in_s"]))
    return out


def _gh(*args: str, timeout: int = 15) -> str:
    try:
        p = subprocess.run(["gh", *args], cwd=REPO or None, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


_TTL_CACHE: dict[str, tuple[float, object]] = {}
_TTL_LOCK = threading.Lock()


def _cached(key: str, ttl_s: float, produce):
    """Memoize an expensive request-time computation for ttl_s seconds.

    For endpoints that make their OWN blocking gh calls rather than reading STATE's polled
    snapshot. backlog_history was measured at 8.4s per hit (two gh calls, 1000 issues + 500
    PRs) and re-paid it on every single Stats page load, every reload, for every viewer --
    while plotting DAY-granularity buckets that cannot meaningfully change between two loads
    a minute apart.

    produce() returns (value, cacheable). Returning cacheable=False serves the value for this
    one request without storing it -- for when the underlying fetch failed in a way that still
    produces a structurally valid but WRONG answer. _gh swallows every failure into "", which
    the day-bucket aggregators happily turn into a full run of zeroes; caching that would pin a
    false flatline over the real trend for the whole TTL. A stale-but-true chart is fine, a
    confidently-wrong one is not.

    The lock is held across produce() on purpose: this server is a ThreadingHTTPServer, so
    two concurrent loads would otherwise both miss and fire duplicate 8s gh calls. Holding it
    means the second waits on the first's result instead (a "thundering herd" / cache
    stampede -- the standard fix is exactly this single-flight lock). Serializing distinct
    keys is acceptable here: this cache fronts a handful of endpoints on a dashboard with a
    handful of viewers, and correctness beats the parallelism we give up.

    Stale entries are never evicted on a timer -- the key set is fixed and tiny (one per
    endpoint+params combination), so the dict cannot grow without bound.
    """
    now = time.time()
    with _TTL_LOCK:
        hit = _TTL_CACHE.get(key)
        if hit is not None and now - hit[0] < ttl_s:
            return hit[1]
        value, cacheable = produce()
        if cacheable:
            _TTL_CACHE[key] = (now, value)
        return value


# Settings page dial fields -- non-secret tuning knobs a human may want to see/edit from the
# browser instead of ssh+vim. Allow-listed the same way dino-dashboard.py's READABLE_FIELDS
# is: this is the ONLY set of keys /api/fleet_settings may write. Never widen to "any key".
DIAL_FIELDS = [
    "FLEET_SHARE_FRACTION", "FLEET_GRU_ALLOWANCE_FRACTION", "FLEET_GRU_CADENCE",
    "FLEET_CADENCE_BUILD", "FLEET_CADENCE_REVIEW", "FLEET_CADENCE_GITPULL",
    "FLEET_BUILDER_MODEL", "FLEET_CODE_REVIEW_MODEL",
    "FLEET_QUEUE_CAP", "FLEET_MAX_BUDGET_USD",
]


def read_env_flags() -> dict:
    """FLEET_ENABLED from fleet.env text (not this process's environment, which was only a
    snapshot taken at start -- a toggle must be visible on the very next page load, not after
    a restart) + every member's REAL enabled state, which lives in its own spec.enabled field
    (post-overrides), not an env var -- see fleet_toggle's own comment for why there is no
    such env var for a run_member.sh member. Also carries the DIAL_FIELDS tuning values (raw
    strings, blank if unset) and SIBLINGS for the Settings page -- same file, same request,
    one round trip."""
    values = read_env_values()
    out = {"FLEET_ENABLED": values.get("FLEET_ENABLED", "true") == "true", "REPO_URL": REPO_URL,
           "SIBLINGS": SIBLINGS}
    for key in DIAL_FIELDS:
        out[key] = values.get(key, "")
    try:
        for spec in member_spec.load_all():
            eff, _ = ov.apply(spec)
            out[spec["name"]] = bool(eff.get("enabled"))
    except Exception:
        pass
    return out


def read_env_values() -> dict:
    """Every KEY=value in fleet.env, as text. The file -- not this process's environment --
    is authoritative for anything an operator can edit at runtime: the container is handed
    FLEET_ENV_FILE (a path) but never the file's values, so a key added to fleet.env after the
    server started is invisible to os.environ forever. read_env_state has always read the file
    for exactly this reason; the auth path did not, which made FLEET_API_KEY unreadable and
    every login a fail-closed 503 on an instance whose fleet.env held a perfectly good key.
    """
    text = ENV_FILE.read_text(errors="ignore") if ENV_FILE.exists() else ""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        values[k.strip()] = v.strip()
    return values


def api_key() -> str:
    """The configured FLEET_API_KEY, file first, process env as fallback. One accessor so the
    gate (_authorized) and the cookie mint (_handle_login) can never disagree about whether a
    key exists -- disagreement there means login succeeds and every write still 401s."""
    return (read_env_values().get("FLEET_API_KEY") or os.environ.get("FLEET_API_KEY") or "").strip()


def subprocess_env() -> dict:
    """os.environ overlaid with fleet.env's values, for any child process this server spawns.

    Same root cause as FLEET_API_KEY in #295, one layer out: the container is handed
    FLEET_ENV_FILE (a PATH) and never the file's VALUES, so this long-lived process has no
    FLEET_MAXX_URL/_HANDLE/_KEY in os.environ no matter what fleet.env holds. Cron jobs
    re-source fleet.env per run and were fine; a child inheriting THIS process's environment
    is not. maxx_share_ceiling.py therefore read an unconfigured meter, printed its
    fail-open empty string, and the Settings page told the operator "maxx meter unreadable
    right now" while the meter was healthy -- measured live 2026-09-02: the same script with
    fleet.env sourced returns label="ok".

    File values win over os.environ: fleet.env is what an operator edits at runtime, and a
    stale snapshot taken at process start must never shadow it.
    """
    env = dict(os.environ)
    env.update({k: v for k, v in read_env_values().items() if v})
    return env


def write_env_flag(key: str, value: bool) -> None:
    """Set KEY=true|false in fleet.env, preserving every other line. Appends the key if it
    isn't present yet (a fresh fleet.env copied from fleet.env.example already has it, but
    don't assume)."""
    write_env_field(key, "true" if value else "false")


def write_env_field(key: str, value: str) -> None:
    """Set KEY=value (any string) in fleet.env, preserving every other line. Appends the key
    if it isn't present yet. write_env_flag's bool-only twin, factored out so DIAL_FIELDS
    (strings/numbers) and the true/false flags share one file-rewrite path."""
    text = ENV_FILE.read_text(errors="ignore") if ENV_FILE.exists() else ""
    lines = text.splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


def read_pass_block(member: str, n: int) -> list[str]:
    """Return the Nth-most-recent 'pass start' ... 'pass end' block from <member>.log, n=0 is
    the latest. A judge-judy-style pass with no explicit 'pass start' line (see that member's
    own log shape) has no block boundary to find -- returns [] and the frontend falls back to
    the run record's own outcome/evidence fields, which is all there ever was for that member.
    Bounded read: this kit's whole reason to exist is not shipping a second product to watch
    the first one, so this stays a plain file scan, no index, no DB.
    """
    log_path = LOG_DIR / f"{member}.log"
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(errors="ignore").splitlines()
    except OSError:
        return []
    starts = [i for i, ln in enumerate(lines) if "pass start" in ln]
    if not starts:
        return []
    starts.sort()
    if n >= len(starts):
        return []
    start_i = starts[-(n + 1)]
    # end = next pass start after this one, or the end of the file
    end_i = starts[-n] if n > 0 else len(lines)
    return lines[start_i:end_i]


# Self-evolution means jefe or dumbledore -- the fleet's own two self-correcting personas --
# decided to change the fleet's rules, not just any worker (minion/roomba/etc) touching an
# agent-definition path as incidental work. GitHub's PR `author` is USELESS for this: every
# merged PR shows the human account (goodindustries) because that account's `gh` credentials
# do the actual merge for every persona -- confirmed live 2026-08-25 (PR #3176, a plain
# human/session PR on branch docs/hack-solo-mode, author reads identically to a real jefe PR).
# The real signal is the BRANCH NAME: jefe's own worktree/PR flow names its branch `jefe/...`
# (confirmed live: PR #3150, branch `jefe/fix-3108-msh-squash`) and dumbledore's the same way
# (confirmed live: PR #3032, branch `dumbledore/memory-20260820h`) -- vs. a human-authored
# branch (generic docs/, devops/, feat/ prefixes) or a WORKER's own branch (`member/<name>-...`
# for roomba, minion, etc -- NOT jefe/dumbledore, and not what this panel is about per Reif's
# correction, 2026-08-25: "jefe was the source of the PR", not "any of the 9 members touched an
# agent file"). jefe is reactive (priority ladder, backlog); dumbledore's whole charter IS
# "fix the instruction/charter/gate that caused the symptom, not the instance" -- the only two
# personas whose job is deciding the fleet's OWN rules should change, not doing the work itself.
# Filtered server-side via gh's `head:` search qualifier (see poll_gh_state below) rather than
# pulling N generic merged PRs and filtering client-side -- jefe/dumbledore PRs are sparse (2
# and 5 of the last 100 merged, measured live) so a client-filtered recent-N window would often
# show this panel empty even when real self-evolution happened.


def poll_gh_state() -> dict:
    prs_raw = _gh("pr", "list", "--state", "open", "--json",
                   "number,title,isDraft,headRefName,url,statusCheckRollup,updatedAt")
    issues_raw = _gh("issue", "list", "--state", "open", "--label", "fleet:backlog", "--json",
                      "number,title,labels,updatedAt", "--limit", "100")
    # Recently merged: plain feed, whatever's most recent -- what just shipped, any branch.
    merged_raw = _gh("pr", "list", "--state", "merged", "--json",
                      "number,title,mergedAt,url,author,files,headRefName", "--limit", "30")
    # Self-evolution: server-side head: search per persona (see the "Self-evolution means
    # jefe or dumbledore" comment above for why) rather than filtering a recent-N window
    # client-side. Two searches per persona: their own `<name>/...` branch convention AND the
    # generic per-item dispatch shape (`member/<name>-<itemid>-<ts>`) that minion/roomba/the-fixer
    # also use -- #237, confirmed live miss: PR #214 (dumbledore, `member/dumbledore-186507-...`)
    # was silently absent from this panel because only `head:dumbledore/` was searched. A third
    # shape -- an owner-prefix-less hyphenated slug (e.g. #181, `jefe-judgejudy-fairness`) -- is
    # NOT caught here; that needs content-based inference, not a branch-name search qualifier,
    # and is an explicit, known residual gap (#237's PRD non-goal).
    self_evolution_fields = "number,title,mergedAt,url,author,files,headRefName"
    jefe_raw = _gh("pr", "list", "--state", "merged", "--search", "head:jefe/", "--json",
                    self_evolution_fields, "--limit", "20")
    dumbledore_raw = _gh("pr", "list", "--state", "merged", "--search", "head:dumbledore/",
                          "--json", self_evolution_fields, "--limit", "20")
    jefe_member_raw = _gh("pr", "list", "--state", "merged", "--search", "head:member/jefe-",
                           "--json", self_evolution_fields, "--limit", "20")
    dumbledore_member_raw = _gh("pr", "list", "--state", "merged", "--search",
                                 "head:member/dumbledore-", "--json", self_evolution_fields,
                                 "--limit", "20")
    try:
        prs = json.loads(prs_raw) if prs_raw else []
    except json.JSONDecodeError:
        prs = []
    try:
        issues = json.loads(issues_raw) if issues_raw else []
    except json.JSONDecodeError:
        issues = []
    try:
        merged = json.loads(merged_raw) if merged_raw else []
    except json.JSONDecodeError:
        merged = []
    try:
        self_evolution_raw = (
            (json.loads(jefe_raw) if jefe_raw else []) +
            (json.loads(dumbledore_raw) if dumbledore_raw else []) +
            (json.loads(jefe_member_raw) if jefe_member_raw else []) +
            (json.loads(dumbledore_member_raw) if dumbledore_member_raw else [])
        )
    except json.JSONDecodeError:
        self_evolution_raw = []
    # A PR could match more than one of the four searches (unlikely given the branch-name
    # prefixes are disjoint, but not impossible if a title/description also matched somehow) --
    # dedup by PR number so it doesn't double-count in the panel.
    self_evolution_by_number = {}
    for pr in self_evolution_raw:
        self_evolution_by_number[pr["number"]] = pr
    self_evolution = list(self_evolution_by_number.values())
    for pr in prs:
        checks = pr.get("statusCheckRollup") or []
        states = {c.get("state") or c.get("conclusion") for c in checks}
        pr["_rollup"] = ("failing" if states & {"FAILURE", "ERROR", "failure"} else
                          "pending" if states & {"PENDING", "IN_PROGRESS", None} else
                          "green" if checks else "none")
    for issue in issues:
        names = {lb.get("name") for lb in issue.get("labels") or []}
        issue["_claimed"] = any(n and n.endswith(":claimed") for n in names)
    merged.sort(key=lambda pr: pr.get("mergedAt") or "", reverse=True)
    self_evolution.sort(key=lambda pr: pr.get("mergedAt") or "", reverse=True)
    return {"prs": prs, "issues": issues, "merged": merged,
            "self_evolution": self_evolution, "polled_at": time.time()}



def _session_token(key: str) -> str:
    """The cookie value that proves possession of FLEET_API_KEY.

    A DERIVED value, never the key itself: the cookie is sent on every same-origin request
    and lands in browser storage, so putting the real key there would spread it far wider
    than the one Authorization-style header it replaces. HMAC over a fixed label means the
    token is stable across restarts (an operator is not logged out by a redeploy) while
    still being useless for deriving the key back out.
    """
    return hmac.new(key.encode(), b"fleet-view-session-v1", "sha256").hexdigest()



def _budget_preview() -> dict:
    """Live derivation of gru's hourly allowance, for display on the Settings page.

    Returns every intermediate value, not just the answer, so an operator can SEE which dial
    moved what -- and so a nonsense result (empty ceiling, zero headroom, a fraction that
    changes nothing) is visible instead of silently swallowed. Never raises: this is a
    read-only display route and a broken meter must degrade to an explanation, not a 500.
    """
    flags = read_env_flags()
    share = (flags.get("FLEET_SHARE_FRACTION") or "").strip()
    gru_frac = (flags.get("FLEET_GRU_ALLOWANCE_FRACTION") or "").strip()

    out: dict = {
        "share_fraction": share or None,
        "gru_fraction": gru_frac or None,
        "ceiling_pct": None,
        "gru_allowance_pct": None,
        "others_pct": None,
        "formula": "instance_ceiling = account_hourly_headroom x share_fraction ; "
                   "gru_allowance = instance_ceiling x gru_fraction",
        "note": None,
    }
    if not share or share == "1.0":
        out["note"] = ("FLEET_SHARE_FRACTION is unset or 1.0, so no ceiling is exported and "
                       "gru falls back to its own default -- set it below to cap this instance.")
        return out
    try:
        proc = subprocess.run(
            [sys.executable, str(KIT_DIR / "scripts" / "maxx_share_ceiling.py"), share],
            capture_output=True, text=True, timeout=20, env=subprocess_env())
        ceiling = (proc.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001 -- display route, never 500 on a meter hiccup
        out["note"] = f"could not read the maxx meter: {exc}"
        return out
    if not ceiling:
        # Distinguish the two causes that both print an empty ceiling. "Unreadable" was
        # reported for months when the real answer was "this server cannot see the maxx
        # credentials", which is an operator-fixable configuration fault, not a meter outage.
        missing = [k for k in ("FLEET_MAXX_URL", "FLEET_MAXX_HANDLE", "FLEET_MAXX_KEY")
                   if not (subprocess_env().get(k) or "").strip()]
        if missing:
            out["note"] = ("maxx is not configured for this instance -- " + ", ".join(missing)
                           + " missing from fleet.env. gru fails OPEN to its own conservative "
                             "default; nothing is over-spent.")
        else:
            out["note"] = ("maxx meter unreadable right now -- no ceiling. gru fails OPEN to "
                           "its own conservative default; nothing is over-spent.")
        if (proc.stderr or "").strip():
            out["meter_stderr"] = (proc.stderr or "").strip()[:300]
        return out

    out["ceiling_pct"] = ceiling
    try:
        sys.path.insert(0, str(KIT_DIR / "scripts"))
        import gru_allowance
        allowance = gru_allowance.compute(ceiling, gru_frac or None)
    except Exception as exc:  # noqa: BLE001
        out["note"] = f"could not compute allowance: {exc}"
        return out
    if allowance:
        out["gru_allowance_pct"] = allowance
        try:
            out["others_pct"] = f"{float(ceiling) - float(allowance):.4f}"
        except ValueError:
            pass
        if float(ceiling) == 0.0:
            out["note"] = ("ceiling is a real 0.0 -- this hour is already at or past "
                           "sustainable pace once other instances' reservations are counted.")
    return out


class State:
    """In-memory snapshot, refreshed by two background loops. Reads never block on either."""
    def __init__(self):
        self.lock = threading.Lock()
        self.runs: list[dict] = []
        self.gh = {"prs": [], "issues": [], "polled_at": 0}
        self._seen_offset = 0

    def load_existing_runs(self):
        if not RUNS_FILE.exists():
            return
        lines = RUNS_FILE.read_text(errors="ignore").splitlines()
        with self.lock:
            self._seen_offset = RUNS_FILE.stat().st_size
            for line in lines[-MAX_RUNS:]:
                try:
                    self.runs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    def tail_runs_forever(self):
        # A separate sqlite connection for the background thread -- sqlite3 connections aren't
        # shared across threads by default, and this loop's writes (fleet_db.sync) are
        # independent of anything a request handler reads, so a dedicated connection is
        # simpler than adding a lock around a shared one.
        db = fleet_db.connect()
        while True:
            try:
                if RUNS_FILE.exists():
                    size = RUNS_FILE.stat().st_size
                    if size < self._seen_offset:
                        self._seen_offset = 0  # file rotated/truncated underneath us
                    if size > self._seen_offset:
                        with RUNS_FILE.open() as fh:
                            fh.seek(self._seen_offset)
                            new = fh.read()
                            self._seen_offset = fh.tell()
                        with self.lock:
                            for line in new.splitlines():
                                if not line.strip():
                                    continue
                                try:
                                    self.runs.append(json.loads(line))
                                except json.JSONDecodeError:
                                    continue
                            self.runs = self.runs[-MAX_RUNS:]
                fleet_db.sync(db)
            except OSError:
                pass
            time.sleep(2)

    def poll_gh_forever(self):
        while True:
            gh = poll_gh_state()
            with self.lock:
                self.gh = gh
            time.sleep(GH_POLL_S)

    def tail_member_logs_forever(self):
        """Raw log lines, live, per member -- Reif: 'we are piping everything, i want to see
        it in a feed'. run_member.sh already streams thinking:/tool call:/tool result: lines
        into each member's own <name>.log AS THEY HAPPEN (stream_log.py, piped through `log`)
        -- this loop is the missing other half: it existed on disk the whole time, nothing
        ever tailed it for the dashboard. runs.jsonl (tailed above) only carries the FINAL
        summary once a pass ends; this is the live, in-progress detail a human watching the
        page actually asked to see.
        """
        offsets: dict[str, int] = {}
        while True:
            try:
                for log_path in sorted(LOG_DIR.glob("*.log")):
                    member = log_path.stem
                    try:
                        size = log_path.stat().st_size
                    except OSError:
                        continue
                    seen = offsets.get(member, size)  # first sight of a file: start at EOF,
                    # never replay a whole historical log as if it just happened
                    if member not in offsets:
                        offsets[member] = size
                        continue
                    if size < seen:
                        seen = 0  # rotated/truncated underneath us
                    if size > seen:
                        with log_path.open(errors="ignore") as fh:
                            fh.seek(seen)
                            new = fh.read()
                            offsets[member] = fh.tell()
                        for line in new.splitlines():
                            if line.strip():
                                broadcast("logline", {"member": member, "line": line})
            except OSError:
                pass
            time.sleep(1)

    def snapshot(self) -> dict:
        with self.lock:
            return {"runs": list(self.runs), "gh": dict(self.gh)}


STATE = State()

# Subscribers to the SSE stream; each is a Queue-like list drained by its own connection thread.
_subscribers: list[list[str]] = []
_subscribers_lock = threading.Lock()


def broadcast(event: str, data: dict) -> None:
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _subscribers_lock:
        for q in _subscribers:
            q.append(payload)


def watch_and_broadcast():
    """Re-derive what changed each tick and push only the delta as an SSE event."""
    last_run_count = 0
    last_gh_at = 0
    while True:
        snap = STATE.snapshot()
        if len(snap["runs"]) != last_run_count:
            new = snap["runs"][last_run_count:]
            last_run_count = len(snap["runs"])
            for rec in new:
                broadcast("run", rec)
        if snap["gh"]["polled_at"] != last_gh_at:
            last_gh_at = snap["gh"]["polled_at"]
            broadcast("gh", snap["gh"])
        time.sleep(1)


PAGE = (KIT_DIR / "scripts" / "fleet_view.html")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet the default stderr access log
        pass

    def _access_log(self, path: str, status: int):
        """Append one JSON line per API read to access.jsonl, for abuse forensics.

        What this can and cannot establish, stated plainly so nobody over-trusts it later:
        reads are anonymous by design (no key, Allow-Origin *), so NOTHING here is asserted
        identity -- a client supplies its own User-Agent/Origin/Referer and can forge all three.
        The one field a caller cannot forge is the source IP, and behind the Cloudflare tunnel
        even that is only true via CF-Connecting-IP; self.client_address is the tunnel's own
        loopback for every remote request and is useless for attribution. Recorded so a burst
        can be characterised after the fact, not so anyone can be authenticated.
        """
        h = self.headers
        entry = {
            "ts": time.time(),
            "path": path,
            "status": status,
            # Cloudflare's client IP first; the raw socket peer is the tunnel itself.
            "ip": (h.get("CF-Connecting-IP") or h.get("X-Forwarded-For") or
                   (self.client_address[0] if self.client_address else "")),
            "peer": self.client_address[0] if self.client_address else "",
            "cf_country": h.get("CF-IPCountry") or "",
            "ua": (h.get("User-Agent") or "")[:300],
            "origin": (h.get("Origin") or "")[:200],
            "referer": (h.get("Referer") or "")[:200],
        }
        try:
            with (LOG_DIR / "access.jsonl").open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # logging must never take the server down

    def _json(self, obj: dict, status: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # CORS on API responses so a browser on another origin (philanthropy.org) can actually
        # READ these. Without it every cross-origin fetch() fails at the origin check even
        # though the request returns 200 -- the response arrives and the browser discards it.
        #
        # `*` is deliberate and safe HERE, and only because of what it does NOT cover:
        #   - Allow-Origin `*` forbids credentials by spec, so no cookie or auth header rides
        #     along on a cross-site request.
        #   - Only GET is advertised. A cross-origin POST to a write route still preflights,
        #     gets no Allow-Methods for POST, and never reaches do_POST's key gate.
        #   - X-Fleet-Key is NOT in Allow-Headers, so a page cannot send the write key from a
        #     browser at all, even if it somehow had one.
        # Read routes were already world-readable through the tunnel before this line existed;
        # this changes who can PARSE the bytes, not who can fetch them.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()
        self.wfile.write(body)
        if self.command == "GET":
            self._access_log(urlparse(self.path).path, status)

    def do_OPTIONS(self):
        """CORS preflight. Answers for GET only -- a POST preflight gets no Allow-Methods for
        POST and the browser refuses the real request before the write gate is ever consulted."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self) -> bool:
        """True if this request may perform a write. See the gate in do_POST for the why.

        FAILS CLOSED for anything off-box. With no FLEET_API_KEY set, a remote POST is refused
        rather than allowed -- an unset key must never silently mean "no authentication", which
        is precisely the state that left run_now world-callable in the first place. Localhost
        stays allowed without a key so an operator on the box (and the container's own cron,
        which POSTs nothing today but might) is never locked out of their own fleet by a
        missing config value.
        """
        client = self.client_address[0] if self.client_address else ""
        if client in ("127.0.0.1", "::1", "localhost"):
            return True
        key = api_key()
        if not key:
            return False   # fail closed: no key configured => no remote writes, ever
        sent = (self.headers.get("X-Fleet-Key") or "").strip()
        if sent and hmac.compare_digest(sent, key):
            return True
        # Same-origin session cookie -- the ONLY credential a browser can actually present.
        # _cors deliberately keeps X-Fleet-Key out of Allow-Headers so a page cannot send the
        # write key; without this branch that made every POST route unreachable from the very
        # UI they were built for (live 2026-09-02: Settings dials showed "save failed", server
        # logged DENIED, and all 11 write buttons were dead for any remote operator).
        # Safe against a cross-site caller for the same reasons the header path is: Allow-Origin
        # `*` forbids credentials, only GET is advertised in Allow-Methods, and the cookie is
        # SameSite=Strict so a third-party page's POST never carries it.
        return hmac.compare_digest(self._session_cookie(), _session_token(key))

    def _handle_login(self, body: dict) -> None:
        """Exchange FLEET_API_KEY for a same-origin session cookie.

        This is the one POST that runs BEFORE _authorized(), so it carries the whole
        fail-closed burden itself: with no key configured it refuses outright rather than
        treating "unset" as "no authentication needed" -- the exact state that left run_now
        world-callable before the gate existed (2026-08-25 incident).

        Cookie flags are load-bearing, not decoration:
          HttpOnly     -- page scripts cannot read it, so an XSS on this dashboard cannot
                          lift the session and replay it elsewhere.
          SameSite=Strict -- it never rides a cross-site request, which is what keeps a
                          third-party page from POSTing to /api/run_now on the operator's
                          behalf. This is the CSRF defense; do not relax it to Lax.
          Path=/       -- every write route is under the same origin.
        Secure is set only when the request arrived over TLS: the tunnel terminates HTTPS,
        but an operator on the box hits plain http://localhost and a Secure cookie would be
        silently dropped there.
        """
        key = api_key()
        if not key:
            # Fail closed, and say why -- an operator staring at a dead Save button deserves
            # the actual reason rather than a generic 401.
            self._json({"ok": False,
                        "error": "no FLEET_API_KEY configured on this instance -- set it in "
                                 "fleet.env and restart the container to enable writes"}, 503)
            return
        sent = str(body.get("key", "")).strip()
        if not sent or not hmac.compare_digest(sent, key):
            client = self.client_address[0] if self.client_address else "?"
            print(f"[fleet-view] LOGIN FAILED from {client}", flush=True)
            self._json({"ok": False, "error": "wrong key"}, 401)
            return
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
        body_bytes = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Set-Cookie",
                         f"fleet_session={_session_token(key)}; HttpOnly; SameSite=Strict; "
                         f"Path=/; Max-Age=31536000{secure}")
        self.end_headers()
        self.wfile.write(body_bytes)

    def _session_cookie(self) -> str:
        """This request's fleet_session cookie value, or "" -- never raises on junk input."""
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "fleet_session":
                return value.strip()
        return ""

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/status":
            # Server-rendered, unauthenticated, no JS: a status page has to be readable
            # precisely when the thing it reports on is broken, so it must not depend on
            # this server's own API, a session, or a client runtime to say "down".
            try:
                import status_page
                body = status_page.render().encode()
                code = 200
            except Exception as exc:  # noqa: BLE001
                # Never 500 a status page into a blank screen -- say what broke.
                body = ("<!doctype html><meta charset=utf-8><title>Fleet status</title>"
                        "<body style='font:14px system-ui;padding:40px'>"
                        "<h1>Fleet status unavailable</h1><p>The status page itself failed "
                        "to render: <code>%s</code></p>" % type(exc).__name__).encode()
                code = 503
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/":
            html = PAGE.read_text() if PAGE.exists() else "<h1>fleet_view.html missing</h1>"
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # No caching -- this page has genuinely gone stale in a browser across a redeploy
            # before (a button acting on backend logic that changed underneath the old JS,
            # looking like the button was just broken). It's a live status page, never worth
            # a byte of caching.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/snapshot":
            self._json(STATE.snapshot())
            return
        if path == "/api/spend":
            qs = parse_qs(urlparse(self.path).query)
            hours = float(qs.get("hours", ["24"])[0])
            member = qs.get("member", [None])[0]
            db = fleet_db.connect()
            fleet_db.sync(db)
            self._json({"spend": fleet_db.spend(db, member=member, hours=hours), "hours": hours})
            return
        if path == "/api/kpi":
            # Per-member headline count (fleet_kpi.py), summed over a time window -- reuses
            # STATE.runs (already in memory, already the live source for the run feed) rather
            # than a fresh gh/db query, since this only needs member+outcome+ts, all present
            # on every in-memory run record.
            qs = parse_qs(urlparse(self.path).query)
            hours = float(qs.get("hours", ["24"])[0])
            cutoff = time.time() - hours * 3600
            snap = STATE.snapshot()
            windowed = [r for r in snap["runs"] if (r.get("ts") or 0) >= cutoff]
            try:
                members = member_spec.load_all()
                names = [m["name"] for m in members]
            except Exception:
                names = sorted({r.get("member") for r in windowed if r.get("member")})
            out = [fleet_kpi.sum_kpi_over_runs(name, windowed) for name in names]
            self._json({"kpi": out, "hours": hours})
            return
        if path == "/api/stats/runs_summary":
            qs = parse_qs(urlparse(self.path).query)
            hours = float(qs.get("hours", ["24"])[0])
            snap = STATE.snapshot()
            self._json(fleet_stats.runs_summary(snap["runs"], hours=hours))
            return
        if path == "/api/stats/token_usage":
            qs = parse_qs(urlparse(self.path).query)
            hours = float(qs.get("hours", ["24"])[0])
            snap = STATE.snapshot()
            self._json({"buckets": fleet_stats.token_usage_by_hour(snap["runs"], hours=hours), "hours": hours})
            return
        if path == "/api/stats/backlog_history":
            # Own gh calls, not the cached STATE.gh snapshot -- that only carries currently-OPEN
            # issues (poll_gh_state's own --state open filter), but backlog_history needs every
            # issue ever labeled fleet:backlog, including closed ones, to reconstruct the
            # historical open-count trend.
            #
            # CACHED, 120s (Reif, 2026-08-25). The original note here guessed "a fresh call each
            # time is cheap enough"; measured, it was 8.4s per Stats page load -- two blocking gh
            # calls (1000 issues + 500 PRs) re-run on every load and reload, by every viewer,
            # to redraw buckets that are DAY-granular and cannot change between two loads a
            # minute apart. 120s keeps the page honest against a fleet that ships several PRs an
            # hour while making the second load instant.
            qs = parse_qs(urlparse(self.path).query)
            days = int(qs.get("days", ["14"])[0])

            def _build_backlog_history():
                issues_raw = _gh("issue", "list", "--state", "all", "--label", "fleet:backlog",
                                  "--json", "number,createdAt,closedAt", "--limit", "1000")
                # New-PRs-opened + PRs-merged (shipped) per day -- the throughput counterpart to
                # backlog size, plotted on the same chart/x-axis, so it's fetched alongside rather
                # than as a separate endpoint the frontend has to join itself.
                prs_raw = _gh("pr", "list", "--state", "all", "--json", "createdAt,mergedAt", "--limit", "500")
                # _gh swallows failure into "" (timeout, rate limit, auth blip), and both
                # aggregators turn "" into a full run of zero-count days -- which is
                # indistinguishable, on the chart, from a genuinely empty backlog. Caching that
                # would pin a false flatline over the real trend for the whole TTL, so refuse to
                # cache it: raise, and let the caller serve this one request uncached. Slow beats
                # confidently wrong on a page whose only job is to tell the truth about the fleet.
                pr_activity = fleet_stats.pr_activity_by_day(prs_raw, days=days)
                payload = {
                    "days": fleet_stats.backlog_history(issues_raw, days=days),
                    "new_prs": pr_activity["new_prs"],
                    "merged_prs": pr_activity["merged_prs"],
                }
                return payload, bool(issues_raw.strip())

            # Key on days: /api/stats/backlog_history?days=7 and ?days=30 are different answers.
            self._json(_cached(f"backlog_history:{days}", 120.0, _build_backlog_history))
            return
        if path == "/api/stats/self_improve_score":
            # Read-only tail of self_improve_score.jsonl -- written every 3h by
            # self_improve_score.sh (cron, on dino), NOT computed here. This endpoint's only
            # job is to hand the frontend the latest score + a short trend, same "server owns
            # aggregation, this file is a plain jsonl the server tails" pattern as runs.jsonl.
            #
            # days= is a WINDOW IN DAYS, not a row count. It used to be `history[-days:]`, which
            # was the same thing back when the score was daily -- at one row per 3h it would
            # mean "the last 1.75 days" and quietly shrink the chart to a stub. Rows written
            # before the 3h switch carry a bare "2026-08-25" date; newer ones carry a full
            # timestamp. Both start with YYYY-MM-DD, so a string compare on the first 10 chars
            # windows them correctly without having to parse either shape.
            qs = parse_qs(urlparse(self.path).query)
            days = int(qs.get("days", ["14"])[0])
            score_file = LOG_DIR / "self_improve_score.jsonl"
            history = []
            if score_file.exists():
                for line in score_file.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        history.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            cutoff = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
            history = [h for h in history if str(h.get("date", ""))[:10] >= cutoff]
            latest = history[-1] if history else None
            self._json({"latest": latest, "history": history})
            return
        if path == "/api/query":
            qs = parse_qs(urlparse(self.path).query)
            db = fleet_db.connect()
            fleet_db.sync(db)
            rows = fleet_db.query_runs(
                db, member=qs.get("member", [None])[0], status=qs.get("status", [None])[0],
                item_id=qs.get("item_id", [None])[0],
                limit=int(qs.get("limit", ["100"])[0]))
            self._json({"runs": rows})
            return
        if path == "/api/fleet_state":
            self._json(read_env_flags())
            return
        if path == "/api/budget_preview":
            # Show the operator the ACTUAL arithmetic behind the two dials, with live numbers.
            # Reif, 2026-09-02: "we can easily mess this up and it be way wrong" -- and it had
            # been, silently, for weeks (see gru_allowance.py's header). Two nested percentages
            # that LOOK independent are exactly the shape a human mis-tunes, so the page shows
            # the derivation and the resulting number rather than two bare inputs.
            self._json(_budget_preview())
            return
        if path == "/api/members":
            # Every member's reviewed spec + whatever's currently overridden on top of it --
            # the same effective config a running pass would get (member_spec.load + overrides.apply,
            # not a re-derivation of that logic).
            out = []
            try:
                specs = member_spec.load_all()
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
                return
            for spec in specs:
                eff, applied = ov.apply(spec)
                out.append({"spec": spec, "effective": eff, "overrides": applied})
            self._json({"members": out})
            return
        if path == "/api/next_fires":
            self._json({"next_fires": next_fires()})
            return
        if path == "/api/pass_log":
            qs = parse_qs(urlparse(self.path).query)
            member = qs.get("member", [""])[0]
            # nth-from-end: the feed row and the log block are both written in chronological
            # order by the SAME pass, one record per pass -- so "the Nth most recent run for
            # this member" and "the Nth most recent pass start/end block in this member's log"
            # name the same pass without needing a shared run_id in the log lines themselves.
            n = int(qs.get("n", ["0"])[0])
            self._json({"lines": read_pass_block(member, n)})
            return
        if path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q: list[str] = []
            with _subscribers_lock:
                _subscribers.append(q)
            try:
                while True:
                    if q:
                        chunk = q.pop(0)
                        try:
                            self.wfile.write(chunk.encode())
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                    else:
                        time.sleep(0.3)
            finally:
                with _subscribers_lock:
                    if q in _subscribers:
                        _subscribers.remove(q)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}

        # --- AUTH GATE: every write route, one check ------------------------------------------
        # Found live 2026-08-25: this server has always been unauthenticated, and it is published
        # to the public internet through the Cloudflare tunnel. Anyone who knew the URL could POST
        # a member name to /api/run_now and spawn `claude -p --dangerously-skip-permissions` on
        # this box -- burning the account pool's budget and running an agent with repo write
        # access and a gh token. Verified by POSTing an invalid member from off-box and getting
        # this handler's own 400 back, which proves reachability and input processing.
        #
        # The gate lives HERE, at the top of do_POST, rather than per-route on purpose: every
        # mutating endpoint (steer/prune/close_pr/create_issue/comment_issue/close_issue/
        # fleet_toggle/run_now) is a POST, so one check covers all of them and a NEW write route
        # added later is protected by default instead of being protected only if its author
        # remembered. Read routes (GET) stay open -- the dashboard is a read-only view and
        # requiring a header would break it in the browser for no security gain.
        #
        # Compared with hmac.compare_digest, not `==`: a plain string compare returns early on
        # the first differing byte, which leaks key material to a patient attacker timing
        # responses. Constant-time comparison is the standard fix and costs nothing here.
        # /api/login is the ONE route ahead of the gate -- it exists to satisfy the gate, so
        # sitting behind it would make it unreachable by the only client that needs it. It
        # carries its own fail-closed check instead (see _handle_login).
        if path == "/api/login":
            self._handle_login(body)
            return

        if not self._authorized():
            client = self.client_address[0] if self.client_address else "?"
            print(f"[fleet-view] DENIED {path} from {client} (bad or missing X-Fleet-Key)",
                  flush=True)
            self._json({"ok": False, "error": "unauthorized -- sign in on the Settings page"}, 401)
            return

        # --- steer: throttle/disable/re-tune a member, via overrides.py (dials only, by design
        # -- see that module's header: prompt/tools are PR-only even from this page). ----------
        if path == "/api/steer":
            member = body.get("member", "")
            key = body.get("key", "")
            value = body.get("value")
            why = body.get("why", "fleet-view UI")
            if not (member and key):
                self._json({"ok": False, "error": "member and key required"}, 400)
                return
            cmd = [sys.executable, str(KIT_DIR / "scripts" / "overrides.py"), member,
                   "--set", key, json.dumps(value), "--by", "fleet-view", "--why", why]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            self._json({"ok": p.returncode == 0, "out": p.stdout, "err": p.stderr})
            return

        # --- prune: release a stuck claim back to the board, via board_github.py's own release
        # verb (the exact gap learning #7 in deployment-learnings.md names as not-yet-fixed --
        # this button is the same code path worktree_builder.sh's own failure trap now calls,
        # not a second hand-rolled implementation of "what does release mean"). ---------------
        if path == "/api/prune":
            number = body.get("issue")
            note = body.get("note", "released from fleet-view: stuck claim")
            if not number:
                self._json({"ok": False, "error": "issue number required"}, 400)
                return
            p = subprocess.run([sys.executable, str(KIT_DIR / "scripts" / "board_github.py"),
                               "release", str(number), note],
                               cwd=REPO or None, capture_output=True, text=True, timeout=15)
            self._json({"ok": p.returncode == 0, "out": p.stdout, "err": p.stderr})
            return

        # --- close a PR outright (steering the fleet away from a bad direction, not just a
        # stuck claim). ---------------------------------------------------------------------
        if path == "/api/close_pr":
            number = body.get("pr")
            if not number:
                self._json({"ok": False, "error": "pr number required"}, 400)
                return
            p = subprocess.run(["gh", "pr", "close", str(number)], cwd=REPO or None,
                               capture_output=True, text=True, timeout=15)
            self._json({"ok": p.returncode == 0, "out": p.stdout, "err": p.stderr})
            return

        # --- issue CRUD, straight `gh` calls same as close_pr above -- a human filing/closing
        # a backlog item from the page is the same action as typing the command, never a second
        # authority. create ALWAYS applies fleet:backlog (the board-first LAW: nothing is worked
        # without an item) plus whatever lane label the caller names. ------------------------
        if path == "/api/create_issue":
            title = (body.get("title") or "").strip()
            issue_body = body.get("body", "")
            lane = body.get("lane", "")
            if not title:
                self._json({"ok": False, "error": "title required"}, 400)
                return
            labels = "fleet:backlog" + (f",lane:{lane}" if lane else "")
            p = subprocess.run(["gh", "issue", "create", "--title", title, "--body", issue_body,
                                "--label", labels], cwd=REPO or None,
                                capture_output=True, text=True, timeout=15)
            self._json({"ok": p.returncode == 0, "out": p.stdout, "err": p.stderr})
            return

        # --- Reif-priority epic: "outside of everything else in the queue, do this first."
        # Creates one issue labeled fleet:reif-priority,fleet:epic,fleet:backlog,
        # fleet:priority-high. gru (gru.md step 2) checks for an open fleet:reif-priority
        # issue BEFORE reading marie's normal ranking and builds only work under it while one
        # is open, full allowance, no RICE competition. marie (marie.md Part C) never re-ranks
        # or cruft-closes a fleet:reif-priority issue -- it closes the epic itself once a pass
        # finds no more actionable child issues/PRs referencing it (gh#<epic> convention,
        # same as every other cross-reference in this repo), and says why in its report.
        # Confirmed live 2026-08-30 (Reif): "I want it as a priority, and then a few branching
        # PRs, and then they consider it done" -- fleet self-closes, human doesn't have to.
        # Label creation is idempotent (gh label create errors harmlessly if present already),
        # same pattern marie.md's own priority labels use.
        if path == "/api/priority_epic":
            title = (body.get("title") or "").strip()
            goal_body = body.get("body", "")
            if not title:
                self._json({"ok": False, "error": "title required"}, 400)
                return
            subprocess.run(["gh", "label", "create", "fleet:reif-priority", "--color", "b60205",
                            "--description", "Reif's standing top priority -- gru builds this "
                            "before anything else; only the fleet closes it, when no child work "
                            "remains"], cwd=REPO or None, capture_output=True, text=True, timeout=15)
            subprocess.run(["gh", "label", "create", "fleet:epic", "--color", "5319e7",
                            "--description", "A fleet:reif-priority issue that gru builds "
                            "against and marie tracks for child-work completion"],
                            cwd=REPO or None, capture_output=True, text=True, timeout=15)
            p = subprocess.run(["gh", "issue", "create", "--title", title, "--body", goal_body,
                                "--label", "fleet:reif-priority,fleet:epic,fleet:backlog,"
                                "fleet:priority-high"],
                                cwd=REPO or None, capture_output=True, text=True, timeout=15)
            self._json({"ok": p.returncode == 0, "out": p.stdout, "err": p.stderr})
            return

        if path == "/api/comment_issue":
            number = body.get("issue")
            text = (body.get("body") or "").strip()
            if not (number and text):
                self._json({"ok": False, "error": "issue and body required"}, 400)
                return
            p = subprocess.run(["gh", "issue", "comment", str(number), "--body", text],
                               cwd=REPO or None, capture_output=True, text=True, timeout=15)
            self._json({"ok": p.returncode == 0, "out": p.stdout, "err": p.stderr})
            return

        if path == "/api/close_issue":
            number = body.get("issue")
            reason = body.get("reason", "")  # "" | "completed" | "not planned"
            if not number:
                self._json({"ok": False, "error": "issue number required"}, 400)
                return
            cmd = ["gh", "issue", "close", str(number)]
            if reason:
                cmd += ["--reason", reason]
            p = subprocess.run(cmd, cwd=REPO or None, capture_output=True, text=True, timeout=15)
            self._json({"ok": p.returncode == 0, "out": p.stdout, "err": p.stderr})
            return

        # --- master kill switch (fleet.env's FLEET_ENABLED, read by fleet_enabled.sh on every
        # invocation) vs a per-member kill switch (each member's OWN spec.enabled field, read
        # by run_member.sh -- there is no per-member env var; fleet_enabled_or_exit only ever
        # checks the global one). Two different mechanisms because they gate two different
        # things: the whole fleet vs one member's own schedule. ------------------------------
        if path == "/api/fleet_settings":
            # DIAL_FIELDS only -- server-side allow-list, same spirit as fleet_toggle's
            # MEMBERS check. Silently ignores any key not on the list rather than writing
            # it; never trust client-submitted field names.
            written = []
            for key, value in body.items():
                if key in DIAL_FIELDS:
                    write_env_field(key, str(value))
                    written.append(key)
            self._json({"ok": True, "written": written, "state": read_env_flags()})
            return

        if path == "/api/fleet_toggle":
            target = body.get("target", "")  # "fleet" or a name from MEMBERS
            value = bool(body.get("value"))
            if target == "fleet":
                write_env_flag("FLEET_ENABLED", value)
            elif target in MEMBERS:
                cmd = [sys.executable, str(KIT_DIR / "scripts" / "overrides.py"), target,
                       "--set", "enabled", json.dumps(value), "--by", "fleet-view",
                       "--why", "dashboard toggle"]
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if p.returncode != 0:
                    self._json({"ok": False, "error": p.stderr or p.stdout}, 500)
                    return
            else:
                self._json({"ok": False, "error": f"unknown toggle target {target!r}"}, 400)
                return
            self._json({"ok": True, "state": read_env_flags()})
            return

        # --- run a member right now, off-cron, for watching one in the wild before flipping
        # the next one on. Runs in the BACKGROUND (Popen, not run) -- a real builder pass can
        # take many minutes and this HTTP request must return immediately; the pass's own
        # result shows up on the live runs feed the same way a cron-fired one does, because it
        # writes to the same runs.jsonl through the same run_report.py call the script always
        # makes. FLEET_RUN_NOW=1 tells the script to skip its own kill-switch checks (see
        # fleet_enabled.sh) -- a manual click is an explicit human action, not the thing those
        # switches exist to gate; without this, testing a member you deliberately keep off
        # cron would be impossible. ------------------------------------------------------------
        if path == "/api/run_now":
            name = body.get("member", "")
            if name not in MEMBERS:
                self._json({"ok": False, "error": f"unknown member {name!r}"}, 400)
                return
            script = KIT_DIR / "scripts" / MEMBERS[name]["script"]
            env = dict(os.environ)
            env.setdefault("FLEET_ENV_FILE", str(ENV_FILE))
            env["FLEET_RUN_NOW"] = "1"
            try:
                subprocess.Popen(["bash", str(script), *MEMBERS[name].get("args", [])],
                                 cwd=REPO or None, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            except OSError as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
                return
            self._json({"ok": True, "started": name})
            return

        self.send_response(404)
        self.end_headers()


def main() -> int:
    if not REPO:
        print("fleet_view_server: FLEET_REPO not set (source fleet.env first)", file=sys.stderr)
        return 1
    STATE.load_existing_runs()
    threading.Thread(target=STATE.tail_runs_forever, daemon=True).start()
    threading.Thread(target=STATE.poll_gh_forever, daemon=True).start()
    threading.Thread(target=STATE.tail_member_logs_forever, daemon=True).start()
    threading.Thread(target=watch_and_broadcast, daemon=True).start()
    STATE.gh = poll_gh_state()  # one synchronous poll so the first page load isn't empty

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"fleet_view_server: serving http://0.0.0.0:{PORT}  (repo={REPO}, runs={RUNS_FILE})",
          file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
