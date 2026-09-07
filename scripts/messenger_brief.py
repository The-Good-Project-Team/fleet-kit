#!/usr/bin/env python3
"""messenger_brief.py -- the one voice to the human: collect what happened, send the brief.

Reif, 2026-09-06: "I'd like to wake up to a PDF of reading to do based on what happened that
night, and then one project for the day." And: "appropriate to have a scout or something that
is just the one reporting and sending those tasks to me, send via email too." fk#558, deliverable
8. The messenger member (dont-shoot-the-messenger) is the only member that emails Reif; this
script is the I/O half of it -- deterministic collection and delivery -- so the charter's model
only writes the words.

Two subcommands, same split as ask.py / cost_bridge.py (script does I/O, the pass does judgment):

  collect --since-hours N        -> JSON on stdout: the number header, PRs merged in the window
                                    (kit + product repo), open PRs, open asks, run outcomes by
                                    member/status, deploys, and the plan file's bets.
  send --kind K --md FILE [--pdf] -> renders the markdown brief to HTML (and a PDF via the image's
                                    playwright chromium when --pdf) and emails it through Resend.
                                    K is morning|afternoon|wrap|ask. One send per kind per day:
                                    a second call the same day is a logged no-op (state file), so
                                    a retried pass never double-mails.

Credentials: RESEND_API_KEY / MAIL_FROM / FLEET_ALERT_EMAIL, read from FLEET_ALERT_ENV (default
/home/ubuntu/.config/maxx/alert.env -- the same file fleet_alert.sh reads; deploy.sh mounts it
read-only into the container). RESEND_API_URL overrides the endpoint (tests point it at a local
fake). Nothing here ever prints a key.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

KIT = pathlib.Path(__file__).resolve().parent.parent
LOG_DIR = pathlib.Path(os.environ.get("FLEET_LOG_DIR") or os.path.expanduser("~/Library/Logs/fleet-kit"))
ALERT_ENV = pathlib.Path(os.environ.get("FLEET_ALERT_ENV") or "/home/ubuntu/.config/maxx/alert.env")
RESEND_URL = os.environ.get("RESEND_API_URL", "https://api.resend.com/emails")
KINDS = ("morning", "afternoon", "wrap", "ask")
CENTRAL = dt.timezone(dt.timedelta(hours=-5))  # CDT; the crontab is in UTC, see entrypoint.sh


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(LOG_DIR / "messenger.log", "a") as fh:
        fh.write(f"[{stamp}] {msg}\n")


def sh(cmd: list[str], timeout: int = 60, cwd: str | None = None) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd).stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"collect: {cmd[0]} failed: {exc}")
        return ""


# ---------------------------------------------------------------- collect

def repo_slugs() -> list[str]:
    slugs = ["The-Good-Project-Team/fleet-kit"]
    url = os.environ.get("FLEET_REPO_URL", "")
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    if m and m.group(1) not in slugs:
        slugs.append(m.group(1))
    return slugs


def gh_prs(slug: str, state: str, since: str | None, limit: int = 60) -> list[dict]:
    cmd = ["gh", "pr", "list", "--repo", slug, "--state", state, "--limit", str(limit),
           "--json", "number,title,author,mergedAt,url,additions,deletions"]
    if since:
        cmd += ["--search", f"merged:>={since}"]
    out = sh(cmd, timeout=90)
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError:
        return []
    return [{"repo": slug, "number": r.get("number"), "title": r.get("title"), "url": r.get("url"),
             "author": (r.get("author") or {}).get("login"), "merged_at": r.get("mergedAt"),
             "lines": (r.get("additions") or 0) + (r.get("deletions") or 0)} for r in rows]


def runs_since(since_ts: float) -> dict:
    path = LOG_DIR / "runs.jsonl"
    by: dict[str, dict[str, int]] = {}
    notable: list[dict] = []
    if not path.exists():
        return {"by_member": by, "notable": notable}
    with open(path, errors="ignore") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(r.get("ts") or 0) < since_ts:
                continue
            m, s = str(r.get("member")), str(r.get("status"))
            by.setdefault(m, {}).setdefault(s, 0)
            by[m][s] += 1
            if s in ("killed", "timed_out", "budget_declined") or (r.get("outcome") and m in ("gru", "jefe", "dumbledore", "datta")):
                notable.append({"member": m, "status": s, "outcome": (r.get("outcome") or "")[:200],
                                "item_id": r.get("item_id")})
    return {"by_member": by, "notable": notable[-40:]}


def deploys_since(since: dt.datetime) -> list[str]:
    out = []
    for name in ("deploy.log",):
        p = LOG_DIR / name
        if not p.exists():
            continue
        for line in p.read_text(errors="ignore").splitlines()[-400:]:
            m = re.match(r"\[deploy (\S+ \S+) \S+\] (DEPLOYED:|ROLLED BACK|FAILED)(.*)", line)
            if not m:
                continue
            try:
                when = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CENTRAL)
            except ValueError:
                continue
            if when >= since:
                out.append(line[:200])
    return out[-20:]


def asks_open() -> list[dict]:
    out = sh([sys.executable, str(KIT / "scripts" / "ask.py"), "list"], timeout=60)
    try:
        return json.loads(out or "[]")
    except json.JSONDecodeError:
        return []


def number_header() -> str:
    return sh([sys.executable, str(KIT / "scripts" / "number_read.py"), "--render"], timeout=60).strip()


def plan_bets() -> str:
    repo = os.environ.get("FLEET_REPO", "/repo")
    for cand in (pathlib.Path(repo) / "docs" / "plan" / "philanthropy.md",):
        if cand.exists():
            text = cand.read_text(errors="ignore")
            i = text.find("## Bets")
            return text[i:i + 6000] if i != -1 else text[:6000]
    return ""


def collect(since_hours: float) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=since_hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    merged, open_prs = [], []
    for slug in repo_slugs():
        merged += gh_prs(slug, "merged", since_iso)
        open_prs += gh_prs(slug, "open", None, limit=40)
    return {
        "generated_at_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "generated_at_central": now.astimezone(CENTRAL).strftime("%Y-%m-%d %H:%M CT"),
        "window_hours": since_hours,
        "number": number_header(),
        "merged": merged,
        "open_prs": open_prs,
        "asks": asks_open(),
        "runs": runs_since(since.timestamp()),
        "deploys": deploys_since(since.astimezone(CENTRAL)),
        "plan_bets": plan_bets(),
    }


# ---------------------------------------------------------------- render + send

def md_to_html(md: str) -> str:
    """Tiny markdown: #/##/### headings, - bullets, **bold**, `code`, [text](url), blank-line
    paragraphs, | tables |. Enough for a brief; no dependency to ship."""
    def inline(s: str) -> str:
        s = html.escape(s, quote=False)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', s)
        return s

    out, para, in_list, in_table = [], [], False, False

    def flush_para():
        nonlocal para
        if para:
            out.append("<p>" + " ".join(inline(x) for x in para) + "</p>")
            para = []

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>"); in_list = False

    def close_table():
        nonlocal in_table
        if in_table:
            out.append("</table>"); in_table = False

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_para(); close_list(); close_table(); continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            flush_para(); close_list(); close_table()
            out.append(f"<h{len(m.group(1))}>{inline(m.group(2))}</h{len(m.group(1))}>"); continue
        if line.lstrip().startswith("- ") or line.lstrip().startswith("* "):
            flush_para(); close_table()
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{inline(line.lstrip()[2:])}</li>"); continue
        if line.startswith("|"):
            flush_para(); close_list()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if not in_table:
                out.append("<table>"); in_table = True
            out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>"); continue
        close_list(); close_table()
        para.append(line.strip())
    flush_para(); close_list(); close_table()
    return "\n".join(out)


STYLE = """<style>
body{font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1a1a;max-width:720px;margin:24px auto;padding:0 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:22px 0 6px;border-bottom:1px solid #ddd;padding-bottom:3px}h3{font-size:15px;margin:14px 0 4px}
table{border-collapse:collapse;margin:8px 0}td{border:1px solid #ddd;padding:3px 8px;font-size:13px}
code{background:#f3f3f3;padding:1px 4px;border-radius:3px;font-size:13px}ul{padding-left:20px}a{color:#0b57d0}
.meta{color:#666;font-size:12px}
</style>"""


def render_html(md: str, kind: str, stamp: str) -> str:
    body = md_to_html(md)
    return f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}</head><body>{body}<p class='meta'>fleet messenger &middot; {kind} &middot; {stamp}</p></body></html>"


def html_to_pdf(html_text: str, path: pathlib.Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log("pdf: playwright not importable, sending without attachment")
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.set_content(html_text, wait_until="load")
            pg.pdf(path=str(path), format="Letter", margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"})
            b.close()
        return path.exists() and path.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001 -- best effort, the email still goes
        log(f"pdf: render failed: {exc}")
        return False


def read_alert_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in ("RESEND_API_KEY", "MAIL_FROM", "FLEET_ALERT_EMAIL") if os.environ.get(k)}
    if ALERT_ENV.exists():
        for line in ALERT_ENV.read_text().splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*$", line)
            if m and m.group(1) not in env:
                env[m.group(1)] = m.group(2).strip("\"'")
    return env


def subject_for(kind: str, md: str, when: dt.datetime) -> str:
    first = next((l.lstrip("# ").strip() for l in md.splitlines() if l.startswith("#")), "")
    day = when.astimezone(CENTRAL).strftime("%a %b %-d")
    label = {"morning": "Morning brief", "afternoon": "Afternoon block", "wrap": "Wrap", "ask": "Fleet ask"}[kind]
    return f"{label} · {day}" + (f" · {first}" if first and kind != "ask" else "")


def send(kind: str, md_path: pathlib.Path, want_pdf: bool, force: bool) -> int:
    if kind not in KINDS:
        print(f"kind must be one of {KINDS}", file=sys.stderr); return 2
    md = md_path.read_text()
    if not md.strip():
        log(f"send {kind}: brief file is empty, refusing to send a blank email"); return 2
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(CENTRAL).strftime("%Y-%m-%d")
    state_path = LOG_DIR / "messenger_sent.json"
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
    except json.JSONDecodeError:
        state = {}
    if kind != "ask" and state.get(kind) == today and not force:
        log(f"send {kind}: already sent today ({today}) -- no-op (pass --force to resend)")
        print("already-sent"); return 0

    creds = read_alert_env()
    missing = [k for k in ("RESEND_API_KEY", "MAIL_FROM", "FLEET_ALERT_EMAIL") if not creds.get(k)]
    if missing:
        log(f"send {kind}: missing {missing} (looked in env and {ALERT_ENV}) -- not sent"); print("no-credentials"); return 1

    html_text = render_html(md, kind, now.astimezone(CENTRAL).strftime("%Y-%m-%d %H:%M CT"))
    payload: dict = {
        "from": creds["MAIL_FROM"],
        "to": [a.strip() for a in creds["FLEET_ALERT_EMAIL"].split(",") if a.strip()],
        "subject": subject_for(kind, md, now),
        "html": html_text,
        "text": md,
    }
    if want_pdf:
        pdf = LOG_DIR / f"brief-{kind}-{today}.pdf"
        if html_to_pdf(html_text, pdf):
            payload["attachments"] = [{"filename": pdf.name, "content": base64.b64encode(pdf.read_bytes()).decode()}]
    req = urllib.request.Request(RESEND_URL, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {creds['RESEND_API_KEY']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = resp.status
            body = resp.read(300).decode(errors="ignore")
    except urllib.error.HTTPError as exc:
        code, body = exc.code, exc.read(300).decode(errors="ignore")
    except (urllib.error.URLError, OSError) as exc:
        log(f"send {kind}: transport error {exc} -- not sent"); print("transport-error"); return 1
    if code >= 300:
        log(f"send {kind}: Resend HTTP {code}: {body}"); print(f"http-{code}"); return 1
    state[kind] = today
    state_path.write_text(json.dumps(state))
    log(f"send {kind}: delivered to {len(payload['to'])} recipient(s), subject={payload['subject']!r}, pdf={'attachments' in payload}")
    print("sent"); return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect"); c.add_argument("--since-hours", type=float, default=14)
    s = sub.add_parser("send"); s.add_argument("--kind", required=True); s.add_argument("--md", required=True)
    s.add_argument("--pdf", action="store_true"); s.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        json.dump(collect(a.since_hours), sys.stdout, indent=1); print(); return 0
    return send(a.kind, pathlib.Path(a.md), a.pdf, a.force)


if __name__ == "__main__":
    sys.exit(main())
