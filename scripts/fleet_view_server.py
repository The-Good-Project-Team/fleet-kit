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
import member_spec       # noqa: E402
import overrides as ov   # noqa: E402  ('overrides' shadows nothing here; keep the module name clear)

REPO = os.environ.get("FLEET_REPO", "")
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
RUNS_FILE = LOG_DIR / "runs.jsonl"
PORT = int(os.environ.get("FLEET_VIEW_PORT", "8420"))
GH_POLL_S = int(os.environ.get("FLEET_VIEW_GH_POLL_S", "20"))
MAX_RUNS = 500  # bound memory; this is a window, not an archive -- runs.jsonl on disk is the archive


def _gh(*args: str, timeout: int = 15) -> str:
    try:
        p = subprocess.run(["gh", *args], cwd=REPO or None, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def poll_gh_state() -> dict:
    prs_raw = _gh("pr", "list", "--state", "open", "--json",
                   "number,title,isDraft,headRefName,url,statusCheckRollup,updatedAt")
    issues_raw = _gh("issue", "list", "--state", "open", "--label", "fleet:backlog", "--json",
                      "number,title,labels,updatedAt", "--limit", "100")
    try:
        prs = json.loads(prs_raw) if prs_raw else []
    except json.JSONDecodeError:
        prs = []
    try:
        issues = json.loads(issues_raw) if issues_raw else []
    except json.JSONDecodeError:
        issues = []
    for pr in prs:
        checks = pr.get("statusCheckRollup") or []
        states = {c.get("state") or c.get("conclusion") for c in checks}
        pr["_rollup"] = ("failing" if states & {"FAILURE", "ERROR", "failure"} else
                          "pending" if states & {"PENDING", "IN_PROGRESS", None} else
                          "green" if checks else "none")
    for issue in issues:
        names = {lb.get("name") for lb in issue.get("labels") or []}
        issue["_claimed"] = any(n and n.endswith(":claimed") for n in names)
    return {"prs": prs, "issues": issues, "polled_at": time.time()}


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

    def _json(self, obj: dict, status: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            html = PAGE.read_text() if PAGE.exists() else "<h1>fleet_view.html missing</h1>"
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
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

        # --- prune: release a stuck claim back to the board (the exact gap learning #7 in
        # deployment-learnings.md names as not-yet-fixed -- this button calls board_github.py's
        # `done`/label edit directly via gh so a human can unstick one without shelling in). ----
        if path == "/api/prune":
            number = body.get("issue")
            note = body.get("note", "released from fleet-view: stuck claim")
            if not number:
                self._json({"ok": False, "error": "issue number required"}, 400)
                return
            prefix = os.environ.get("FLEET_LABEL_PREFIX", "fleet:")
            p1 = subprocess.run(["gh", "issue", "edit", str(number), "--remove-label",
                                  f"{prefix}claimed"], cwd=REPO or None,
                                 capture_output=True, text=True, timeout=15)
            p2 = subprocess.run(["gh", "issue", "comment", str(number), "--body", note],
                                 cwd=REPO or None, capture_output=True, text=True, timeout=15)
            self._json({"ok": p1.returncode == 0, "out": p1.stdout + p2.stdout,
                       "err": p1.stderr + p2.stderr})
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

        self.send_response(404)
        self.end_headers()


def main() -> int:
    if not REPO:
        print("fleet_view_server: FLEET_REPO not set (source fleet.env first)", file=sys.stderr)
        return 1
    STATE.load_existing_runs()
    threading.Thread(target=STATE.tail_runs_forever, daemon=True).start()
    threading.Thread(target=STATE.poll_gh_forever, daemon=True).start()
    threading.Thread(target=watch_and_broadcast, daemon=True).start()
    STATE.gh = poll_gh_state()  # one synchronous poll so the first page load isn't empty

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"fleet_view_server: serving http://0.0.0.0:{PORT}  (repo={REPO}, runs={RUNS_FILE})",
          file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
