"""A child process this server spawns must see fleet.env's values.

RED without subprocess_env(): the child inherits a process environment that never carried
FLEET_MAXX_* (the container is handed FLEET_ENV_FILE, a path, not the file's values), so
maxx_share_ceiling.py reads an unconfigured meter and the Settings page reports the meter
as unreadable while it is healthy.
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent


class SubprocessEnvCarriesFleetEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self.tmp.write("FLEET_MAXX_URL=https://example.invalid\n"
                       "FLEET_MAXX_HANDLE=probe\n"
                       "FLEET_MAXX_KEY=probe-secret\n")
        self.tmp.close()
        os.environ["FLEET_ENV_FILE"] = self.tmp.name
        # The container state this reproduces: the values exist ONLY in the file.
        for k in ("FLEET_MAXX_URL", "FLEET_MAXX_HANDLE", "FLEET_MAXX_KEY"):
            os.environ.pop(k, None)
        sys.path.insert(0, str(KIT / "scripts"))
        for mod in [m for m in list(sys.modules) if m == "fleet_view_server"]:
            del sys.modules[mod]

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_child_process_sees_maxx_credentials_from_the_file(self):
        import fleet_view_server as fvs
        env = fvs.subprocess_env()
        for k, want in (("FLEET_MAXX_URL", "https://example.invalid"),
                        ("FLEET_MAXX_HANDLE", "probe"),
                        ("FLEET_MAXX_KEY", "probe-secret")):
            self.assertEqual(env.get(k), want,
                             f"{k} must reach a child process from fleet.env, not os.environ")

        # And prove it end to end: a child launched with that env actually receives them.
        out = subprocess.run(
            [sys.executable, "-c",
             "import os;print(os.environ.get('FLEET_MAXX_HANDLE',''))"],
            capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(out.stdout.strip(), "probe")


class PruneUsesTheRunningInstancesLabelPrefix(unittest.TestCase):
    """gh#311: /api/prune's board_github.py subprocess must see the RUNNING instance's
    FLEET_LABEL_PREFIX, not the bare/absent env the server process itself was started with.

    RED without env=subprocess_env() on that one subprocess.run call: the child inherits this
    process's environment, which per entrypoint.sh only ever carries FLEET_ENV_FILE (a path),
    never fleet.env's values -- so board_github.py's `PREFIX = os.environ.get
    ("FLEET_LABEL_PREFIX", "fleet:")` always falls back to the hardcoded default regardless of
    what this instance actually configures. Two fleet-kit instances sharing a repo (this kit's
    own nonprofit-atlas + philanthropy deployment) would then silently act on each other's
    claimed-label state.

    This drives the real HTTP handler (not just subprocess_env() in isolation, which #307
    already covers) and intercepts subprocess.run so no real `gh` call or network access is
    needed -- only the *env passed to that call* is under test.
    """

    @staticmethod
    def _restore_env(key, value):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self.tmp.write("FLEET_LABEL_PREFIX=custom-test:\n")
        self.tmp.close()
        self.addCleanup(os.unlink, self.tmp.name)

        # addCleanup (not tearDown) for these: it still runs even if setUp raises partway
        # through (e.g. the server bind below), so a failed setUp can never leave
        # FLEET_ENV_FILE/FLEET_LABEL_PREFIX permanently mutated for the rest of the process.
        self.addCleanup(self._restore_env, "FLEET_ENV_FILE", os.environ.get("FLEET_ENV_FILE"))
        self.addCleanup(self._restore_env, "FLEET_LABEL_PREFIX", os.environ.get("FLEET_LABEL_PREFIX"))
        os.environ["FLEET_ENV_FILE"] = self.tmp.name
        # The container state this reproduces: the value exists ONLY in the file.
        os.environ.pop("FLEET_LABEL_PREFIX", None)

        sys.path.insert(0, str(KIT / "scripts"))
        sys.modules.pop("fleet_view_server", None)
        import fleet_view_server as fvs
        self.fvs = fvs

        self.calls = []

        def fake_run(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        # subprocess is a shared module object -- patch.object + addCleanup(patcher.stop)
        # restores fvs.subprocess.run even if a later setUp step (the server bind) raises.
        patcher = unittest.mock.patch.object(fvs.subprocess, "run", side_effect=fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), fvs.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def test_release_subprocess_resolves_the_configured_prefix(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps({"issue": 999, "note": "test release"})
        conn.request("POST", "/api/prune", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()

        self.assertTrue(payload.get("ok"), f"prune call failed: {payload}")
        board_calls = [(cmd, kw) for cmd, kw in self.calls if "board_github.py" in cmd[1]]
        self.assertEqual(len(board_calls), 1, "expected exactly one board_github.py invocation")
        cmd, kwargs = board_calls[0]
        self.assertIn("release", cmd)
        env = kwargs.get("env")
        self.assertIsNotNone(
            env, "no env= passed to the board_github.py subprocess -- it inherits the "
                 "server's bare environment and always falls back to the hardcoded 'fleet:' "
                 "prefix, regardless of this instance's fleet.env")
        self.assertEqual(
            env.get("FLEET_LABEL_PREFIX"), "custom-test:",
            "board_github.py subprocess did not resolve this instance's configured "
            "FLEET_LABEL_PREFIX -- multi-instance deployments would silently collide")


if __name__ == "__main__":
    unittest.main(verbosity=2)
