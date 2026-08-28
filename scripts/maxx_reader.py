#!/usr/bin/env python3
"""maxx_reader.py — read real Claude usage headroom from a maxx MCP endpoint.

Provenance: nonprofit-atlas's scripts/m_budget_maxx.py reads the same signal via a REST-shaped
endpoint (GET /api/u/{handle}/budget). That endpoint is not reachable the same way from every
box (confirmed 404 from a plain client on 2026-08-22 -- turned out that path doesn't exist for
this deployment; the real one is MCP JSON-RPC at /mcp). This module speaks the real shape:

  POST {base_url}/mcp?handle={handle}&k={secret}
  {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"maxx_budget","arguments":{}}}

Verified live 2026-08-22 against api.meetmaxx.co: returns verdict/usage_week_pct/
session_advised_pct/session_used_pct, real numbers, HTTP 200.

FAILS OPEN, ALWAYS. A meter that cannot be read must never stop a fleet -- same law
m_budget_maxx.py's header documents from real incident (26h dark fleet from a meter
misclassified as over-budget). Any read failure (network, bad JSON, missing fields, non-ok
verdict this module doesn't recognize) returns a result the caller (gru, reading this
directly per its own charter) can only use to CONSERVE ambition, never to invent an
outage-shaped hard stop of its own.

Env:
  FLEET_MAXX_URL     base URL, e.g. https://api.meetmaxx.co (no trailing /mcp)
  FLEET_MAXX_HANDLE   the maxx handle to read
  FLEET_MAXX_KEY      the bearer/query-string secret (never logged, never printed)

If any of the three is unset, get_headroom() returns None -- "no real signal
configured," the caller's documented cue to fall back to its own fixed-ceiling default. This
is not an error state; most fleet-kit consumers will never set these.

WHAT THE FRACTION MEASURES. The per-diem/week bank, which is fleet-wide. It is deliberately
NOT session_advised_pct/session_used_pct: those describe one interactive session, and reading
them as a fleet signal idled the build fleet for hours on 2026-08-26 (a laptop at 22% of a
7.4% advised share pinned this to 0.0 while per_diem_hourly_pct=0.356 and verdict=ok).
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

import maxx_lease

SAFE_VERDICTS = {"ok", "degraded"}

# maxx's own definitive "at/past the ceiling" answer -- a REAL restrictive reading, not a
# broken meter. Kept separate from SAFE_VERDICTS so verdict=over routes to a real 0.0-headroom
# result below, never through the unreadable-meter branch. Conflating the two is the exact
# failure nonprofit-atlas's scripts/m_budget_maxx.py (board #9777937) already fixed once: an
# `|| BUDGET_JSON=full` fallback overwrote a correctly-computed standby with a hardcoded
# default because `over` and `unreadable` shared one bucket. Recurred here 2026-08-27 ~11:03
# UTC: verdict=over on 3+ consecutive gru passes read as `maxx_verdict_over` (unreadable) and
# fell back to a several-hours-stale cached allowance instead of the real "spend ~0" signal
# maxx was actually sending (nonprofit-atlas#3422).
OVER_VERDICTS = {"over"}
TIMEOUT_S = 20.0  # matches m_budget_maxx.py's measured 2-6s real latency through Cloudflare


def _ssl_context() -> "ssl.SSLContext":
    ctx = ssl.create_default_context()
    cafile = ssl.get_default_verify_paths().openssl_cafile
    if cafile and os.path.exists(cafile):
        return ctx
    try:
        import certifi
    except ImportError:
        return ctx
    return ssl.create_default_context(cafile=certifi.where())


def fetch_budget(base_url: str, handle: str, key: str, timeout: float = TIMEOUT_S) -> dict | str:
    """POST the maxx_budget tool call. Returns the parsed inner JSON payload, or an error
    label string -- never a budget-shaped stand-in for a failure (same discipline as
    m_budget_maxx.py: a failure must never be readable as a real over/under-budget answer)."""
    url = f"{base_url.rstrip('/')}/mcp?{urllib.parse.urlencode({'handle': handle, 'k': key})}"
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "maxx_budget", "arguments": {}},
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "fleet-kit-maxx-reader/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            envelope = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "maxx_auth_rejected"
        return f"maxx_http_{exc.code}"
    except ValueError:
        return "maxx_bad_json"
    except (urllib.error.URLError, TimeoutError, OSError):
        return "maxx_unreachable"

    try:
        text = envelope["result"]["content"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError):
        return "maxx_unexpected_shape"


def get_headroom(
    base_url: str | None = None, handle: str | None = None, key: str | None = None,
    fetcher=fetch_budget,
) -> tuple[float | None, str, dict]:
    """(fraction, label, budget). `budget` carries the fleet's real allowance fields through
    to the caller -- `per_diem_hourly_pct` and `reserved_pct` are what gru.md and fanout.py
    both document spending against, and returning only a scalar left gru with no source for
    the one number its charter tells it to use. Empty dict whenever there is no trustworthy
    reading.

    `fraction` is 0.0-1.0 -- how much of the PER-DIEM bank is still safe to spend -- or None
    when no real signal is available.

    NOT derived from session_advised_pct/session_used_pct. Those describe ONE interactive
    session's pacing, and on 2026-08-26 they idled the whole build fleet for hours: a laptop
    at 22% of a 7.4% advised share drove this to exactly 0.0 while the fleet's own slice
    (per_diem_hourly_pct=0.356, reserved_pct=0, verdict=ok) was entirely healthy. A human's
    hot session is not a fleet-wide wall. The week bank is the fleet-wide quantity, so it is
    what gates the fleet.
    """
    base_url = base_url or os.environ.get("FLEET_MAXX_URL")
    handle = handle or os.environ.get("FLEET_MAXX_HANDLE")
    key = key or os.environ.get("FLEET_MAXX_KEY")
    if not (base_url and handle and key):
        return None, "not_configured", {}

    budget = fetcher(base_url, handle, key)
    if isinstance(budget, str):
        return None, budget, {}
    if not isinstance(budget, dict):
        return None, "maxx_unexpected_shape", {}

    verdict = str(budget.get("verdict", "")).lower()

    allowance = {
        k: budget[k] for k in ("per_diem_hourly_pct", "reserved_pct", "week_bank_pct",
                               "per_diem_usable_pct", "sustainable_pct_per_hour", "verdict")
        if budget.get(k) is not None
    }

    if verdict in OVER_VERDICTS:
        # A REAL over-budget reading, not a broken meter -- 0.0 headroom is the honest answer,
        # never None (None means "no trustworthy reading", which would send the caller back to
        # a stale fallback instead of the fresh "spend ~0" signal maxx just gave it).
        return 0.0, "over", allowance
    if verdict not in SAFE_VERDICTS:
        return None, f"maxx_verdict_{verdict or 'missing'}", allowance

    bank = budget.get("week_bank_pct")
    if bank is None:
        return None, "maxx_missing_pacing_fields", allowance
    return max(0.0, min(1.0, float(bank) / 100.0)), "ok", allowance


def get_headroom_fraction(
    base_url: str | None = None, handle: str | None = None, key: str | None = None,
    fetcher=fetch_budget,
) -> tuple[float | None, str]:
    """Back-compat two-tuple for callers that only want the scalar."""
    fraction, label, _ = get_headroom(base_url, handle, key, fetcher)
    return fraction, label


def main() -> int:
    # --selftest exercises the CLI's shape without a network call.
    if "--selftest" in sys.argv:
        fraction, label, budget = get_headroom(
            "https://example.invalid", "h", "k",
            fetcher=lambda *a, **k: {"verdict": "ok", "week_bank_pct": 1.9,
                                     "per_diem_hourly_pct": 0.356, "reserved_pct": 0},
        )
    else:
        fraction, label, budget = get_headroom()
    output = {"headroom_fraction": fraction, "label": label, **budget}
    # gh#161 part 2: reserved_pct is a LOCAL contribution on top of whatever the remote
    # returned (today, nothing -- the remote never learns about gru's in-flight leases).
    output["reserved_pct"] = budget.get("reserved_pct", 0.0) + maxx_lease.total_reserved_pct()
    print(json.dumps(output))
    return 0 if fraction is not None else 1


if __name__ == "__main__":
    sys.exit(main())
