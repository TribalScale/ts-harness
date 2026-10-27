# flake8: noqa
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker" / "docker-compose.workflow-runner.yml"
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.workflow-runner"
COMPOSE_PHASE = REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh"
COMPOSE_COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"
CODING_JOBS_COMPOSE = REPO_ROOT / "docker" / "docker-compose.coding-jobs.yml"
CODING_WORKER_DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.coding-worker"


class WorkflowRunnerComposeTests(unittest.TestCase):
    def test_workflow_runner_compose_is_optional_sidecar(self):
        compose = COMPOSE.read_text()

        self.assertIn("workflow-runner:", compose)
        self.assertIn("build:", compose)
        self.assertIn("context: ${ENTERPRISE_ASSETS_DIR:?set ENTERPRISE_ASSETS_DIR}", compose)
        self.assertIn("dockerfile: docker/Dockerfile.workflow-runner", compose)
        self.assertIn('network_mode: "service:agent-runtime"', compose)
        self.assertIn("AGENT_RUNTIME_URL:", compose)
        self.assertIn("TS_HARNESS_ROOT:", compose)
        self.assertIn("TS_HARNESS_CONFIG:", compose)
        self.assertIn("ts-harness-runtime.yaml:/config/ts-harness-runtime.yaml:ro", compose)
        self.assertIn("single-replica only; do not scale", compose)
        self.assertIn("restart: unless-stopped", compose)

    def test_workflow_runner_env_and_healthcheck_are_wired(self):
        compose = COMPOSE.read_text()

        for name in (
            "WORKFLOW_RUNNER_ENABLED",
            "WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS",
            "WORKFLOW_RUNNER_BATCH_SIZE",
            "WORKFLOW_RUNNER_LEASE_SECONDS",
            "WORKFLOW_RUNNER_DISPATCH_TIMEOUT_SECONDS",
            "WORKFLOW_RUNNER_STALE_POLICY",
            "MEMORY_API_URL",
            "WORKFLOW_RUNNER_ADAPTER",
            "GATEWAY_TOKEN",
            "TS_HARNESS_SESSION_DB_URL",
            "TS_HARNESS_WORKFLOW_DEFINITION_STORE",
            "TS_HARNESS_MANAGED_WORKFLOWS_DIR",
            "TS_HARNESS_PROGRAM_DEFINITION_STORE",
        ):
            self.assertIn(f"{name}:", compose)
        self.assertIn("healthcheck:", compose)
        self.assertIn("/tmp/workflow-runner.tick", compose)
        self.assertIn('command: ["python3", "/app/scripts/workflow-runner.py"]', compose)
        self.assertNotIn("/app/workflow-runner.py", compose)

    def test_workflow_runner_gets_only_the_github_token_for_merge_readiness(self):
        compose = COMPOSE.read_text()
        common = COMPOSE_COMMON.read_text()

        self.assertIn("GITHUB_TOKEN: ${GITHUB_TOKEN:-}", compose)
        self.assertNotIn("env_file:", compose)
        self.assertIn('GITHUB_TOKEN="${GITHUB_TOKEN:-}"', common)

    def test_workflow_runner_mounts_managed_definitions_read_only(self):
        compose = COMPOSE.read_text()

        self.assertIn(
            "ts-harness-workflow-definitions:/workflow-definitions:ro",
            compose,
        )
        self.assertIn(
            "TS_HARNESS_WORKFLOW_DEFINITION_STORE: /workflow-definitions",
            compose,
        )
        self.assertIn(
            "TS_HARNESS_MANAGED_WORKFLOWS_DIR: /workflow-definitions/active",
            compose,
        )
        self.assertIn(
            "TS_HARNESS_PROGRAM_DEFINITION_STORE: /workflow-definitions/programs",
            compose,
        )
        self.assertIn(
            "TS_HARNESS_FRAGMENT_DEFINITION_STORE: /workflow-definitions/fragments",
            compose,
        )
        self.assertIn("/config/fragments:/config/fragments:ro", compose)

    def test_workflow_runner_dockerfile_copies_fragments(self):
        dockerfile = DOCKERFILE.read_text()

        self.assertIn("COPY scripts/workflow_fragments.py ./scripts/workflow_fragments.py", dockerfile)
        self.assertIn("COPY config/fragments /app/config/fragments", dockerfile)

    def test_workflow_runner_dockerfile_copies_runner_and_definitions(self):
        dockerfile = DOCKERFILE.read_text()

        self.assertIn("COPY scripts/workflow-runner.py ./scripts/workflow-runner.py", dockerfile)
        self.assertIn("COPY scripts/workflow_definitions.py ./scripts/workflow_definitions.py", dockerfile)
        self.assertIn("COPY scripts/program_definitions.py ./scripts/program_definitions.py", dockerfile)
        self.assertIn("COPY scripts/program_legacy_compat.py ./scripts/program_legacy_compat.py", dockerfile)
        self.assertIn("COPY config/workflows /app/config/workflows", dockerfile)
        self.assertIn("COPY config/programs /app/config/programs", dockerfile)
        self.assertIn("TS_HARNESS_ROOT=/app", dockerfile)
        self.assertIn('CMD ["python3", "/app/scripts/workflow-runner.py"]', dockerfile)

    def test_compose_phase_includes_runner_before_fingerprint(self):
        phase = COMPOSE_PHASE.read_text()
        common = COMPOSE_COMMON.read_text()
        runner_idx = phase.index("docker-compose.workflow-runner.yml")
        fingerprint_idx = phase.index("PHASE_FINGERPRINT=")

        self.assertLess(runner_idx, fingerprint_idx)
        # Defaults are applied in run_docker_compose_as_admin; phase persists them.
        self.assertIn(
            'WORKFLOW_RUNNER_ENABLED="${WORKFLOW_RUNNER_ENABLED:-false}"',
            common,
        )
        self.assertIn("WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS", phase)
        self.assertIn("WORKFLOW_RUNNER_LEASE_SECONDS", phase)
        self.assertIn("WORKFLOW_RUNNER_STALE_POLICY", phase)
        self.assertIn(
            'export WORKFLOW_RUNNER_ENABLED=$(printf \'%q\' "${WORKFLOW_RUNNER_ENABLED:-false}")',
            phase,
        )
        # Recreating agent-runtime gives it a new network namespace. The runner joins that
        # namespace, so the recreate helper has to include it or the mirror sync gets connection refused.
        self.assertIn("docker-compose.workflow-runner.yml", common)
        self.assertIn("printf '%s\\n' workflow-runner", common)


class CodingJobsComposeTests(unittest.TestCase):
    def test_launcher_and_worker_image_are_wired(self):
        compose = CODING_JOBS_COMPOSE.read_text()
        self.assertIn("coding-launcher:", compose)
        self.assertIn('command: ["python3", "/app/scripts/coding-launcher.py"]', compose)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", compose)
        self.assertIn('network_mode: "service:agent-runtime"', compose)
        self.assertIn("CODING_WORKER_NETWORK: auto", compose)
        self.assertIn("CODING_MAX_RUNNING: ${CODING_MAX_RUNNING:-3}", compose)
        self.assertIn("ts-harness-runtime.yaml", compose)
        self.assertIn("dockerfile: docker/Dockerfile.coding-worker", compose)

    def test_launcher_follows_the_profile_and_gets_ecs_settings(self):
        compose = CODING_JOBS_COMPOSE.read_text()
        self.assertIn("CODING_LAUNCHER: ${CODING_LAUNCHER:-}", compose)
        self.assertIn("DEPLOYMENT_PROFILE: ${DEPLOYMENT_PROFILE:-single_host}", compose)
        common = COMPOSE_COMMON.read_text()
        phase = COMPOSE_PHASE.read_text()
        for key in ("CODING_ECS_CLUSTER", "CODING_ECS_TASK_DEFINITION", "CODING_ECS_SUBNETS", "CODING_ECS_SECURITY_GROUPS"):
            self.assertIn(f"{key}: ${{{key}:-}}", compose)
            self.assertIn(f'{key}="${{{key}:-}}"', common)
            self.assertIn(f"export {key}=", phase)

    def test_worker_image_bundles_opencode_and_runs_the_worker(self):
        dockerfile = CODING_WORKER_DOCKERFILE.read_text()
        self.assertIn("FROM ${AGENT_RUNTIME_IMAGE}", dockerfile)
        self.assertIn("opencode-ai@${OPENCODE_VERSION}", dockerfile)
        self.assertIn("/opt/tools/opencode/current", dockerfile)
        self.assertIn('CMD ["python3", "/app/scripts/coding-worker.py"]', dockerfile)

    def test_flag_reaches_the_runner_and_compose_phase(self):
        self.assertIn("CODING_JOBS_ENABLED: ${CODING_JOBS_ENABLED:-false}", COMPOSE.read_text())
        phase = COMPOSE_PHASE.read_text()
        self.assertLess(phase.index("docker-compose.coding-jobs.yml"), phase.index("PHASE_FINGERPRINT="))
        self.assertIn('CODING_JOBS_ENABLED="${CODING_JOBS_ENABLED:-false}"', COMPOSE_COMMON.read_text())

if __name__ == "__main__":
    unittest.main()
