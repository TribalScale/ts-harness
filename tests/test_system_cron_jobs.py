"""Every system job in config/runtime-cron-jobs.json is also in phase 15's list.

Phase 15 is the only path that puts system jobs into the live cron list on hosts that keep state
in Postgres; a job only in the JSON file never runs there (retry-program-phases did not).
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def phase_15_jobs() -> dict[str, dict]:
    text = (REPO / "scripts" / "phases" / "15-automation-cron.sh").read_text()
    names = re.findall(r'^\s+"name": "([a-z0-9-]+)",\n\s+"kind": "system",', text, re.M)
    handlers = dict(re.findall(r'"name": "([a-z0-9-]+)",\n\s+"kind": "system",\n\s+"handler": "([a-z_]+)"', text))
    intervals = dict(re.findall(r'"handler": "([a-z_]+)",\n\s+"interval_seconds": (\d+)', text))
    return {name: {"handler": handlers.get(name), "interval_seconds": int(intervals.get(handlers.get(name), 0))} for name in names}


class SystemCronJobTests(unittest.TestCase):
    def test_json_system_jobs_are_in_phase_15(self):
        jobs = json.loads((REPO / "config" / "runtime-cron-jobs.json").read_text())["jobs"]
        system = {j["name"]: j for j in jobs if j.get("kind") == "system"}
        phase = phase_15_jobs()
        self.assertTrue(system)
        for name, job in system.items():
            self.assertIn(name, phase, f"{name} is in runtime-cron-jobs.json but not in phase 15")
            self.assertEqual(phase[name]["handler"], job["handler"], name)
            self.assertEqual(phase[name]["interval_seconds"], job["interval_seconds"], name)


if __name__ == "__main__":
    unittest.main()
