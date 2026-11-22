"""Redeploys let running coding workers finish: they share agent-runtime's network namespace."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REDEPLOY = (ROOT / "scripts/ops/redeploy.sh").read_text()
PHASE_12 = (ROOT / "scripts/phases/12-compose-up.sh").read_text()
COMMON = (ROOT / "scripts/phases/lib/common.sh").read_text()


class RedeployDrainTests(unittest.TestCase):
    def test_redeploy_drains_before_any_phase_and_holds_the_launcher(self):
        drain = REDEPLOY.index("drain_coding_workers redeploy")
        self.assertLess(drain, REDEPLOY.index("for phase in 10-fetch-secrets"))
        self.assertLess(drain, REDEPLOY.index("export HOLD_CODING_LAUNCHER=1"))
        self.assertIn("trap resume_coding_launcher_on_failure EXIT", REDEPLOY)
        # The last `up` starts the launcher again.
        # The launcher starts after phase 15, which may recreate agent-runtime again.
        self.assertLess(REDEPLOY.index("15-automation-cron.sh"), REDEPLOY.index("HOLD_CODING_LAUNCHER=0 run_docker_compose_as_admin up -d"))
        self.assertLess(REDEPLOY.index("start_stack_launcher_last redeploy"), REDEPLOY.index("15-automation-cron.sh"))

    def test_phase_12_drains_and_respects_the_hold(self):
        self.assertLess(PHASE_12.index("drain_coding_workers"), PHASE_12.index("start_stack_launcher_last"))
        self.assertIn('start_stack_launcher_last "phase:${PHASE_NAME}"', PHASE_12)

    def test_the_launcher_starts_after_agent_runtime_is_healthy(self):
        body = COMMON[COMMON.index("start_stack_launcher_last() {"):]
        body = body[: body.index("\n}\n")]
        self.assertLess(body.index("--scale coding-launcher=0"), body.index("wait_for_agent_runtime_health"))
        self.assertLess(body.index("wait_for_agent_runtime_health"), body.rindex("run_docker_compose_as_admin up -d"))
        self.assertIn('HOLD_CODING_LAUNCHER:-0}" == "1" ]] && return 0', body)

    def test_recreating_services_never_starts_the_launcher_with_agent_runtime(self):
        body = COMMON[COMMON.index("recreate_ts_harness_services() {"):]
        body = body[: body.index("\n}\n")]
        self.assertLess(body.index("drain_coding_workers"), body.index("--scale coding-launcher=0"))
        self.assertLess(body.index("--scale coding-launcher=0"), body.index("wait_for_agent_runtime_health"))

    def test_drain_waits_for_workers_and_can_be_forced(self):
        for needle in (
            'docker ps --filter "name=^ts-coding-"',
            "label=com.docker.compose.service=coding-launcher",
            "CODING_DRAIN_TIMEOUT_MINUTES",
            'FORCE_REDEPLOY:-0}" == "1"',
            "--scale coding-launcher=0",
        ):
            self.assertIn(needle, COMMON)


if __name__ == "__main__":
    unittest.main()
