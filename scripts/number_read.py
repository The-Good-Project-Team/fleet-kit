#!/usr/bin/env python3
"""number_read.py -- fetch the venture's number and render it at the top of every prompt.

fleet-kit#513. Fleet-kit owns the loop; the venture owns the number. An instance sets
FLEET_NUMBER_URL (and FLEET_NUMBER_TOKEN) in its fleet.env, pointing at an endpoint that
answers:

    {"as_of": "...", "number": {name, value, unit, delta_7d}, "guardrail": {...},
     "channel": {...}, "errors": [...]}

Two modes:

  --fetch   GET the URL, write $FLEET_LOG_DIR/number.json (with fetched_at) and append one
            line to number_history.jsonl. A failed fetch leaves the previous number.json in
            place and exits 0 -- the reader below reports it STALE; a cron job that dies on
            a 503 helps nobody. Never called by a member (KPI doctrine rule 1: the measured
            never measures itself); the crontab line in entrypoint.sh calls it.

  --render  Print the header run_member.sh puts above every charter, or nothing at all when
            no URL is configured (a venture with no number configured gets no header, not a
            fake one). STALE past NUMBER_STALE_S (48h) is said out loud on the first line
            (rule 5: missing reads STALE, never last-known-as-current).

The header is deliberately five short lines. It replaces reading 15 KB of VISION prose to
find out what matters: members self-arrange when they can see the number move or not move.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

STALE_S = int(os.environ.get("NUMBER_STALE_S", str(48 * 3600)))


def _paths() -> tuple[Path, Path]:
    log_dir = Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"))
    return log_dir / "number.json", log_dir / "number_history.jsonl"


def fetch(url: str, token: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, headers={"X-PM-Token": token, "User-Agent": "fleet-kit/number_read"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def do_fetch() -> int:
    url = os.environ.get("FLEET_NUMBER_URL", "").strip()
    if not url:
        print("number_read: FLEET_NUMBER_URL unset -- this instance has no number configured", file=sys.stderr)
        return 0
    token = os.environ.get("FLEET_NUMBER_TOKEN", "").strip()
    current, history = _paths()
    try:
        payload = fetch(url, token)
    except Exception as exc:  # noqa: BLE001 -- a 503 must not kill the cron job
        print(f"number_read: fetch failed ({type(exc).__name__}: {str(exc)[:160]}) -- keeping previous number.json", file=sys.stderr)
        return 0
    if not isinstance(payload, dict) or "number" not in payload:
        print(f"number_read: endpoint answered without a 'number' key -- keeping previous number.json", file=sys.stderr)
        return 0
    payload = {**payload, "fetched_at": int(time.time()), "url": url}
    current.parent.mkdir(parents=True, exist_ok=True)
    tmp = current.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(current)
    with history.open("a") as fh:
        fh.write(json.dumps(payload) + "\n")
    n = payload.get("number") or {}
    print(f"number_read: {n.get('name', 'number')}={n.get('value')} {n.get('unit', '')} as_of={payload.get('as_of')}")
    return 0


def _fmt(v) -> str:
    if v is None:
        return "unmeasured"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _delta(v) -> str:
    if v is None:
        return "delta unmeasured"
    sign = "+" if v >= 0 else ""
    return f"{sign}{_fmt(v)} in 7d"


def render(payload: dict, now: float | None = None) -> str:
    now = now or time.time()
    age = now - int(payload.get("fetched_at") or 0)
    stale = age > STALE_S
    lines = []
    head = "THE NUMBER (read this before your charter)"
    if stale:
        head += f" -- STALE, last read {age / 3600:.0f}h ago; treat every figure below as unverified"
    lines.append(head)
    for key, label in (("number", "Number"), ("guardrail", "Guardrail"), ("channel", "Channel")):
        block = payload.get(key)
        if not block:
            lines.append(f"{label}: unmeasured (the endpoint could not read it)")
            continue
        unit = block.get("unit", "")
        lines.append(f"{label}: {block.get('name', key)} = {_fmt(block.get('value'))} {unit}".rstrip() + f" ({_delta(block.get('delta_7d'))})")
    errs = payload.get("errors") or []
    tail = f"as of {payload.get('as_of', '?')}."
    if errs:
        tail += f" {len(errs)} reader error(s): " + "; ".join(str(e)[:80] for e in errs[:2])
    lines.append(tail + " Every item you file or PR you open names which of these it moves, or says none.")
    return "\n".join(lines)


def do_render() -> int:
    if not os.environ.get("FLEET_NUMBER_URL", "").strip():
        return 0
    current, _ = _paths()
    if not current.exists():
        print("THE NUMBER: not yet read for this instance (number.json missing) -- report it as unmeasured, never as zero.")
        return 0
    try:
        payload = json.loads(current.read_text())
    except Exception:  # noqa: BLE001
        print("THE NUMBER: number.json unreadable -- report it as unmeasured, never as zero.")
        return 0
    print(render(payload))
    return 0


def main(argv: list[str]) -> int:
    if "--fetch" in argv:
        return do_fetch()
    if "--render" in argv:
        return do_render()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
