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
    control_plane.py pause NAME           FLEET_ENABLED=false in that fleet.env, then tick
    control_plane.py resume NAME          FLEET_ENABLED=true, then tick
    control_plane.py weight NAME N        set the weight in the registry, then tick

The index (one page, every instance: number, share, weight, live build, runs last hour, last
brief) is written to $FLEET_CONTROL_PLANE_OUT/index.html (default
~/.cache/fleet-kit/control_plane) and served by caddy at /fleet/.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import os
import subprocess
import sys
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
        trs.append(
            f"<tr class='{state}'><td><a href='{html.escape(r['path'])}/'>{html.escape(r['name'])}</a>"
            f"<div class=sub>{html.escape(r['container_name'])} · {html.escape(r['container'])}</div></td>"
            f"<td>{state}</td><td>{num_txt}</td>"
            f"<td><b>{r['share']:.0%}</b><div class=sub>weight {r['weight']:g}</div></td>"
            f"<td>{runs['ok']} ok · <span class='{'warn' if runs['budget_declined'] else ''}'>"
            f"{runs['budget_declined']} budget-declined</span></td>"
            f"<td><code>{html.escape(r['head'])}</code></td><td>{_central(runs['last_brief'])}</td></tr>")
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>fleet control plane</title>
<style>body{{font:15px/1.4 system-ui,sans-serif;margin:2rem;max-width:72rem;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:.6rem .7rem;border-bottom:1px solid #ddd;vertical-align:top}}
th{{font-size:.8rem;text-transform:uppercase;color:#666}}.sub{{font-size:.8rem;color:#777}}
tr.paused td{{color:#999}}tr.DOWN td:nth-child(2){{color:#b00;font-weight:600}}.warn{{color:#b00;font-weight:600}}
p.meta{{color:#666;font-size:.9rem}}</style>
<h1>Control plane</h1>
<p class=meta>{len(rows)} instances · shares sum {total:.0%} · human reserve {human_reserve:.0%} ·
written {_central(generated)} · pause/resume from each instance's console (on/off button) or
<code>control_plane.py pause NAME</code>; weights in <code>instances/registry.json</code></p>
<table><tr><th>instance</th><th>state</th><th>number</th><th>share this hour</th><th>runs last hour</th><th>live build</th><th>last brief</th></tr>
{''.join(trs)}</table>"""


# ---------------------------------------------------------------- the tick
def tick(reg: dict, dry_run: bool = False, out_dir: Path = OUT_DIR, now: float | None = None) -> list[dict]:
    now = now or time.time()
    insts = reg["instances"]
    envs = {i["name"]: read_env(Path(i["dir"]) / "fleet.env") for i in insts}
    enabled = {n: is_enabled(e) for n, e in envs.items()}
    shares = compute_shares(insts, enabled, reg["human_reserve"])
    containers = container_status()
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


def _find(reg: dict, name: str) -> dict:
    for i in reg["instances"]:
        if i["name"] == name:
            return i
    sys.exit(f"no instance named {name!r} in {REGISTRY} (have: "
             f"{', '.join(i['name'] for i in reg['instances'])})")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "tick"
    reg = load_registry()
    if cmd == "tick":
        tick(reg, dry_run="--dry-run" in argv)
    elif cmd in ("pause", "resume"):
        inst = _find(reg, argv[1])
        write_env_field(Path(inst["dir"]) / "fleet.env", ENABLED_KEY, "false" if cmd == "pause" else "true")
        print(f"{inst['name']}: {ENABLED_KEY}={'false' if cmd == 'pause' else 'true'}")
        tick(reg)
    elif cmd == "weight":
        inst = _find(reg, argv[1])
        inst["weight"] = float(argv[2])
        save_registry(reg)
        print(f"{inst['name']}: weight {inst['weight']:g}")
        tick(reg)
    else:
        print(__doc__.split("Usage (host):", 1)[1], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
