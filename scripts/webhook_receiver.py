#!/usr/bin/env python3
"""webhook_receiver.py -- GitHub webhook -> run_member.sh, event-driven instead of polling.

Reif, 2026-08-21: the-fixer polls every 2 minutes, but a CI/deploy failure is a GitHub EVENT --
"a failed test" should fire the-fixer directly, not wait up to 2 minutes for the next poll.
This is the receiving end: a tiny stdlib HTTP server (no framework, matches member_spec.py's
own "whatever this needs to run on must already have it" reasoning) that verifies GitHub's
HMAC signature, checks the event is a completed+failed workflow_run on CI or deploy, and fires
`run_member.sh the-fixer` in the background.

WHY EVENT-DRIVEN DOESN'T REPLACE THE POLL: prod-down (health check failing with no CI signal
at all -- the exact case that motivated check.sh's double-probe) has no GitHub event to hook.
the-fixer's own poll interval should widen to a coarse backstop for THAT case once this is
wired in; this script only handles the two cases that DO have a real GitHub event: a red CI
run on the default branch, a red deploy run.

SECURITY: every request must carry a valid HMAC-SHA256 signature (X-Hub-Signature-256) keyed
on FLEET_WEBHOOK_SECRET, verified with a constant-time compare -- same shape as GitHub's own
docs recommend, this is the one place forging a request would let an attacker spend fleet
budget or spam the-fixer, so it is not optional.

Usage: FLEET_WEBHOOK_SECRET=<shared secret> FLEET_REPO=/path/to/target/repo \
         python3 webhook_receiver.py [--port 8562]
Wire GitHub -> Settings -> Webhooks -> Add webhook, Payload URL = this server's public path
(behind the Cloudflare Tunnel path ingress, e.g. https://dino.luckymachines.co/webhook),
Content type = application/json, Secret = the same FLEET_WEBHOOK_SECRET, events = "Workflow
runs" only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
LOG_FILE = LOG_DIR / "webhook_receiver.log"
SECRET = os.environ.get("FLEET_WEBHOOK_SECRET", "")
# Which workflows count as a "the-fixer should look at this" failure. Space-separated
# filenames, matched against workflow_run.path's basename -- same env-var convention as
# check.sh's FIXER_CI_WORKFLOW/FIXER_DEPLOY_WORKFLOW, so operators configure both once.
WATCHED_WORKFLOWS = set(
    os.environ.get("FLEET_WEBHOOK_WORKFLOWS", "ci.yml deploy.yml").split()
)


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    import datetime

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    with LOG_FILE.open("a") as fh:
        fh.write(f"[{ts}] {msg}\n")


def verify_signature(body: bytes, signature_header: str) -> bool:
    if not SECRET:
        return False  # never accept unsigned/unconfigured -- fail closed, not open
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    got = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, got)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence BaseHTTPServer's default stderr chatter
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        sig = self.headers.get("X-Hub-Signature-256", "")

        if not verify_signature(body, sig):
            log(f"REJECTED: bad or missing signature from {self.client_address[0]}")
            self.send_response(401)
            self.end_headers()
            return

        event = self.headers.get("X-GitHub-Event", "")
        # GitHub's webhook config offers "application/json" vs "application/x-www-form-urlencoded"
        # content types, and this kit's setup always requests json -- but a real delivery was
        # observed arriving form-urlencoded anyway (config.content_type correctly saved as
        # "application/json" on GitHub's side, Content-Type header on the actual POST said
        # x-www-form-urlencoded regardless). Unwrap defensively rather than trust the config
        # matches the wire format: form-encoded wraps the JSON as one field named `payload`.
        ctype = self.headers.get("Content-Type", "")
        raw_json = body
        if "x-www-form-urlencoded" in ctype:
            from urllib.parse import parse_qs

            parsed = parse_qs(body.decode("utf-8", errors="replace"))
            raw_json = (parsed.get("payload") or [""])[0].encode("utf-8")
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            log(f"REJECTED: unparseable body (content-type={ctype!r})")
            self.send_response(400)
            self.end_headers()
            return

        if event == "ping":
            log("ping received -- webhook configured correctly")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
            return

        if event != "workflow_run":
            self.send_response(204)
            self.end_headers()
            return

        wr = payload.get("workflow_run", {})
        status = wr.get("status")
        conclusion = wr.get("conclusion")
        wf_path = wr.get("path", "")
        wf_name = Path(wf_path).name if wf_path else ""

        self.send_response(200)  # ack immediately -- GitHub retries on non-2xx, don't hold it
        self.end_headers()

        if status != "completed":
            return
        if wf_name not in WATCHED_WORKFLOWS:
            log(f"ignored: workflow_run for {wf_name!r}, not in watch list {WATCHED_WORKFLOWS}")
            return
        if conclusion != "failure":
            log(f"ignored: {wf_name} completed with conclusion={conclusion}, not a fire")
            return

        sha = wr.get("head_sha", "?")[:12]
        log(f"FIRE: {wf_name} failed at {sha} -- launching the-fixer")
        run_member = KIT_DIR / "scripts" / "run_member.sh"
        try:
            # Detached, best-effort: this receiver's job is to notice and hand off, not to
            # wait out an incident-response pass (which can run up to the-fixer's own
            # timeout_s). A failure to LAUNCH is logged; the-fixer's own log covers the rest.
            subprocess.Popen(
                [str(run_member), "the-fixer"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            log(f"FATAL: could not launch run_member.sh the-fixer: {e}")


def main() -> int:
    if not SECRET:
        print(
            "FATAL: FLEET_WEBHOOK_SECRET not set -- refusing to start unsigned (webhook_receiver.py "
            "would accept nothing, which is safe, but that's not the same as a useful server). "
            "Set it to the same value configured in GitHub's webhook settings.",
            file=sys.stderr,
        )
        return 2
    port = 8562
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    log(f"listening on :{port}, watching {WATCHED_WORKFLOWS}")
    # 0.0.0.0, not 127.0.0.1: inside a container, podman's port-forward connects from OUTSIDE
    # the container's own loopback -- a receiver bound to 127.0.0.1 is unreachable through
    # `-p PORT:PORT` even though `podman exec ... curl localhost` finds it fine. Same reasoning
    # as fleet_view_server.py's own bind (see that file). Found live: this exact bug produced a
    # 502 through the Cloudflare Tunnel while the receiver tested healthy from inside its own
    # container namespace the whole time.
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
