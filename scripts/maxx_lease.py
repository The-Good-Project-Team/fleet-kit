#!/usr/bin/env python3
"""maxx_lease.py — local reservation ledger for maxx's per-diem allowance.

Provenance: gh#161 part 2. gru.md steps 3a/6 have documented `maxx_reserve(pct=...,
label=..., ttl_sec=...)` / `maxx_release(lease_id=...)` since 2026-08-something, but neither
function existed anywhere in this repo -- every gru pass silently no-op'd the reservation,
so `reserved_pct` in maxx_reader.py's output stayed permanently 0 no matter how many hours of
spend should logically still be reserved. This module implements the contract gru.md already
documents; nothing there needs to change once this lands.

This is a LOCAL-ONLY lease ledger, not a real remote reservation against maxx itself (that is
gh#161 part 1, a human/credential decision, out of scope here). It exists purely so that
back-to-back gru passes (cron does not wait for one pass to fully land before the next fires)
can see each other's in-flight spend.

Persists to a flat JSON file under $FLEET_LOG_DIR, using the same tmp-file-then-`mv` atomic
write account_pool.sh's state file already uses -- no new dependency, no sqlite table.

A lease past `created_at + ttl_sec` is treated as expired and excluded from
`total_reserved_pct()` without an explicit release call -- gru.md's documented backstop: "a
lease that outlives its own hour self-expires instead of choking every later pass forever."
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
STATE_FILE = LOG_DIR / "maxx-leases.json"


def _read_leases(state_file: Path) -> list[dict]:
    try:
        return json.loads(state_file.read_text())
    except (FileNotFoundError, ValueError):
        return []


def _write_leases(state_file: Path, leases: list[dict]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(leases))
    tmp.replace(state_file)


def _unexpired(leases: list[dict], now: float) -> list[dict]:
    return [lease for lease in leases if lease["created_at"] + lease["ttl_sec"] > now]


def maxx_reserve(pct: float, label: str, ttl_sec: int, state_file: Path | None = None) -> str:
    """Record a new lease and return its lease_id. Prunes expired leases while it's at it."""
    state_file = state_file or STATE_FILE
    now = time.time()
    lease_id = f"{label}-{uuid.uuid4().hex[:8]}"
    leases = _unexpired(_read_leases(state_file), now)
    leases.append({"lease_id": lease_id, "pct": pct, "label": label,
                    "created_at": now, "ttl_sec": ttl_sec})
    _write_leases(state_file, leases)
    return lease_id


def maxx_release(lease_id: str, state_file: Path | None = None) -> None:
    """Drop a lease early. Always safe to call -- a lease that already expired and was pruned
    is simply not found, never an error (gru.md step 6 calls this unconditionally, even on a
    failure path)."""
    state_file = state_file or STATE_FILE
    leases = [lease for lease in _unexpired(_read_leases(state_file), time.time())
              if lease["lease_id"] != lease_id]
    _write_leases(state_file, leases)


def total_reserved_pct(state_file: Path | None = None) -> float:
    """Sum of every currently-unexpired lease's pct. Prunes expired leases as a side effect,
    same self-cleaning shape as account_pool.sh's exhaustion state file."""
    state_file = state_file or STATE_FILE
    now = time.time()
    leases = _read_leases(state_file)
    live = _unexpired(leases, now)
    if len(live) != len(leases):
        _write_leases(state_file, live)
    return sum(lease["pct"] for lease in live)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reserve = sub.add_parser("reserve", help="record a new lease, print its lease_id")
    p_reserve.add_argument("--pct", type=float, required=True)
    p_reserve.add_argument("--label", required=True)
    p_reserve.add_argument("--ttl-sec", type=int, default=3600)

    p_release = sub.add_parser("release", help="drop a lease early")
    p_release.add_argument("--lease-id", required=True)

    sub.add_parser("total", help="sum of all currently-unexpired leases' pct")

    args = parser.parse_args()
    if args.cmd == "reserve":
        print(json.dumps({"lease_id": maxx_reserve(args.pct, args.label, args.ttl_sec)}))
    elif args.cmd == "release":
        maxx_release(args.lease_id)
        print(json.dumps({"released": args.lease_id}))
    elif args.cmd == "total":
        print(json.dumps({"reserved_pct": total_reserved_pct()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
