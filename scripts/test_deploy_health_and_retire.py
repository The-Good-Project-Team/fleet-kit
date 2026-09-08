"""Regression test for deploy.sh (gh#672 AC5/AC6).

No podman/caddy in this sandbox, so this checks the two fixes at the source level, the same
way AC3 in the issue's own PRD is verified ("reading the locking code after the change"):

AC5: HEALTH_TIMEOUT_S defaults to >=120s (was 30s, which false-negatived a genuinely-healthy
green build under real load on 2026-09-07 and then tore down a container still serving
in-flight passes, fk#671).

AC6: proxy_deploy() no longer stops a still-running previous retired build to free its ports
before green has been health-checked -- it abandons that deploy tick instead, and only removes
the (already-stopped) retired container after green is proven healthy.

Run: python3 scripts/test_deploy_health_and_retire.py
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
DEPLOY = (KIT / "scripts" / "deploy.sh").read_text()


class HealthTimeoutTests(unittest.TestCase):
    def test_default_is_at_least_120(self):
        m = re.search(r'HEALTH_TIMEOUT_S="\$\{FLEET_HEALTH_TIMEOUT_S:-(\d+)\}"', DEPLOY)
        self.assertIsNotNone(m, "could not find HEALTH_TIMEOUT_S default assignment")
        self.assertGreaterEqual(int(m.group(1)), 120)


class ProxyDeployRetireOrderTests(unittest.TestCase):
    def _proxy_deploy_body(self) -> str:
        start = DEPLOY.index("proxy_deploy() {")
        # proxy_deploy is the only top-level function starting in column 0 after this point.
        end = DEPLOY.index("\n}\n", start)
        return DEPLOY[start:end]

    def test_does_not_stop_retired_before_green_is_built(self):
        body = self._proxy_deploy_body()
        build_pos = body.index("podman build")
        before_build = body[:build_pos]
        self.assertNotIn("podman stop", before_build,
                          "proxy_deploy stops a container before the green build even starts")

    def test_abandons_when_retired_still_running(self):
        body = self._proxy_deploy_body()
        self.assertIn("return 1", body.split("running \"$RETIRED_MARKER\"", 1)[1][:400])

    def test_removes_retired_only_after_health_check_passes(self):
        body = self._proxy_deploy_body()
        health_pos = body.index("health_check \"$nv\"")
        rm_positions = [m.start() for m in re.finditer(r'podman rm -f "\$RETIRED_MARKER"', body)]
        self.assertTrue(rm_positions, "no removal of $RETIRED_MARKER found in proxy_deploy")
        self.assertTrue(all(p > health_pos for p in rm_positions),
                         "RETIRED_MARKER is removed before green's health check runs")


if __name__ == "__main__":
    unittest.main()
