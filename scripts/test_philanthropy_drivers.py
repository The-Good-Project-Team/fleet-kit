"""Tests for scripts/drivers/philanthropy_prod_diag.sh and philanthropy_deploy.sh (gh#728, AC5).

Neither driver ever touches a real host in these tests: a fake `ssh` executable is put first on
PATH, and each test asserts on what THAT fake recorded being called with -- same technique the
issue's own AC5 names ("asserted against a stubbed ssh so the test never touches the real
host").

Run: python3 scripts/test_philanthropy_drivers.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIAG_DRIVER = ROOT / "scripts" / "drivers" / "philanthropy_prod_diag.sh"
DEPLOY_DRIVER = ROOT / "scripts" / "drivers" / "philanthropy_deploy.sh"


def _fake_ssh_dir(tmp: Path, behavior: str) -> Path:
    """Writes a fake `ssh` onto its own directory (to prepend to PATH) that records its argv
    to $SSH_CALL_LOG and behaves per `behavior` (a bash snippet after logging)."""
    bindir = tmp / "bin"
    bindir.mkdir()
    fake_ssh = bindir / "ssh"
    fake_ssh.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo "$@" >> "$SSH_CALL_LOG"
        {behavior}
    """))
    fake_ssh.chmod(0o755)
    return bindir


class ProdDiagDriverTests(unittest.TestCase):
    """AC5: invoked with a section argument, shells out to `ssh atlas-serve prod-diag "$1"` and
    nothing else."""

    def _run(self, tmp: Path, section: str, behavior: str = "echo fake-diag-output"):
        bindir = _fake_ssh_dir(tmp, behavior)
        call_log = tmp / "ssh_calls.log"
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["SSH_CALL_LOG"] = str(call_log)
        result = subprocess.run(
            [str(DIAG_DRIVER), section], capture_output=True, text=True, env=env, timeout=10,
        )
        calls = call_log.read_text().splitlines() if call_log.exists() else []
        return result, calls

    def test_shells_to_ssh_atlas_serve_prod_diag_with_the_section_arg(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self._run(Path(tmp), "pg")
            self.assertEqual(calls, ["atlas-serve prod-diag pg"])
            self.assertEqual(result.returncode, 0)
            self.assertIn("fake-diag-output", result.stdout)

    def test_a_different_section_is_forwarded_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, calls = self._run(Path(tmp), "all")
            self.assertEqual(calls, ["atlas-serve prod-diag all"])

    def test_only_ssh_is_ever_invoked(self):
        """Nothing else shells out -- one call, one command, exactly what AC5 asks to assert."""
        with tempfile.TemporaryDirectory() as tmp:
            _, calls = self._run(Path(tmp), "pg")
            self.assertEqual(len(calls), 1)

    def test_a_nonzero_ssh_propagates_the_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run(Path(tmp), "pg", behavior="echo denied >&2; exit 7")
            self.assertEqual(result.returncode, 7)
            self.assertIn("denied", result.stderr)


class DeployDriverTests(unittest.TestCase):
    def _run(self, tmp: Path, args: list[str], behavior: str = "echo ok"):
        bindir = _fake_ssh_dir(tmp, behavior)
        call_log = tmp / "ssh_calls.log"
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["SSH_CALL_LOG"] = str(call_log)
        result = subprocess.run(
            [str(DEPLOY_DRIVER), *args], capture_output=True, text=True, env=env, timeout=10,
        )
        calls = call_log.read_text().splitlines() if call_log.exists() else []
        return result, calls

    def test_current_sha_reads_checksum_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self._run(Path(tmp), ["current_sha"], behavior="echo deadbeef")
            self.assertEqual(calls, ["atlas-serve checksum-report"])
            self.assertIn("deadbeef", result.stdout)
            self.assertEqual(result.returncode, 0)

    def test_rollback_calls_rollback_blue_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self._run(Path(tmp), ["rollback"])
            self.assertEqual(calls, ["atlas-serve rollback-blue-green"])
            self.assertEqual(result.returncode, 0)

    def test_status_ok_when_atlas_serve_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run(Path(tmp), ["status"])
            self.assertEqual(result.returncode, 0)
            self.assertIn("ok", result.stdout)

    def test_status_fails_loud_when_atlas_serve_does_not_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run(Path(tmp), ["status"], behavior="exit 3")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FAILED", result.stderr)

    def test_unknown_command_rejected_without_touching_ssh(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self._run(Path(tmp), ["deploy"])
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls, [], "an unrecognized verb must never reach ssh")


if __name__ == "__main__":
    unittest.main()
