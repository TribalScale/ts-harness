"""When several scenarios tag the same design frame, the ticket's own scenario is captured."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ts_harness.implementation import test_services as ts


class OwnScenarioTests(unittest.TestCase):
    def test_the_work_orders_own_scenario_wins_over_another_tickets(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, out = Path(tmp) / "repo", Path(tmp) / "out"
            repo.mkdir()
            shots = {}
            for name in ("other.png", "own.png"):
                shots[name] = Path(tmp) / name
                shots[name].write_bytes(name.encode())
            tests = [
                {"title": "features/PROJ-48-app-shell.feature.spec.js App shell D1 Week, base @frame-D1", "status": "passed",
                 "screenshot": str(shots["other.png"])},
                {"title": "features/PROJ-53-week-header.feature.spec.js Week header D1 Week, base @frame-D1", "status": "passed",
                 "screenshot": str(shots["own.png"])},
            ]
            with patch.object(ts, "playwright_config", return_value=repo / "playwright.config.ts"), \
                    patch("ts_harness.implementation.workspaces.find_bin", return_value=Path("/bin/true")), \
                    patch.object(ts, "uses_playwright_bdd", return_value=False), \
                    patch.object(ts, "_shell", return_value=(0, "")), \
                    patch.object(ts, "_frames_config_text", return_value=""), \
                    patch.object(ts, "_report_tests", return_value=tests), \
                    patch("ts_harness.implementation.test_services.json.loads", return_value={}), \
                    patch.object(Path, "read_text", return_value="{}"):
                captures, _ = ts.capture_frames(repo, [{"id": "D1", "title": "Week", "width": 1440, "height": 900}], out, {},
                                                work_order_id="PROJ-53")
            self.assertEqual(captures[0]["status"], "captured")
            self.assertIn("PROJ-53", captures[0]["scenario"])
            self.assertEqual(Path(captures[0]["file"]).read_bytes(), b"own.png")


if __name__ == "__main__":
    unittest.main()
