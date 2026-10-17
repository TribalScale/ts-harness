# flake8: noqa
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY = REPO_ROOT / "scripts" / "ops" / "verify-multi-agent.sh"
ROTATE = REPO_ROOT / "scripts" / "ops" / "rotate-secrets.sh"
SKILL = REPO_ROOT / "skills" / "orchestrator" / "workflow-state.md"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "workflow-runner-smoke-test.md"
ARCH = REPO_ROOT / "docs" / "architecture" / "workflow-engine.md"
PROD = REPO_ROOT / "docs" / "architecture" / "workflow-engine-production-state.md"


class WorkflowRunnerOpsDocsTests(unittest.TestCase):
    def test_verify_multi_agent_checks_runner_when_enabled(self):
        script = VERIFY.read_text()

        self.assertIn("docker-compose.workflow-runner.yml", script)
        self.assertIn("WORKFLOW_RUNNER_ENABLED", script)
        self.assertIn("workflow-runner", script)
        self.assertIn("/workflow/dispatch", script)

    def test_rotate_secrets_recreates_runner(self):
        script = ROTATE.read_text()

        self.assertIn("docker-compose.workflow-runner.yml", script)
        self.assertIn("workflow-runner", script)
        self.assertIn("agent-runtime", script)

    def test_orchestrator_skill_documents_runner_and_plan_approval(self):
        skill = SKILL.read_text()

        self.assertIn("/workflow/plan/propose", skill)
        self.assertIn("/workflow/plan/approve", skill)
        self.assertIn("workflow_plans", skill)
        self.assertIn("manual orchestrator-driven workflows remain valid", skill)

    def test_runner_runbook_contains_manual_approval_flow(self):
        runbook = RUNBOOK.read_text()

        self.assertIn("workflow-runner smoke test", runbook.lower())
        self.assertIn("GET /workflow/plan/<plan_id>", runbook)
        self.assertIn("/workflow/plan/approve", runbook)
        self.assertIn("workflow_plans.materialized_at", runbook)
        self.assertIn("stale lock", runbook.lower())

    def test_architecture_docs_reflect_mvp_and_remaining_future_work(self):
        arch = ARCH.read_text()
        prod = PROD.read_text()

        self.assertIn("mvp runner", arch.lower())
        self.assertIn("workflow_plans", arch)
        self.assertIn("implemented by the MVP", prod)
        self.assertIn("still future", prod)
        self.assertIn("compensation", prod)


if __name__ == "__main__":
    unittest.main()
