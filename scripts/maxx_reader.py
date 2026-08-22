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
verdict this module doesn't recognize) returns a result that fanout.py's caller can only use
to CONSERVE ambition, never to invent an outage-shaped hard stop of its own.

Env:
  FLEET_MAXX_URL     base URL, e.g. https://api.meetmaxx.co (no trailing /mcp)
  FLEET_MAXX_HANDLE   the maxx handle to read
  FLEET_MAXX_KEY      the bearer/query-string secret (never logged, never printed)

If any of the three is unset, get_headroom_fraction() returns None -- "no real signal
configured," the caller's documented cue to fall back to its own fixed-ceiling default. This
is not an error state; most fleet-kit consumers will never set these.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

SAFE_VERDICTS = {"ok", "degraded"}
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


def get_headroom_fraction(
    base_url: str | None = None, handle: str | None = None, key: str | None = None,
    fetcher=fetch_budget,
) -> tuple[float | None, str]:
    """(fraction, label). fraction is 0.0-1.0 -- how much of the current pacing window is
    still safe to spend -- or None when no real signal is available/trustworthy. label is
    always a short diagnostic string, never sensitive (no secret, no raw payload).

    fraction is derived from session_advised_pct/session_used_pct when maxx marks them live
    (the same "pace against your own advised share, not raw 5h%" lesson m_budget_maxx.py's
    _tier_from_real_usage learned the hard way) -- NOT usage_week_pct/usage_five_pct directly,
    which read fine at 100% right up until the wall.
    """
    base_url = base_url or os.environ.get("FLEET_MAXX_URL")
    handle = handle or os.environ.get("FLEET_MAXX_HANDLE")
    key = key or os.environ.get("FLEET_MAXX_KEY")
    if not (base_url and handle and key):
        return None, "not_configured"

    budget = fetcher(base_url, handle, key)
    if isinstance(budget, str):
        return None, budget
    if not isinstance(budget, dict):
        return None, "maxx_unexpected_shape"

    verdict = str(budget.get("verdict", "")).lower()
    if verdict not in SAFE_VERDICTS:
        return None, f"maxx_verdict_{verdict or 'missing'}"

    advised = budget.get("session_advised_pct")
    used = budget.get("session_used_pct")
    if advised is not None and used is not None and advised > 0:
        fraction = max(0.0, 1.0 - (float(used) / float(advised)))
        return fraction, "ok"

    # No session pacing fields -- fall back to the coarser week-bank reading rather than
    # refusing outright (still a real, if less precise, signal).
    bank = budget.get("week_bank_pct")
    if bank is not None:
        return max(0.0, min(1.0, float(bank) / 100.0)), "ok_bank_only"

    return None, "maxx_missing_pacing_fields"


def main() -> int:
    fraction, label = get_headroom_fraction()
    print(json.dumps({"headroom_fraction": fraction, "label": label}))
    return 0 if fraction is not None else 1


if __name__ == "__main__":
    sys.exit(main())
