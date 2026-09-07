#!/usr/bin/env python3
"""inbox.py -- Reif replies to the brief; the fleet takes it in (fk#669).

Reif, 2026-09-07: "how do I steer the fleet? idea here is that I can just respond to the email
and it will take those updates in."

The path: every brief is sent with Reply-To fleet@reply.philanthropy.org (a Resend receiving
subdomain). Resend posts an `email.received` webhook to /webhook/inbox on the fleet's
webhook receiver; the receiver verifies the Svix signature, checks the sender is one of
FLEET_INBOX_FROM, fetches the full message from Resend, appends it to
$FLEET_LOG_DIR/inbox.jsonl, and kicks `run_member.sh dont-shoot-the-messenger --task inbox`.
That pass reads the pending replies here, answers the asks they name, turns everything else
into a steering issue the fleet acts on, and emails back what it did.

This module is the deterministic half: signature check, sender allowlist, fetch, store, the
"yes 12 / no 12: why / 12: text" parser, and the pending/done ledger. No model here.

  inbox.py pending            -> JSON list of replies not yet processed
  inbox.py done <id>          -> mark one processed
  inbox.py parse <file>       -> JSON: ask answers + free text found in a reply body (for tests)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html as html_mod
import json
import os
import pathlib
import re
import sys
import time
import urllib.request

LOG_DIR = pathlib.Path(os.environ.get("FLEET_LOG_DIR") or os.path.expanduser("~/Library/Logs/fleet-kit"))
INBOX = LOG_DIR / "inbox.jsonl"
DONE = LOG_DIR / "inbox.done"
ALERT_ENV = pathlib.Path(os.environ.get("FLEET_ALERT_ENV") or "/home/ubuntu/.config/maxx/alert.env")
RESEND_API = os.environ.get("RESEND_API_URL_BASE", "https://api.resend.com")
TOLERANCE_S = 300


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "inbox.log", "a") as fh:
        fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}] {msg}\n")


# ---------------------------------------------------------------- webhook side

def verify_svix(body: bytes, headers: dict, secret: str, now: float | None = None) -> bool:
    """Standard Svix check (Resend's webhooks): HMAC-SHA256 over "<id>.<timestamp>.<body>"
    keyed by the base64 part of the whsec_ secret; the signature header carries one or more
    "v1,<base64>" entries; the timestamp must be within TOLERANCE_S of now."""
    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sig = h.get("svix-id", ""), h.get("svix-timestamp", ""), h.get("svix-signature", "")
    if not (msg_id and ts and sig and secret):
        return False
    try:
        if abs((now if now is not None else time.time()) - int(ts)) > TOLERANCE_S:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except (ValueError, IndexError):
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    for part in sig.split():
        if "," in part and hmac.compare_digest(part.split(",", 1)[1], expected):
            return True
    return False


def allowed_sender(addr: str, allow: str) -> bool:
    """FLEET_INBOX_FROM is a comma list of addresses; match the bare address inside
    'Name <addr>' too, case-insensitively."""
    m = re.search(r"<([^>]+)>", addr or "")
    bare = (m.group(1) if m else (addr or "")).strip().lower()
    return bare in {a.strip().lower() for a in (allow or "").split(",") if a.strip()}


def resend_key() -> str:
    if os.environ.get("RESEND_API_KEY"):
        return os.environ["RESEND_API_KEY"]
    if ALERT_ENV.exists():
        for line in ALERT_ENV.read_text().splitlines():
            m = re.match(r"^\s*(?:export\s+)?RESEND_API_KEY\s*=\s*(.*?)\s*$", line)
            if m:
                return m.group(1).strip("\"'")
    return ""


def fetch_received(email_id: str) -> dict:
    req = urllib.request.Request(f"{RESEND_API}/emails/receiving/{email_id}",
                                 headers={"Authorization": f"Bearer {resend_key()}",
                                          "User-Agent": "fleet-kit-inbox/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def strip_quotes(text: str) -> str:
    """Drop the quoted brief under the reply: everything from the first quote marker or
    'On ... wrote:' line on. Keeps what Reif typed."""
    out = []
    for line in (text or "").splitlines():
        if line.startswith(">") or re.match(r"^On .+ wrote:\s*$", line) or line.strip() in ("-- ", "--"):
            break
        if re.match(r"^\s*(From|Sent|To|Subject):\s", line) and out:
            break
        out.append(line)
    return "\n".join(out).strip()


def html_to_text(h: str) -> str:
    t = re.sub(r"<(br|/p|/div|/tr)[^>]*>", "\n", h or "", flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return html_mod.unescape(t)


def store(email: dict, event: dict) -> dict:
    text = email.get("text") or html_to_text(email.get("html") or "")
    row = {
        "id": email.get("id") or event.get("email_id"),
        "received_at": time.time(),
        "from": email.get("from") or event.get("from"),
        "subject": email.get("subject") or event.get("subject"),
        "text": strip_quotes(text),
        "full_text": text[:20000],
        "in_reply_to": (email.get("headers") or {}).get("in-reply-to") or (email.get("headers") or {}).get("In-Reply-To"),
        "message_id": email.get("message_id"),
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(INBOX, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    log(f"stored reply {row['id']} from {row['from']} subject={row['subject']!r} chars={len(row['text'])}")
    return row


# ---------------------------------------------------------------- pass side

ANSWER_RE = re.compile(r"^\s*(?:(yes|no|ok|approve[d]?|go|do it)\s+#?(\d+)\s*[:\-–]?\s*(.*)|#?(\d+)\s*[:\-–]\s*(.+))\s*$", re.I)


def parse_reply(text: str) -> dict:
    """Lines like 'yes 12', 'no 12: too expensive', '12: send it Tuesday' answer ask 12.
    Everything else is free text for the messenger to turn into steering."""
    answers, free = [], []
    for line in strip_quotes(text or "").splitlines():
        m = ANSWER_RE.match(line)
        if m and (m.group(2) or m.group(4)):
            if m.group(2):
                word = m.group(1).lower()
                verdict = "no" if word == "no" else "yes"
                answers.append({"ask_id": int(m.group(2)), "answer": (verdict + (": " + m.group(3).strip() if m.group(3).strip() else ""))})
            else:
                answers.append({"ask_id": int(m.group(4)), "answer": m.group(5).strip()})
        elif line.strip():
            free.append(line.rstrip())
    return {"answers": answers, "free_text": "\n".join(free).strip()}


def pending() -> list[dict]:
    done = set(DONE.read_text().split()) if DONE.exists() else set()
    rows = []
    if INBOX.exists():
        for line in INBOX.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("id") and r["id"] not in done:
                r["parsed"] = parse_reply(r.get("text") or "")
                rows.append(r)
    return rows


def mark_done(email_id: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(DONE, "a") as fh:
        fh.write(email_id + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending")
    d = sub.add_parser("done"); d.add_argument("id")
    p = sub.add_parser("parse"); p.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "pending":
        json.dump(pending(), sys.stdout, indent=1); print(); return 0
    if a.cmd == "done":
        mark_done(a.id); print("done"); return 0
    json.dump(parse_reply(pathlib.Path(a.file).read_text()), sys.stdout, indent=1); print(); return 0


if __name__ == "__main__":
    sys.exit(main())
