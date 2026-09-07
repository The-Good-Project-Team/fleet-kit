#!/usr/bin/env python3
"""journey_issue_filer -- turns a failed sentry journey step into a filed, deduped,
self-closing GitHub issue (gh#660).

THE GAP THIS CLOSES. #656 gave sentry a catalog of journeys (`members/sentry/journeys.yaml`);
#657 (still unbuilt as of this PR -- see PART OF, below) will give it a walker that runs them
and records pass/fail per step. Neither one turns a failure into something a builder can act
on: today that would mean a human reading sentry's own run output line by line. This is the
seam between "a step failed" and "there is exactly one open, evidence-carrying issue for it,
and it closes itself the moment the step passes again."

RESULTS FILE CONTRACT (the interface #657's walker must produce; this is the schema this
file was written against, since #657 does not exist yet -- see PART OF):

  {
    "run": "<run id, e.g. a timestamp or qa-out/<run> directory name>",
    "deploy_sha": "<sha under test this run, or \"\" if unknown>",
    "journeys": [
      {
        "id": "sign-in",                 # matches journeys.yaml's id, kebab-case
        "name": "Sign in",               # matches journeys.yaml's name
        "steps": [
          {
            "index": 0,                  # 0-based position in the journey
            "action": "Navigate to https://philanthropy.org/login",
            "observable_result": "The page renders a sign-in form ...",
            "status": "pass" | "fail",
            "screenshot": "qa-out/<run>/journeys/sign-in/desktop/0.png",  # optional
            "request": {...} | null,     # optional, HTTP request that broke, if observable
            "response": {...} | null     # optional, HTTP response that broke, if observable
          },
          ...
        ]
      },
      ...
    ]
  }

Conventionally written by the walker to `qa-out/<run>/journeys/results.json`, sibling to the
per-step screenshot directories `qa-out/<run>/journeys/<id>/<viewport>/` journeys.yaml already
documents. This file does not read journeys.yaml itself -- the walker already parsed the
catalog to run it, so the walker is what carries action/observable_result into the results
file. That keeps this module decoupled from the catalog's YAML format (no PyYAML dependency
here) and gives it one, already-validated source of truth per run.

LABEL: `fleet:sentry-journey` (UNKNOWN #1 in #660's PRD) -- a new label, not a reuse of
`fleet:visual-change` (#659's), because #659's label is about a visual diff crossing a
threshold and this is about a functional step failing outright; #661's KPI tile and any future
dedup need to be able to tell those apart by label alone.

LAST-PASSING SHA: (UNKNOWN #2) tracked in a small per-journey-step state file (default
`~/.cache/fleet-kit/journey_last_pass.json`), not derived from `runs.jsonl` after the fact --
runs.jsonl records one row per MEMBER PASS, not per journey step, so recovering "which sha last
passed this exact step" from it would mean re-parsing every historical sentry run's own qa-out
tree. A small state file keyed by `<journey_id>::step<index>` is the same shape auto_deploy.sh
already uses for its own last-sha tracking (`STATE=...auto_deploy.last_sha.*`).

DEDUPE: match on a hidden marker in the issue body (`<!-- fleet:sentry-journey key=... -->`),
not on title text -- a title can be edited or reworded by a human without breaking the match,
same reasoning closes_gate.py's own comment-parsing takes for machine-vs-human text.

PART OF #660, not Closes: acceptance criterion 1 requires a failed step to result in an issue
"within the same [sentry] pass" -- that pass does not exist yet, since #657 (the walker that
would actually run journeys.yaml and emit a results.json) is still unbuilt on main as of this
PR (confirmed: `gh issue view 657` shows it open, unclaimed, no merged PR). This module is
built and demonstrated standalone against the results-file contract above (see
`test_journey_issue_filer.py`, which exercises file -> repeat -> recover end to end against a
mocked `gh`) so it needs zero changes once #657 lands and starts writing that file for real.

Pure command-BUILDERS are separated from `gh`-executing calls so tests assert exact argv and
exact title/body text without a network call -- same split board_github.py already uses.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PREFIX = os.environ.get("FLEET_LABEL_PREFIX", "fleet:")
LABEL_JOURNEY = f"{PREFIX}sentry-journey"
LABEL_COLOR = "5319e7"
LABEL_DESC = "sentry: a journey step (gh#657/gh#660) failed and was filed by journey_issue_filer.py"

DEFAULT_STATE_PATH = Path(
    os.environ.get(
        "FLEET_JOURNEY_STATE",
        str(Path.home() / ".cache" / "fleet-kit" / "journey_last_pass.json"),
    )
)

MARKER_RE = re.compile(r"<!--\s*fleet:sentry-journey\s+key=([^\s]+?)\s*-->")


def step_key(journey_id: str, step_index: int) -> str:
    return f"{journey_id}::step{step_index}"


def marker_for(key: str) -> str:
    return f"<!-- fleet:sentry-journey key={key} -->"


def key_from_body(body: str) -> str | None:
    m = MARKER_RE.search(body or "")
    return m.group(1) if m else None


# --- pure content builders (unit-tested; never executed by tests) ------------------------------

def build_issue_title(journey_name: str, step_action: str) -> str:
    action = (step_action or "").strip().rstrip(".")
    if len(action) > 70:
        action = action[:67] + "..."
    return f'{journey_name}: "{action}" isn\'t working'


def _repro_steps(steps: list[dict], up_to_index: int) -> str:
    lines = []
    for s in steps:
        if s.get("index", 0) > up_to_index:
            break
        lines.append(f"{s.get('index', 0) + 1}. {s.get('action', '').strip()}")
    return "\n".join(lines)


def build_issue_body(
    journey: dict,
    step: dict,
    run: str,
    deploy_sha: str,
    last_pass_sha: str | None,
    key: str,
) -> str:
    lines = [
        f"Sentry drove the **{journey.get('name', journey.get('id'))}** journey as a real "
        "person would, and this step stopped doing its job.",
        "",
        f"**Failed step:** {step.get('action', '').strip()}",
        f"**Expected:** {step.get('observable_result', '').strip()}",
        "",
        "**Repro steps (run these in order):**",
        _repro_steps(journey.get("steps", [step]), step.get("index", 0)),
        "",
    ]
    screenshot = step.get("screenshot")
    if screenshot:
        lines += [f"**Screenshot:** {screenshot}", ""]
    request, response = step.get("request"), step.get("response")
    if request or response:
        lines += ["**What broke, on the wire:**"]
        if request:
            lines.append(f"- Request: `{json.dumps(request)}`")
        if response:
            lines.append(f"- Response: `{json.dumps(response)}`")
        lines.append("")
    lines += [
        "**Last deploy sha known to pass this step:** "
        + (last_pass_sha or "unknown -- this is the first observed failure"),
        f"**This run:** {run} (sha {deploy_sha or 'unknown'})",
        "",
        marker_for(key),
    ]
    return "\n".join(lines)


def build_file_cmd(title: str, body: str) -> list[str]:
    return ["gh", "issue", "create", "--title", title, "--body", body, "--label", LABEL_JOURNEY]


def build_list_cmd() -> list[str]:
    return [
        "gh", "issue", "list", "--state", "open", "--label", LABEL_JOURNEY,
        "--limit", "200", "--json", "number,body",
    ]


def build_comment_cmd(number: int, body: str) -> list[str]:
    return ["gh", "issue", "comment", str(number), "--body", body]


def build_close_cmd(number: int, body: str) -> list[str]:
    return ["gh", "issue", "close", str(number), "--comment", body]


# --- state (last-passing sha per journey+step) ---------------------------------------------

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


# --- execution -----------------------------------------------------------------------------

def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)


def ensure_label(runner=_run) -> None:
    """Idempotent: `gh` errors on a duplicate create; that failure is expected and ignored."""
    runner(["gh", "label", "create", LABEL_JOURNEY, "--color", LABEL_COLOR, "--description", LABEL_DESC])


def find_open_issue(key: str, runner=_run) -> int | None:
    rc, out = runner(build_list_cmd())
    if rc != 0:
        print(f"journey_issue_filer: list FAILED: {out[:300]}", file=sys.stderr)
        return None
    try:
        issues = json.loads(out)
    except json.JSONDecodeError:
        return None
    for issue in issues:
        if key_from_body(issue.get("body") or "") == key:
            return issue.get("number")
    return None


def load_results(path: Path) -> dict:
    return json.loads(path.read_text())


def iter_steps(results: dict):
    for journey in results.get("journeys", []):
        for step in journey.get("steps", []):
            yield journey, step


def process(results_path: Path, state_path: Path = DEFAULT_STATE_PATH, runner=_run, dry_run: bool = False) -> dict:
    """Walks one results.json, files/comments/closes as needed. Returns a summary dict of
    what happened -- never raises on a `gh` failure, since one bad call must not stop the rest
    of the run from being processed (same non-crashing-on-a-single-failure shape #657's own
    walker AC3 requires of itself)."""
    results = load_results(results_path)
    run = results.get("run", "")
    deploy_sha = results.get("deploy_sha", "")
    state = load_state(state_path)
    summary = {"filed": [], "commented": [], "closed": [], "errors": []}

    if not dry_run:
        ensure_label(runner)

    for journey, step in iter_steps(results):
        key = step_key(journey["id"], step.get("index", 0))
        status = step.get("status")

        if status == "fail":
            existing = None if dry_run else find_open_issue(key, runner)
            if existing:
                note = f"Recurred again on run `{run}` (sha `{deploy_sha or 'unknown'}`)."
                if not dry_run:
                    rc, out = runner(build_comment_cmd(existing, note))
                    if rc != 0:
                        summary["errors"].append(f"comment #{existing} failed: {out[:200]}")
                        continue
                summary["commented"].append({"issue": existing, "key": key})
            else:
                title = build_issue_title(journey.get("name", journey["id"]), step.get("action", ""))
                body = build_issue_body(journey, step, run, deploy_sha, state.get(key, {}).get("last_pass_sha"), key)
                if dry_run:
                    summary["filed"].append({"issue": None, "key": key, "title": title})
                    continue
                rc, out = runner(build_file_cmd(title, body))
                if rc != 0:
                    summary["errors"].append(f"file {key} failed: {out[:300]}")
                    continue
                print(out)  # the issue URL -- callers log it as the durable id
                m = re.search(r"/issues/(\d+)\s*$", out)
                summary["filed"].append({"issue": int(m.group(1)) if m else None, "key": key, "title": title})

        elif status == "pass":
            state[key] = {"last_pass_sha": deploy_sha, "last_pass_run": run}
            existing = None if dry_run else find_open_issue(key, runner)
            if existing:
                note = f"Passing again as of run `{run}` (sha `{deploy_sha or 'unknown'}`)."
                if not dry_run:
                    rc, out = runner(build_close_cmd(existing, note))
                    if rc != 0:
                        summary["errors"].append(f"close #{existing} failed: {out[:200]}")
                        continue
                summary["closed"].append({"issue": existing, "key": key})

    if not dry_run:
        save_state(state_path, state)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", required=True, type=Path, help="path to a walker results.json")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="per-journey last-pass state file")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen, touch nothing")
    args = ap.parse_args()

    summary = process(args.results, args.state, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
