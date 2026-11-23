"""Choosing where coding jobs run (local containers or Fargate) and the host wiring for ECS workers."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"
PHASE_12 = REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh"
REDEPLOY = REPO_ROOT / "scripts" / "ops" / "redeploy.sh"
PUSH = REPO_ROOT / "scripts" / "ops" / "push-coding-worker-image.sh"
SWITCH = REPO_ROOT / "scripts" / "ops" / "coding-launcher.sh"
CODING_JOBS_COMPOSE = REPO_ROOT / "docker" / "docker-compose.coding-jobs.yml"


def _function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    return text[start:text.index("\n}\n", start) + 3]


def _resolve(data_dir: str | None = None, override: str | None = None, **env: str) -> str:
    common = COMMON.read_text()
    body = "\n".join(_function(common, n) for n in ("coding_launcher_override_path", "resolve_coding_launcher"))
    if data_dir and override is not None:
        setup = Path(data_dir) / ".setup"
        setup.mkdir(parents=True, exist_ok=True)
        (setup / "coding-launcher.override").write_text(override)
    if data_dir:
        env = {"DATA_DIR": data_dir, **env}
    assignments = " ".join(f"{k}={v!r}" for k, v in env.items())
    script = f"{body}\n{assignments} resolve_coding_launcher"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout


class ResolveLauncherTests(unittest.TestCase):
    def test_profile_default(self):
        self.assertEqual(_resolve(), "local")
        self.assertEqual(_resolve(DEPLOYMENT_PROFILE="scale_out"), "ecs")

    def test_runtime_config_wins_over_profile(self):
        self.assertEqual(_resolve(CODING_LAUNCHER="ecs"), "ecs")
        self.assertEqual(_resolve(CODING_LAUNCHER="LOCAL", DEPLOYMENT_PROFILE="scale_out"), "local")

    def test_host_override_wins_over_runtime_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_resolve(tmp, "local\n", CODING_LAUNCHER="ecs"), "local")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_resolve(tmp, "ecs", CODING_LAUNCHER="local"), "ecs")

    def test_unknown_values_fall_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_resolve(tmp, "fargate", CODING_LAUNCHER="ecs"), "ecs")
        self.assertEqual(_resolve(CODING_LAUNCHER="__PLACEHOLDER__"), "local")


class HostWiringTests(unittest.TestCase):
    def test_phase_12_uses_the_resolved_launcher(self):
        phase = PHASE_12.read_text()
        resolved = phase.index('CODING_LAUNCHER_RESOLVED="$(resolve_coding_launcher)"')
        self.assertLess(resolved, phase.index('export CODING_LAUNCHER="$CODING_LAUNCHER_RESOLVED"'))
        self.assertIn('echo "export CODING_LAUNCHER=$(printf \'%q\' "${CODING_LAUNCHER_RESOLVED}")"', phase)
        self.assertIn('"$CODING_FINGERPRINT"', phase)
        for name in ("CODING_LAUNCHER_RESOLVED", "CODING_MAX_RUNNING", "CODING_ECS_SUBNETS", "CODING_ECS_ASSIGN_PUBLIC_IP"):
            self.assertIn(name, phase[phase.index("CODING_FINGERPRINT="):phase.index("PHASE_FINGERPRINT=")])

    def test_memory_api_is_published_whenever_the_fargate_stack_exists(self):
        phase = PHASE_12.read_text()
        gate = phase.index('if [[ "${DEPLOYMENT_PROFILE:-single_host}" != "scale_out" ]] && ! is_blank_or_placeholder "${CODING_ECS_CLUSTER:-}"; then')
        self.assertLess(gate, phase.index("PHASE_FINGERPRINT="))
        self.assertIn("docker-compose.scale-out.yml", phase[gate:gate + 800])

    def test_ecs_without_a_cluster_stops_the_deploy(self):
        phase = PHASE_12.read_text()
        self.assertIn('if [[ "$CODING_LAUNCHER_RESOLVED" == "ecs" ]] && is_blank_or_placeholder "${CODING_ECS_CLUSTER:-}"; then', phase)

    def test_public_ip_flag_reaches_the_launcher(self):
        self.assertIn("CODING_ECS_ASSIGN_PUBLIC_IP: ${CODING_ECS_ASSIGN_PUBLIC_IP:-false}", CODING_JOBS_COMPOSE.read_text())
        compose = _function(COMMON.read_text(), "run_docker_compose_as_admin")
        self.assertIn('CODING_ECS_ASSIGN_PUBLIC_IP="${CODING_ECS_ASSIGN_PUBLIC_IP:-false}"', compose)
        self.assertIn('echo "export CODING_ECS_ASSIGN_PUBLIC_IP=', PHASE_12.read_text())

    def test_redeploy_drains_ecs_jobs_too(self):
        common = COMMON.read_text()
        self.assertIn("running_ecs_coding_jobs", _function(common, "running_coding_workers"))
        ecs = _function(common, "running_ecs_coding_jobs")
        self.assertIn('"arn:aws:ecs:%"', ecs)
        self.assertIn('("starting", "running"', ecs)
        self.assertIn("|| true", ecs)


class ImagePushTests(unittest.TestCase):
    def test_redeploy_pushes_after_the_build_and_before_up(self):
        redeploy = REDEPLOY.read_text()
        build = redeploy.index("run_docker_compose_as_admin build\n")
        push = redeploy.index('bash "$OPS_DIR/push-coding-worker-image.sh"')
        self.assertLess(build, push)
        self.assertLess(push, redeploy.index("HOLD_CODING_LAUNCHER=0 run_docker_compose_as_admin up -d"))

    def test_push_is_a_no_op_without_a_repository_and_keeps_the_login_off_disk(self):
        push = PUSH.read_text()
        self.assertIn('if is_blank_or_placeholder "$repository"; then', push)
        self.assertIn('DOCKER_CONFIG="$(mktemp -d)"', push)
        self.assertIn("--password-stdin", push)
        self.assertNotIn("set -x", push)


class SwitchScriptTests(unittest.TestCase):
    def test_switch_waits_for_idle_twice_and_rewrites_compose(self):
        switch = SWITCH.read_text()
        idle = _function(switch, "wait_until_idle")
        self.assertEqual(idle.count("coding_jobs_active"), 2)
        self.assertIn("sleep 60", idle)
        self.assertIn('bash "$PHASES_DIR/12-compose-up.sh"', switch)
        self.assertIn('"queued", "starting", "running"', switch)

    def test_usage_errors(self):
        for script in (SWITCH, PUSH):
            subprocess.run(["bash", "-n", str(script)], check=True)


if __name__ == "__main__":
    unittest.main()
