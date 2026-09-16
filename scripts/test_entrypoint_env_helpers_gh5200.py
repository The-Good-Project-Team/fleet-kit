"""Regression test for entrypoint.sh's crontab env-render helpers (gh#5200).

Vixie cron discards the ENTIRE crontab file -- not just the offending line -- the moment it
sees one env assignment with an empty right-hand side. `entrypoint.sh` used to render
`FLEET_SHARE_DIR=${FLEET_SHARE_DIR:-}` etc directly, so an unset variable silenced all 27
fleet jobs with no error and no log line (the 2026-09-10 ~100-minute outage). The fix replaced
those bare `echo` calls with two helpers: `emit_env_optional` (omit the line when the value is
empty) and `emit_env_required` (exit 1 naming the variable rather than write an empty line).

This test extracts the real helper functions out of entrypoint.sh (between the `BEGIN gh5200
env-render helpers` / `END gh5200 env-render helpers` markers) and exercises the actual bash
text, not a reimplementation, so a regression in entrypoint.sh itself is caught.

Run: python3 scripts/test_entrypoint_env_helpers_gh5200.py
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
ENTRYPOINT = KIT / "entrypoint.sh"


def _extract_helpers() -> str:
    text = ENTRYPOINT.read_text()
    begin = "# BEGIN gh5200 env-render helpers"
    end = "# END gh5200 env-render helpers"
    start = text.index(begin)
    stop = text.index(end, start)
    return text[start:stop]


def _run(script_body: str) -> subprocess.CompletedProcess:
    helpers = _extract_helpers()
    full = "set -u\n" + helpers + "\n" + script_body
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True, timeout=15)


class EmitEnvOptionalTests(unittest.TestCase):
    def test_unset_value_is_omitted_not_rendered_empty(self):
        result = _run('emit_env_optional FLEET_SHARE_DIR ""')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "an empty value must produce no output line at all")

    def test_populated_value_is_rendered(self):
        result = _run('emit_env_optional FLEET_SHARE_DIR "/fleet-kit/shares"')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "FLEET_SHARE_DIR=/fleet-kit/shares\n")

    def test_three_known_unset_vars_produce_zero_lines(self):
        result = _run(
            'emit_env_optional FLEET_SHARE_DIR ""\n'
            'emit_env_optional FLEET_LEASE_DIR ""\n'
            'emit_env_optional FLEET_INSTANCE_NAME ""\n'
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


class EmitEnvRequiredTests(unittest.TestCase):
    def test_populated_value_is_rendered(self):
        result = _run('emit_env_required HOME "/root"')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "HOME=/root\n")

    def test_empty_value_exits_nonzero_and_names_the_variable(self):
        result = _run('emit_env_required HOME ""')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HOME", result.stderr)
        self.assertEqual(result.stdout, "", "must not write a line cron would silently drop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
