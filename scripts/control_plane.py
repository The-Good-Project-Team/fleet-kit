#!/usr/bin/env python3
"""control_plane.py -- the one place that says which instances exist and how much of the
shared account pool each one gets (fleet-kit#759, first slice).

Reif, 2026-09-08: "Seems we need a central control -- for creating new instances, managing
instances, and splitting our resources across instances more smartly."

THE SHAPE (control plane vs data plane, the Kubernetes/Nomad split): the containers are the
data plane -- each one runs its members, reads its own fleet.env every cron tick, publishes
its own share into the shared shares/ dir. This script is the control plane: it runs on the
HOST (outside every container, from the host crontab), reads one registry of instances, and
WRITES each instance's FLEET_SHARE_FRACTION into its fleet.env. Nothing inside a container
changes: run_member.sh already re-sources fleet.env on every tick (run_member.sh line 37), so a
share written here is live on the very next member run, and check_share_sum.sh / publish_share.sh
keep working exactly as before because the number still lives where they read it.

WHY WEIGHTS, NOT "PROPORTIONAL TO THE NUMBER'S 7-DAY MOVEMENT": measured 2026-09-09, both
instances read delta_7d 0.0 (philanthropy: Stripe MRR $10.83, no movement; the fleet-kit fleet
has no number at all). A movement rule with that data collapses to "everyone gets the floor".
Weights are the honest v1: one integer per instance in one file, and the index page shows the
starvation signal (budget_declined last hour) that tells Reif when to move one. The movement
rule can replace the weights once two numbers actually move.

WHY A HUMAN RESERVE: the instances share maxx accounts with Reif's own Claude Code sessions.
Before this script the two instances were hand-set to 0.60 + 0.20 = 0.80, leaving 0.20 for
humans. `human_reserve` keeps that slack explicit; the instances split what is left.

WHAT FLOWS: an instance with FLEET_ENABLED=false in its fleet.env (the console's on/off
button, or `control_plane.py pause NAME`) gets share 0.0 and the others absorb its weight on
the next tick. A weight of 0 does the same without turning the instance off.

Registry (JSON, default ~/fleet-kit/instances/registry.json, gitignored with the rest of
instances/):

    {"human_reserve": 0.2,
     "instances": [
       {"name": "philanthropy", "dir": "/home/ubuntu/fleet-kit/instances/nonprofit-atlas",
        "path": "/fleet/philanthropy", "weight": 3},
       {"name": "fleet-kit", "dir": "/home/ubuntu/fleet-kit-server-fleet",
        "container": "fleet-kit-server-fleet", "path": "/fleet/fleet-kit", "weight": 1}]}

`dir` holds fleet.env, repo/ and logs/ (the same layout deploy.sh mounts). `container`
defaults to `name`; `path` defaults to /fleet/<name>.

Usage (host):
    control_plane.py tick [--dry-run]     compute shares, write changed ones, render the index
    control_plane.py serve [--port 8600]  the page, with pause/resume/weight buttons (caddy
                                          proxies /fleet/* here; systemd user unit on dino)
    control_plane.py pause NAME           FLEET_ENABLED=false in that fleet.env, then tick
    control_plane.py resume NAME          FLEET_ENABLED=true, then tick
    control_plane.py weight NAME N        set the weight in the registry, then tick
    control_plane.py new NAME REPO_URL [--weight N] [--no-deploy]
                                          create instances/NAME (paused, share 0) and deploy it
    control_plane.py remove NAME          containers, routes, cron, registry gone; files kept
    control_plane.py token                print the path of the bearer token /new and /remove need
    control_plane.py secrets init         build ~/.config/fleet-kit/secrets.env from what the
                                          instances carry; put the include line in every fleet.env
    control_plane.py secrets check        per instance: include line, container mount, own copies
    control_plane.py secrets adopt NAME   drop NAME's own copies that equal the store (after redeploy)
    control_plane.py secrets set KEY < v  rotate one shared key; every instance reads it next tick

Shared secret store: one file on the host (0600), bind-mounted read-only into every container at
the same path, pulled in by the first line of each fleet.env. Shared keys: the Claude OAuth
tokens, the maxx keys and handles, GH_TOKEN, NTFY_TOPIC. A new instance never copies them.

Agent API (same server): POST /fleet/new  (JSON or form: name, repo_url, w) with
Authorization: Bearer <token>  -> 202 {job, status}; GET /fleet/new/NAME -> the steps so far;
POST /fleet/remove name=NAME -> 200. Everything else on the page needs no token, like the
per-instance consoles' own on/off buttons.

The page (every instance: number, share, weight, live build, runs last hour, last brief, and
the buttons) is served live by `serve` at /fleet/; `tick` also writes a static copy to
$FLEET_CONTROL_PLANE_OUT/index.html (default ~/.cache/fleet-kit/control_plane) so the numbers
can still be read when the server is down.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
REGISTRY = Path(os.environ.get("FLEET_INSTANCES_REGISTRY",
                               Path.home() / "fleet-kit" / "instances" / "registry.json"))
OUT_DIR = Path(os.environ.get("FLEET_CONTROL_PLANE_OUT",
                              Path.home() / ".cache" / "fleet-kit" / "control_plane"))
SHARE_KEY = "FLEET_SHARE_FRACTION"
ENABLED_KEY = "FLEET_ENABLED"
CADDYFILE = Path(os.environ.get("FLEET_CADDYFILE", Path.home() / "Caddyfile"))
TOKEN_FILE = Path(os.environ.get("FLEET_CONTROL_PLANE_TOKEN_FILE",
                                 Path.home() / ".config" / "fleet-kit" / "control_plane.token"))
PUBLIC_BASE = os.environ.get("FLEET_PUBLIC_BASE", "https://dino.luckymachines.co")
SECRETS_FILE = Path(os.environ.get("FLEET_SECRETS_FILE", Path.home() / ".config" / "fleet-kit" / "secrets.env"))
# Keys every instance shares because the ACCOUNTS are shared: the Claude OAuth tokens, the maxx
# meter keys/handles, the GitHub token, the pager topic. Per-instance secrets (the console's own
# FLEET_API_KEY, a product's number token or inbox secret) are deliberately not here.
SECRET_KEY_RE = re.compile(r"^(CLAUDE_CODE_OAUTH_TOKEN|FLEET_MAXX_KEY|FLEET_MAXX_HANDLE|GH_TOKEN|NTFY_TOPIC)(_[A-Z0-9_]+)?$")
# The canonical host checkout, not this file's parent: cron lines and the deploy lock must
# point at the checkout auto_deploy.sh pulls into, even when this script runs from a worktree.
KIT_DIR = Path(os.environ.get("FLEET_KIT_DIR", Path.home() / "fleet-kit"))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PORT_KEYS = ("FLEET_VIEW_PORT", "FLEET_WEBHOOK_PORT", "FLEET_GREEN_VIEW_PORT", "FLEET_GREEN_WEBHOOK_PORT")
# Keys that belong to ONE product (its number endpoint, its inbox, its prod pager), never to
# be copied from the template instance into a new one. They start blank; the venture fills them.
PRODUCT_KEYS = ("FLEET_NUMBER_URL", "FLEET_NUMBER_TOKEN", "FLEET_INBOX_FROM", "FLEET_REPLY_TO",
                "FLEET_INBOX_WEBHOOK_SECRET", "FLEET_DEPLOY_DRIVER", "FLEET_REQUIRED_CHECKS",
                "FIXER_HEALTH_URL", "FIXER_PAGE_URL", "PROD_DIAG_DRIVER")


# ---------------------------------------------------------------- registry + fleet.env
def load_registry(path: Path = REGISTRY) -> dict:
    reg = json.loads(path.read_text())
    reg.setdefault("human_reserve", 0.0)
    for inst in reg["instances"]:
        inst.setdefault("container", inst["name"])
        inst.setdefault("path", f"/fleet/{inst['name']}")
        inst.setdefault("weight", 1)
    return reg


def save_registry(reg: dict, path: Path = REGISTRY) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=1) + "\n")
    tmp.replace(path)


def read_env(env_file: Path) -> dict:
    """KEY=value lines only; quotes stripped; comments and blanks skipped. Never sourced."""
    out = {}
    if not env_file.exists():
        return out
    for line in env_file.read_text(errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env_field(env_file: Path, key: str, value: str) -> bool:
    """Set KEY=value in fleet.env, preserving every other line (same algorithm as
    fleet_view_server.write_env_field). fleet.env is a FILE bind-mount into the container, so
    this rewrites the file in place -- never a rename, which would leave the container holding
    the old inode. Returns True when the file changed."""
    text = env_file.read_text(errors="ignore") if env_file.exists() else ""
    lines = text.splitlines()
    found = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(f"{key}=") or s.startswith(f"{key} ="):
            if s == f"{key}={value}":
                return False
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    with env_file.open("w") as fh:
        fh.write("\n".join(lines) + "\n")
    return True


def is_enabled(env: dict) -> bool:
    return env.get(ENABLED_KEY, "true").lower() != "false"


# ---------------------------------------------------------------- the rule
def compute_shares(instances: list[dict], enabled: dict, human_reserve: float) -> dict:
    """name -> share. Enabled instances with weight > 0 split (1 - human_reserve) by weight;
    everyone else gets 0.0. Shares always sum to <= 1 - human_reserve, so check_share_sum.sh
    can never see an oversubscribed hour from this script."""
    pool = min(max(1.0 - float(human_reserve), 0.0), 1.0)
    active = [i for i in instances if enabled.get(i["name"], True) and float(i.get("weight", 1)) > 0]
    total = sum(float(i.get("weight", 1)) for i in active)
    shares = {i["name"]: 0.0 for i in instances}
    for i in active:
        shares[i["name"]] = round(pool * float(i["weight"]) / total, 4)
    return shares


# ---------------------------------------------------------------- what the page shows
def runs_last_hour(logs: Path, now: float | None = None) -> dict:
    """Completed rows from runs.jsonl in the last hour, bucketed by status. The
    budget_declined bucket is the starvation signal: a member that woke up and was told no."""
    now = now or time.time()
    out = {"ok": 0, "budget_declined": 0, "other": 0, "last_brief": None}
    f = logs / "runs.jsonl"
    if not f.exists():
        return out
    for line in f.read_text(errors="ignore").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("status") == "started" or "exit_code" not in row:
            continue
        if row.get("member") == "dont-shoot-the-messenger" and row.get("status") == "ok" \
                and str(row.get("outcome") or "").startswith("sent"):
            out["last_brief"] = max(out["last_brief"] or 0, float(row.get("ts", 0)))
        if float(row.get("ts", 0)) < now - 3600:
            continue
        st = row.get("status")
        out[st if st in ("ok", "budget_declined") else "other"] += 1
    return out


def read_number(logs: Path) -> dict | None:
    f = logs / "number.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text())
        n = d.get("number") or {}
        return {"name": n.get("name"), "value": n.get("value"), "unit": n.get("unit"),
                "delta_7d": n.get("delta_7d"), "as_of": d.get("as_of")}
    except (ValueError, AttributeError):
        return None


def repo_head(d: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(d / "repo"), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


def container_status() -> dict:
    try:
        # A literal tab in a Go template comes out as column padding, not a tab; use a
        # separator podman never pads.
        p = subprocess.run(["podman", "ps", "--format", "{{.Names}}|{{.Status}}"],
                           capture_output=True, text=True, timeout=20)
        return dict(l.split("|", 1) for l in p.stdout.splitlines() if "|" in l)
    except (OSError, subprocess.SubprocessError):
        return {}


def _central(ts: float | None) -> str:
    if not ts:
        return "never"
    return _dt.datetime.fromtimestamp(ts, CENTRAL).strftime("%Y-%m-%d %H:%M %Z")


def _secrets_cell(st: dict | None) -> str:
    if st is None:
        return "no store"
    if not st["include"]:
        return "<span class=warn>no include line</span>"
    if st["mount"] is False:
        return "<span class=warn>container lacks mount</span>"
    if st["conflicts"]:
        return f"<span class=warn>overrides {len(st['conflicts'])}</span>"
    if st["own"]:
        return f"own copies {len(st['own'])}"
    return "store"


def render_index(rows: list[dict], human_reserve: float, generated: float) -> str:
    """One page, every instance. Static HTML on purpose: the control plane must not need a
    server up to be read (same law as fleet_enabled.sh's file flag)."""
    total = sum(r["share"] for r in rows)
    trs = []
    for r in rows:
        num = r["number"]
        if not num:
            num_txt = "no number configured"
        else:
            num_txt = f"{html.escape(str(num['name']))}: {num['value']} {html.escape(str(num['unit'] or ''))}"
            if isinstance(num.get("delta_7d"), (int, float)):
                num_txt += f" (7d {num['delta_7d']:+g})"
        state = "paused" if not r["enabled"] else ("up" if r["container"].startswith("Up") else "DOWN")
        runs = r["runs"]
        name = html.escape(r["name"])
        # Relative form actions on purpose: caddy mounts this page under /fleet/ and strips the
        # prefix, so "pause" resolves to /fleet/pause in the browser and to /pause here.
        toggle = "resume" if not r["enabled"] else "pause"
        controls = (f"<form method=post action='{toggle}'><input type=hidden name=name value='{name}'>"
                    f"<button>{toggle}</button></form>"
                    f"<form method=post action='weight'><input type=hidden name=name value='{name}'>"
                    f"<input name=w type=number min=0 step=1 value='{r['weight']:g}' size=3> "
                    f"<button>set weight</button></form>")
        trs.append(
            f"<tr class='{state}'><td><a href='{html.escape(r['path'])}/'>{name}</a>"
            f"<div class=sub>{html.escape(r['container_name'])} · {html.escape(r['container'])}</div></td>"
            f"<td>{state}</td><td>{num_txt}</td>"
            f"<td><b>{r['share']:.0%}</b><div class=sub>weight {r['weight']:g}</div></td>"
            f"<td>{runs['ok']} ok · <span class='{'warn' if runs['budget_declined'] else ''}'>"
            f"{runs['budget_declined']} budget-declined</span></td>"
            f"<td><code>{html.escape(r['head'])}</code></td><td>{_central(runs['last_brief'])}</td>"
            f"<td>{_secrets_cell(r.get('secrets'))}</td>"
            f"<td class=ctl>{controls}</td></tr>")
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>fleet control plane</title>
<style>body{{font:15px/1.4 system-ui,sans-serif;margin:2rem;max-width:72rem;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:.6rem .7rem;border-bottom:1px solid #ddd;vertical-align:top}}
th{{font-size:.8rem;text-transform:uppercase;color:#666}}.sub{{font-size:.8rem;color:#777}}
tr.paused td{{color:#999}}tr.DOWN td:nth-child(2){{color:#b00;font-weight:600}}.warn{{color:#b00;font-weight:600}}
p.meta{{color:#666;font-size:.9rem}}.wrap{{overflow-x:auto}}td.ctl form{{display:inline-block;margin:0 .4rem .3rem 0}}
form.new input{{font:inherit;padding:.3rem;margin:0 .3rem .3rem 0}}h2{{font-size:1.1rem;margin-top:2rem}}
button{{font:inherit;padding:.2rem .6rem}}input[type=number]{{width:3.5rem;font:inherit}}</style>
<h1>Control plane</h1>
<p class=meta>{len(rows)} instances · shares sum {total:.0%} · human reserve {human_reserve:.0%} ·
written {_central(generated)} · a paused instance starts no new pass on its next cron tick and its
share flows to the rest; weights split what the human reserve leaves</p>
<div class=wrap><table><tr><th>instance</th><th>state</th><th>number</th><th>share this hour</th><th>runs last hour</th><th>live build</th><th>last brief</th><th>secrets</th><th></th></tr>
{''.join(trs)}</table></div>
<h2>New instance</h2>
<form method=post action='new' class=new>
<input name=name placeholder='name (a-z, 0-9, -)' pattern='[a-z0-9][a-z0-9-]+' required>
<input name=repo_url placeholder='https://github.com/org/repo.git' size=40 required>
<input name=w type=number min=0 step=1 value=1 title=weight>
<input name=token type=password placeholder='control plane token' required>
<button>create</button></form>
<p class=meta>Creates instances/&lt;name&gt; from the template instance's fleet.env, clones the repo,
routes /fleet/&lt;name&gt;/, adds the host cron lines, deploys, and registers it <b>paused</b> with
share 0. Resume it here when you want it spending. Agents: <code>POST /fleet/new</code> with
<code>Authorization: Bearer &lt;token&gt;</code> (token file on the host: <code>control_plane.py token</code>),
JSON or form <code>name, repo_url, w</code>; answers 202 and <code>GET /fleet/new/&lt;name&gt;</code>
reports each step. <code>POST /fleet/remove name=</code> undoes it (files kept).</p>"""



# ---------------------------------------------------------------- new instance (AC1)
def render_new_env(template_text: str, name: str, repo_url: str, ports: tuple, public_base: str) -> str:
    """A new instance's fleet.env: the template instance's file (same accounts, same maxx keys,
    same dials -- the pool is shared, so the credentials are the same by design) with every
    per-instance and per-product key replaced. Starts PAUSED with share 0: an agent may create
    an instance, but the first pass that spends money waits for a resume."""
    overrides = {
        "FLEET_CONTAINER_NAME": name, "FLEET_REPO_URL": repo_url,
        "FLEET_VIEW_PORT": str(ports[0]), "FLEET_WEBHOOK_PORT": str(ports[1]),
        "FLEET_GREEN_VIEW_PORT": str(ports[2]), "FLEET_GREEN_WEBHOOK_PORT": str(ports[3]),
        ENABLED_KEY: "false", SHARE_KEY: "0",
        "PUBLIC_URL": public_base.rstrip("/") + "/", "PUBLIC_PATH_URL": f"{public_base.rstrip('/')}/fleet/{name}",
    }
    for k in PRODUCT_KEYS:
        overrides[k] = ""
    kept = [include_line()]
    for line in template_text.splitlines():
        s = line.strip()
        if s.startswith("[ -f") and str(SECRETS_FILE) in s:
            continue  # the template's own include line; ours is first already
        key = s.split("=", 1)[0].strip() if "=" in s and not s.startswith("#") else None
        if key in overrides or (key and SECRET_KEY_RE.match(key)):
            continue  # shared secrets come from the store, never copied
        kept.append(line)
    kept.append("")
    kept.append(f"# --- set by control_plane.py new ({name}); the per-instance and per-product keys")
    kept.extend(f"{k}={v}" for k, v in overrides.items())
    return "\n".join(kept) + "\n"


def used_ports(reg: dict) -> set:
    used = set()
    for i in reg["instances"]:
        env = read_env(Path(i["dir"]) / "fleet.env")
        for k in PORT_KEYS:
            if env.get(k, "").isdigit():
                used.add(int(env[k]))
    try:
        out = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True, timeout=10).stdout
        for m in re.finditer(r":(\d+)\s", out):
            used.add(int(m.group(1)))
    except (OSError, subprocess.SubprocessError):
        pass
    return used


def allocate_ports(used: set, start: int = 8601, count: int = 4) -> tuple:
    """First run of `count` consecutive ports above `start` that nothing uses. Consecutive so
    an instance's four ports read as one block in the Caddyfile and `podman ps`."""
    p = start
    while p < 65000:
        block = tuple(range(p, p + count))
        if not any(b in used for b in block):
            return block
        p += 1
    raise ValueError("no free port block")


def _marked(name: str) -> tuple:
    return f"\t# control_plane:{name} begin\n", f"\t# control_plane:{name} end\n"


def caddy_with_instance(text: str, name: str, view_port: int) -> str:
    """Add the two routes deploy.sh's caddy_swap expects (a Referer-matched /api/* upstream
    and the /fleet/<name>* path), each in a marked block so remove can take them out again.
    Idempotent: an instance already present is left alone."""
    begin, end = _marked(name)
    if begin in text:
        return text
    api = (f"{begin}\t@api_{name.replace('-', '_')} {{\n\t\tpath /api/*\n"
           f"\t\theader_regexp Referer ^https?://[^/]+/fleet/{name}\n\t}}\n"
           f"\thandle @api_{name.replace('-', '_')} {{\n\t\treverse_proxy localhost:{view_port}\n\t}}\n{end}")
    route = (f"{begin}\thandle /fleet/{name}* {{\n\t\turi strip_prefix /fleet/{name}\n"
             f"\t\treverse_proxy localhost:{view_port}\n\t}}\n{end}")
    fallback = "\t# fallback for /api/*"
    anchor = "\thandle /webhook*"
    if fallback in text:
        text = text.replace(fallback, api + "\n" + fallback, 1)
    else:
        text = text.replace(anchor, api + "\n" + anchor, 1)
    return text.replace(anchor, route + "\n" + anchor, 1)


def caddy_without_instance(text: str, name: str) -> str:
    begin, end = _marked(name)
    while begin in text:
        a = text.index(begin)
        b = text.index(end, a) + len(end)
        if text[b:b + 1] == "\n":
            b += 1
        text = text[:a] + text[b:]
    return text


def reload_caddy() -> None:
    subprocess.run(["caddy", "reload", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
                   capture_output=True, text=True, timeout=60, check=True)


def cron_with_instance(text: str, name: str, d: Path, ntfy: str = "") -> str:
    """The host crontab lines one instance needs (same three the existing instances carry:
    auto-deploy, member liveness, console path health), each tagged so remove can find them."""
    if f"# control_plane:{name}" in text:
        return text
    kit = KIT_DIR
    logs = Path.home() / "fleet-kit-logs"
    tag = f" # control_plane:{name}"
    lines = [
        f"*/5 * * * * cd {kit} && FLEET_INSTANCE_DIR={d} FLEET_CONTAINER_NAME={name} FLEET_LOG_DIR={logs} "
        f"flock -w 240 {kit}/.git/.auto_deploy.lock bash scripts/auto_deploy.sh >> {logs}/auto_deploy.{name}.cron.log 2>&1{tag}",
        f"*/5 * * * * FLEET_LOG_DIR={d}/logs FLEET_INSTANCE_NAME={name} NTFY_TOPIC={ntfy} "
        f"bash {kit}/scripts/member_liveness_check.sh >> {logs}/member_liveness.{name}.cron.log 2>&1{tag}",
        f"*/5 * * * * PUBLIC_PATH_URL={PUBLIC_BASE}/fleet/{name} NTFY_TOPIC={ntfy} STATE_FILE={d}/logs/.path_health_paged.state "
        f"bash {kit}/scripts/path_health_check.sh >> {logs}/path_health_check.{name}.cron.log 2>&1{tag}",
    ]
    return text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"


def cron_without_instance(text: str, name: str) -> str:
    return "".join(l + "\n" for l in text.splitlines() if not l.rstrip().endswith(f"# control_plane:{name}"))


def read_crontab() -> str:
    p = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    return p.stdout if p.returncode == 0 else ""


def write_crontab(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text, text=True, timeout=10, check=True)


def run_deploy(d: Path, name: str, log: Path) -> None:
    """First deploy of a new instance: deploy.sh under the same lock auto_deploy uses. Blocks
    for the build (minutes); the job thread is what waits, not the HTTP request."""
    env = dict(os.environ, FLEET_INSTANCE_DIR=str(d), FLEET_CONTAINER_NAME=name,
               FLEET_LOG_DIR=str(Path.home() / "fleet-kit-logs"))
    with log.open("a") as fh:
        subprocess.run(["flock", "-w", "600", str(KIT_DIR / ".git" / ".auto_deploy.lock"),
                        "bash", str(KIT_DIR / "scripts" / "deploy.sh")],
                       env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=1800, check=True)


def new_instance(reg: dict, name: str, repo_url: str, weight: float = 1.0,
                 registry: Path = REGISTRY, deploy_fn=run_deploy, caddy: bool = True,
                 cron: bool = True) -> dict:
    """Create instances/<name> (fleet.env from the template instance, a checkout, logs, a
    webhook secret), four free ports, the caddy routes, the host cron lines, the registry
    entry, then the first deploy. Status is written to <dir>/new.json after every step so
    GET /new/<name> can show an agent where it is. Raises ValueError on a bad request before
    touching anything."""
    if not NAME_RE.match(name or ""):
        raise ValueError("name must match ^[a-z0-9][a-z0-9-]{0,31}$")
    if any(i["name"] == name for i in reg["instances"]):
        raise ValueError(f"instance {name!r} already exists")
    if not re.match(r"^(https://|git@|/)", repo_url or ""):
        raise ValueError("repo_url must be an https://, git@, or absolute path")
    root = registry.parent
    d = root / name
    if d.exists():
        raise ValueError(f"{d} already exists")
    tmpl_name = reg.get("template") or (reg["instances"][0]["name"] if reg["instances"] else None)
    tmpl = next((i for i in reg["instances"] if i["name"] == tmpl_name), None)
    if tmpl is None:
        raise ValueError("registry has no template instance to copy fleet.env from")
    template_text = (Path(tmpl["dir"]) / "fleet.env").read_text()

    status = {"name": name, "state": "running", "steps": [], "error": None, "started": time.time()}

    def step(msg):
        status["steps"].append({"ts": time.time(), "msg": msg})
        print(f"new {name}: {msg}", flush=True)
        # atomic write (tmp + replace, same idiom as save_registry) -- GET /new/<name> reads
        # this file concurrently and must never see a partial write
        tmp = (d / "new.json").with_suffix(".json.tmp")
        tmp.write_text(json.dumps(status, indent=1))
        tmp.replace(d / "new.json")

    (d / "logs").mkdir(parents=True)
    try:
        ports = allocate_ports(used_ports(reg))
        step(f"ports {ports[0]}/{ports[1]} (green {ports[2]}/{ports[3]})")
        (d / "webhook_secret").write_text(secrets.token_urlsafe(48) + "\n")
        os.chmod(d / "webhook_secret", 0o600)
        (d / "fleet.env").write_text(render_new_env(template_text, name, repo_url, ports, PUBLIC_BASE))
        os.chmod(d / "fleet.env", 0o600)
        step(f"fleet.env from template {tmpl_name}, paused, share 0")
        subprocess.run(["git", "clone", "-q", repo_url, str(d / "repo")], capture_output=True,
                       text=True, timeout=900, check=True)
        step(f"cloned {repo_url}")
        reg["instances"].append({"name": name, "dir": str(d), "container": name,
                                 "path": f"/fleet/{name}", "weight": float(weight)})
        save_registry(reg, registry)
        step("registered")
        if caddy and CADDYFILE.exists():
            CADDYFILE.write_text(caddy_with_instance(CADDYFILE.read_text(), name, ports[0]))
            reload_caddy()
            step(f"caddy: /fleet/{name}/ -> :{ports[0]}")
        if cron:
            write_crontab(cron_with_instance(read_crontab(), name, d, read_env(d / "fleet.env").get("NTFY_TOPIC", "")))
            step("host cron: auto-deploy, liveness, path health")
        if deploy_fn is not None:
            step("deploying (first build, minutes)")
            deploy_fn(d, name, d / "logs" / "new.log")
            step("deployed")
        tick(reg)
        status["state"] = "done"
        step(f"ready, paused: resume at {PUBLIC_BASE}/fleet/ or POST resume name={name}")
    except Exception as exc:  # noqa: BLE001 -- the status file is the report
        status["state"] = "failed"
        status["error"] = f"{type(exc).__name__}: {getattr(exc, 'stderr', None) or exc}"[:2000]
        step(f"FAILED: {status['error'][:300]}")
        raise
    return status


def remove_instance(reg: dict, name: str, registry: Path = REGISTRY, caddy: bool = True,
                    cron: bool = True) -> str:
    """Undo new_instance: containers stopped and removed, routes and cron lines out, registry
    entry gone; the directory is RENAMED to <name>.removed-<ts>, never deleted (fleet.env,
    logs and the checkout stay on disk for a human)."""
    inst = next((i for i in reg["instances"] if i["name"] == name), None)
    if inst is None:
        raise ValueError(f"no instance named {name!r}")
    for c in (name, f"{name}-green", f"{name}-retired"):
        try:
            subprocess.run(["podman", "rm", "-f", c], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
    if caddy and CADDYFILE.exists():
        CADDYFILE.write_text(caddy_without_instance(CADDYFILE.read_text(), name))
        reload_caddy()
    if cron:
        write_crontab(cron_without_instance(read_crontab(), name))
    reg["instances"] = [i for i in reg["instances"] if i["name"] != name]
    save_registry(reg, registry)
    d = Path(inst["dir"])
    moved = d.with_name(f"{d.name}.removed-{int(time.time())}")
    if d.exists():
        d.rename(moved)
    tick(reg)
    return f"{name}: removed; files kept at {moved}"


def ensure_token() -> str:
    """The bearer token an agent presents to POST /new or /remove (creating an instance is a
    money decision; the page's pause/weight buttons stay open like the consoles' own toggles).
    Created on first use, 0600, one line."""
    if not TOKEN_FILE.exists():
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(secrets.token_urlsafe(32) + "\n")
        os.chmod(TOKEN_FILE, 0o600)
    return TOKEN_FILE.read_text().strip()



# ---------------------------------------------------------------- shared secret store
def include_line() -> str:
    """The one line at the top of every fleet.env that pulls the shared store in. fleet.env is
    bash-sourced everywhere it is read (host scripts, run_member.sh, the container entrypoint),
    so an include line reaches all of them with no script change; the store is bind-mounted into
    each container at the same absolute path it has on the host (the alert.env precedent in
    deploy.sh). Lines after it win, so an instance can still override one key on purpose."""
    return (f"[ -f {SECRETS_FILE} ] && . {SECRETS_FILE}  "
            f"# shared secrets: control_plane.py secrets (instance lines below override)")


def _env_lines(env_file: Path) -> list:
    return env_file.read_text(errors="ignore").splitlines() if env_file.exists() else []


def _write_lines(env_file: Path, lines: list) -> None:
    with env_file.open("w") as fh:  # in place: bind-mounted file, never rename
        fh.write("\n".join(lines) + "\n")


def shared_keys_in(env_file: Path) -> dict:
    """key -> value for every shared-secret key an instance still carries in its own fleet.env
    (last occurrence wins, as bash would)."""
    out = {}
    for line in _env_lines(env_file):
        st = line.strip()
        if st.startswith("#") or "=" not in st or st.startswith("["):
            continue
        k, v = st.split("=", 1)
        if SECRET_KEY_RE.match(k.strip()):
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def has_include(env_file: Path) -> bool:
    return any(str(SECRETS_FILE) in l and l.strip().startswith("[ -f") for l in _env_lines(env_file))


def ensure_include(env_file: Path) -> bool:
    if has_include(env_file):
        return False
    _write_lines(env_file, [include_line()] + _env_lines(env_file))
    return True


def secrets_init(reg: dict, store: Path = SECRETS_FILE) -> dict:
    """Build (or extend) the store from what the instances already carry: the template instance
    first, then the rest, first value wins per key; a later instance whose value differs is a
    CONFLICT (reported by name, its own line stays and keeps overriding). Then put the include
    line at the top of every registered fleet.env. Never prints a value."""
    existing = read_env(store) if store.exists() else {}
    merged = dict(existing)
    conflicts, added = [], []
    tmpl = reg.get("template") or (reg["instances"][0]["name"] if reg["instances"] else None)
    ordered = sorted(reg["instances"], key=lambda i: i["name"] != tmpl)
    for inst in ordered:
        for k, v in shared_keys_in(Path(inst["dir"]) / "fleet.env").items():
            if k not in merged:
                merged[k] = v
                added.append(k)
            elif merged[k] != v:
                conflicts.append(f"{inst['name']}:{k}")
    store.parent.mkdir(parents=True, exist_ok=True)
    body = "# shared secrets for every fleet instance -- control_plane.py secrets; mounted read-only\n"
    body += "".join(f"{k}={v}\n" for k, v in merged.items())
    fd = os.open(store, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    os.chmod(store, 0o600)
    included = [i["name"] for i in reg["instances"] if ensure_include(Path(i["dir"]) / "fleet.env")]
    return {"keys": sorted(merged), "added": added, "conflicts": conflicts, "included": included}


def container_has_mount(container: str) -> bool | None:
    try:
        p = subprocess.run(["podman", "inspect", container, "--format", "{{range .Mounts}}{{.Destination}} {{end}}"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return str(SECRETS_FILE) in p.stdout if p.returncode == 0 else None


def secrets_status(inst: dict, store: dict) -> dict:
    env_file = Path(inst["dir"]) / "fleet.env"
    own = shared_keys_in(env_file)
    return {"include": has_include(env_file),
            "own": sorted(own),
            "conflicts": sorted(k for k, v in own.items() if k in store and store[k] != v),
            "mount": container_has_mount(inst["container"])}


def secrets_check(reg: dict, store: Path = SECRETS_FILE) -> dict:
    st = read_env(store) if store.exists() else {}
    return {"store": str(store), "keys": sorted(st), "exists": store.exists(),
            "instances": {i["name"]: secrets_status(i, st) for i in reg["instances"]}}


def secrets_adopt(reg: dict, name: str, store: Path = SECRETS_FILE) -> str:
    """Strip an instance's own copies of shared keys whose value equals the store's, so the
    store is the only place they live. A differing value is a deliberate override and stays.
    Refuses while the instance's container lacks the mount (it would lose the key)."""
    inst = next((i for i in reg["instances"] if i["name"] == name), None)
    if inst is None:
        raise ValueError(f"no instance named {name!r}")
    st = read_env(store) if store.exists() else {}
    if not st:
        raise ValueError("store is empty; run secrets init first")
    if container_has_mount(inst["container"]) is False:
        raise ValueError(f"{inst['container']} has no {SECRETS_FILE} mount yet; redeploy it first")
    env_file = Path(inst["dir"]) / "fleet.env"
    ensure_include(env_file)
    kept, dropped = [], []
    for line in _env_lines(env_file):
        s = line.strip()
        k = s.split("=", 1)[0].strip() if "=" in s and not s.startswith(("#", "[")) else None
        if k and SECRET_KEY_RE.match(k) and st.get(k) == s.split("=", 1)[1].strip().strip('"').strip("'"):
            dropped.append(k)
            continue
        kept.append(line)
    _write_lines(env_file, kept)
    left = sorted(shared_keys_in(env_file))
    return f"{name}: dropped {len(dropped)} own copies ({', '.join(sorted(set(dropped))) or 'none'}); " \
           f"still overriding: {', '.join(left) or 'none'}"


def secrets_set(key: str, value: str, store: Path = SECRETS_FILE) -> str:
    """Rotate one key in the store (value from stdin on the CLI, never argv). Every instance
    reads it on its next tick; an instance still carrying its own copy keeps the old value
    until adopted -- check says which."""
    if not SECRET_KEY_RE.match(key):
        raise ValueError(f"{key} is not a shared-secret key (pattern {SECRET_KEY_RE.pattern})")
    if not value.strip():
        raise ValueError("empty value")
    lines = _env_lines(store)
    found = False
    for i, l in enumerate(lines):
        if l.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value.strip()}"
            found = True
    if not found:
        lines.append(f"{key}={value.strip()}")
    store.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(store, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return f"{key}: {'rotated' if found else 'added'} in {store}"


# ---------------------------------------------------------------- manage from the page
def apply_action(reg: dict, action: str, name: str, weight: str | None = None,
                 registry: Path = REGISTRY) -> str:
    """The one place a pause/resume/weight change happens, shared by the CLI and the page.
    Returns a one-line receipt. Unknown instance or action raises ValueError."""
    inst = next((i for i in reg["instances"] if i["name"] == name), None)
    if inst is None:
        raise ValueError(f"no instance named {name!r} (have: "
                         f"{', '.join(i['name'] for i in reg['instances'])})")
    if action in ("pause", "resume"):
        val = "false" if action == "pause" else "true"
        write_env_field(Path(inst["dir"]) / "fleet.env", ENABLED_KEY, val)
        return f"{name}: {ENABLED_KEY}={val}"
    if action == "weight":
        w = float(weight if weight is not None else "x")
        if w < 0:
            raise ValueError("weight must be >= 0")
        inst["weight"] = w
        save_registry(reg, registry)
        return f"{name}: weight {w:g}"
    raise ValueError(f"unknown action {action!r}")


def make_server(port: int, registry: Path = REGISTRY, deploy_fn=run_deploy):
    """GET / renders the page from a fresh tick; POST /pause, /resume, /weight apply the change
    (form fields: name, w), tick, and send the browser back to the page. stdlib only, same
    shape as fleet_view_server.py -- no framework for four routes."""
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    jobs: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # one line per request, cron-log style
            print(f"[{_central(time.time())}] {self.address_string()} {fmt % args}", flush=True)

        def send_response(self, code, message=None):  # tracks whether a response ever went out,
            self._responded = True                     # so a crashed handler knows if it can still
            super().send_response(code, message)        # answer with a 500 instead of just closing

        def _respond_error(self, exc: Exception) -> None:
            import traceback
            self.log_message("EXC %s %s: %s", self.command, self.path,
                             "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip())
            if getattr(self, "_responded", False):
                return  # already sent a response; the connection is past saving
            try:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            except Exception:
                pass  # best effort -- the socket may already be gone

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj, indent=1).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def _form(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode(errors="ignore")
            if "json" in (self.headers.get("Content-Type") or ""):
                try:
                    d = json.loads(raw or "{}")
                    return {k: str(v) for k, v in d.items()} if isinstance(d, dict) else {}
                except ValueError:
                    return {}
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

        def _authed(self, form: dict) -> bool:
            auth = self.headers.get("Authorization") or ""
            presented = auth[7:].strip() if auth.lower().startswith("bearer ") else form.get("token", "")
            return bool(presented) and secrets.compare_digest(presented, ensure_token())

        def _page(self):
            reg = load_registry(registry)
            rows = tick(reg, dry_run=True)
            body = render_index(rows, reg["human_reserve"], time.time()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._responded = False
            try:
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html", ""):
                    return self._page()
                if path.startswith("/new/"):
                    name = path[len("/new/"):].strip("/")
                    f = registry.parent / name / "new.json"
                    if NAME_RE.match(name) and f.exists():
                        return self._json(200, json.loads(f.read_text()))
                    return self._json(404, {"error": f"no job for {name!r}"})
                self.send_error(404)
            except Exception as exc:  # a handler that raises must still answer -- see _respond_error
                self._respond_error(exc)

        def do_POST(self):
            self._responded = False
            action = self.path.split("?", 1)[0].strip("/")
            try:
                form = self._form()
                name = form.get("name", "")
                try:
                    reg = load_registry(registry)
                    if action in ("new", "remove"):
                        if not self._authed(form):
                            return self._json(401, {"error": "Authorization: Bearer <token> required "
                                                             "(control_plane.py token prints the path)"})
                        if action == "remove":
                            return self._json(200, {"ok": remove_instance(reg, name, registry)})
                        repo_url = form.get("repo_url", "")
                        weight = float(form.get("w") or 1)
                        # Validate now (400 before anything is touched), then run the long part
                        # in a thread and answer 202 with where to look.
                        if not NAME_RE.match(name):
                            raise ValueError("name must match ^[a-z0-9][a-z0-9-]{0,31}$")
                        if any(i["name"] == name for i in reg["instances"]) or (registry.parent / name).exists():
                            return self._json(409, {"error": f"instance {name!r} already exists"})
                        if not re.match(r"^(https://|git@|/)", repo_url):
                            raise ValueError("repo_url must be an https://, git@, or absolute path")
                        if name in jobs and jobs[name].is_alive():
                            return self._json(409, {"error": f"{name!r} is already being created"})
                        t = threading.Thread(target=self._create, args=(reg, name, repo_url, weight), daemon=True)
                        jobs[name] = t
                        t.start()
                        return self._json(202, {"job": name, "status": f"new/{name}",
                                                "then": f"POST resume name={name} when it should spend"})
                    receipt = apply_action(reg, action, name, form.get("w"), registry)
                    tick(reg)
                    print(receipt, flush=True)
                except ValueError as exc:
                    if "json" in (self.headers.get("Content-Type") or "") or action in ("new", "remove"):
                        return self._json(400, {"error": str(exc)})
                    self.send_error(400, str(exc))
                    return
                self.send_response(303)
                self.send_header("Location", "./")
                self.end_headers()
            except Exception as exc:  # a handler that raises must still answer -- see _respond_error
                self._respond_error(exc)

        @staticmethod
        def _create(reg, name, repo_url, weight):
            try:
                new_instance(reg, name, repo_url, weight, registry, deploy_fn=deploy_fn)
            except Exception as exc:  # noqa: BLE001 -- already in new.json; keep the thread quiet
                print(f"new {name}: failed: {exc}", flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", port), H)
    server.jobs = jobs  # so a caller (tests, mainly) can join the /new background threads it started
    return server


def serve(port: int) -> None:
    print(f"control_plane serving on :{port}", flush=True)
    make_server(port).serve_forever()


# ---------------------------------------------------------------- the tick
def tick(reg: dict, dry_run: bool = False, out_dir: Path | None = None, now: float | None = None) -> list[dict]:
    now = now or time.time()
    out_dir = out_dir or OUT_DIR
    insts = reg["instances"]
    envs = {i["name"]: read_env(Path(i["dir"]) / "fleet.env") for i in insts}
    enabled = {n: is_enabled(e) for n, e in envs.items()}
    shares = compute_shares(insts, enabled, reg["human_reserve"])
    containers = container_status()
    store = read_env(SECRETS_FILE) if SECRETS_FILE.exists() else {}
    rows = []
    for i in insts:
        d = Path(i["dir"])
        name = i["name"]
        share = shares[name]
        current = envs[name].get(SHARE_KEY)
        changed = False
        try:
            same = current is not None and abs(float(current) - share) < 1e-6
        except ValueError:
            same = False
        if not same and not dry_run:
            changed = write_env_field(d / "fleet.env", SHARE_KEY, f"{share:g}")
        print(f"{name}: share {current} -> {share:g}{' (written)' if changed else ''}"
              f"{' [paused]' if not enabled[name] else ''}")
        rows.append({"name": name, "path": i["path"], "container_name": i["container"],
                     "secrets": secrets_status(i, store) if store else None,
                     "container": containers.get(i["container"], "no container"),
                     "enabled": enabled[name], "weight": float(i["weight"]), "share": share,
                     "number": read_number(d / "logs"), "runs": runs_last_hour(d / "logs", now),
                     "head": repo_head(d)})
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / ".index.html.tmp"
        tmp.write_text(render_index(rows, reg["human_reserve"], now))
        tmp.replace(out_dir / "index.html")
    return rows


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "tick"
    reg = load_registry()
    if cmd == "tick":
        tick(reg, dry_run="--dry-run" in argv)
    elif cmd == "serve":
        serve(int(argv[argv.index("--port") + 1]) if "--port" in argv else 8600)
    elif cmd == "new":
        try:
            w = float(argv[argv.index("--weight") + 1]) if "--weight" in argv else 1.0
            new_instance(reg, argv[1], argv[2], w, deploy_fn=None if "--no-deploy" in argv else run_deploy)
        except (ValueError, IndexError) as exc:
            sys.exit(f"new: {exc}")
    elif cmd == "remove":
        try:
            print(remove_instance(reg, argv[1]))
        except (ValueError, IndexError) as exc:
            sys.exit(f"remove: {exc}")
    elif cmd == "secrets":
        sub = argv[1] if len(argv) > 1 else "check"
        try:
            if sub == "init":
                r = secrets_init(reg)
                print(f"store {SECRETS_FILE}: {len(r['keys'])} keys ({', '.join(r['keys'])})")
                print(f"added: {', '.join(r['added']) or 'none'}; include line added to: {', '.join(r['included']) or 'none'}")
                print(f"conflicts (instance keeps its own value): {', '.join(r['conflicts']) or 'none'}")
            elif sub == "check":
                r = secrets_check(reg)
                print(f"store {r['store']}: {'missing' if not r['exists'] else str(len(r['keys'])) + ' keys'}")
                for n, st in r["instances"].items():
                    print(f"  {n}: include={'yes' if st['include'] else 'NO'} mount="
                          f"{'yes' if st['mount'] else ('NO' if st['mount'] is False else '?')} "
                          f"own copies={len(st['own'])} conflicts={', '.join(st['conflicts']) or 'none'}")
            elif sub == "adopt":
                print(secrets_adopt(reg, argv[2]))
                tick(reg)
            elif sub == "set":
                print(secrets_set(argv[2], sys.stdin.read()))
            else:
                raise ValueError("secrets init | check | adopt NAME | set KEY < value")
        except (ValueError, IndexError) as exc:
            sys.exit(f"secrets: {exc}")
    elif cmd == "token":
        ensure_token()
        print(TOKEN_FILE)
    elif cmd in ("pause", "resume", "weight"):
        try:
            print(apply_action(reg, cmd, argv[1], argv[2] if len(argv) > 2 else None))
        except (ValueError, IndexError) as exc:
            sys.exit(f"{cmd}: {exc}")
        tick(reg)
    else:
        print(__doc__.split("Usage (host):", 1)[1], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
