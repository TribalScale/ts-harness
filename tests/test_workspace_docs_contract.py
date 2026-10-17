import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
COMPATIBILITY = REPO_ROOT / "docs" / "compatibility.md"
AUTOMATION_SMOKE = REPO_ROOT / "docs" / "runbooks" / "automation-smoke-test.md"
SLACK_SMOKE = (
    REPO_ROOT / "docs" / "runbooks" / "slack-multi-agent-smoke-test.md"
)
RERUN_PHASE = REPO_ROOT / "docs" / "runbooks" / "re-run-bootstrap-phase.md"
GATEWAY_AUTH = REPO_ROOT / "docs" / "runbooks" / "gateway-auth-debugging.md"
CRON_DEBUG = REPO_ROOT / "docs" / "runbooks" / "cron-registration-debugging.md"
ROOT_AGENTS = REPO_ROOT / "workspace" / "AGENTS.md"
ROOT_RULES = REPO_ROOT / "workspace" / "RULES.md"
SALES_AGENTS = REPO_ROOT / "workspace" / "sales-agent" / "AGENTS.md"
OPS_AGENTS = REPO_ROOT / "workspace" / "ops-agent" / "AGENTS.md"
AGENTS_INIT = REPO_ROOT / "scripts" / "phases" / "11a-agents-init.sh"


class WorkspaceDocsContractTests(unittest.TestCase):
    def test_public_docs_use_current_agent_topology(self):
        for path in (README, COMPATIBILITY, AUTOMATION_SMOKE, ROOT_AGENTS):
            text = path.read_text()
            with self.subTest(path=path):
                self.assertNotIn("6 specialist", text)
                self.assertNotIn("six specialist", text.lower())
                self.assertNotIn("exactly 7 agents", text)
                self.assertIn("marketing-agent", text)
                self.assertIn("sales-agent", text)

    def test_compatibility_lists_14_crons(self):
        text = COMPATIBILITY.read_text()

        self.assertIn("all 14 declared cron jobs", text)
        self.assertIn("stale-deal-alert", text)

    def test_automation_smoke_lists_all_14_cron_jobs(self):
        text = AUTOMATION_SMOKE.read_text()

        self.assertIn("Expected: all 14 jobs", text)
        for job in (
            "weekly-content-digest",
            "weekly-pipeline-report",
            "stale-deal-alert",
        ):
            self.assertIn(job, text)

    def test_specialist_workspaces_receive_memory_guidance(self):
        phase = AGENTS_INIT.read_text()

        self.assertIn('ROOT_MEMORY="$RENDERED_WORKSPACE_DIR/MEMORY.md"', phase)
        self.assertIn('cp "$ROOT_MEMORY" "$workspace_dir/MEMORY.md"', phase)

    def test_sales_agents_ends_at_the_generated_block(self):
        text = SALES_AGENTS.read_text()

        self.assertTrue(text.rstrip().endswith("<!-- END GENERATED: agent-identity -->"))
        self.assertNotIn(
            "Post pipeline updates to `{{SALES_CHANNEL}}` [auto]",
            text,
        )

    def test_workspace_agents_have_no_prose_after_generated_block(self):
        paths = [ROOT_AGENTS, *sorted((REPO_ROOT / "workspace").glob("*/AGENTS.md"))]
        self.assertGreater(len(paths), 1)
        for path in paths:
            text = path.read_text()
            with self.subTest(path=path):
                self.assertIn("<!-- END GENERATED: agent-identity -->", text)
                self.assertTrue(text.rstrip().endswith("<!-- END GENERATED: agent-identity -->"))

    def test_orchestrator_skills_do_not_point_at_agents_md(self):
        for relative in (
            "skills/orchestrator/route-task.md",
            "skills/orchestrator/fan-out.md",
            "skills/orchestrator/synthesize.md",
        ):
            text = (REPO_ROOT / relative).read_text()
            with self.subTest(path=relative):
                self.assertNotIn("AGENTS.md", text)
        route = (REPO_ROOT / "skills/orchestrator/route-task.md").read_text()
        self.assertIn("retry with refined instructions", route)
        self.assertIn("classifies intent", route)

    def test_staging_deploy_policy_requires_explicit_request(self):
        rule_files = [ROOT_RULES, OPS_AGENTS]
        rule_files.extend((REPO_ROOT / "workspace").glob("*/RULES.md"))
        for path in rule_files:
            text = path.read_text()
            with self.subTest(path=path):
                self.assertNotIn("Automated, no approval needed", text)

    def test_runbooks_do_not_hardcode_old_phase_rerun_version(self):
        for path in (RERUN_PHASE, GATEWAY_AUTH, CRON_DEBUG):
            text = path.read_text()
            with self.subTest(path=path):
                self.assertNotIn("AGENT_RUNTIME_VERSION=v2026.4.14", text)
                self.assertTrue("AGENT_RUNTIME_VERSION=" in text or "AGENT_RUNTIME_VERSION=" in text)

    def test_slack_smoke_is_in_runbook_catalog_guidance(self):
        text = SLACK_SMOKE.read_text()

        self.assertIn("Slack Multi-Agent Smoke Test", text)


if __name__ == "__main__":
    unittest.main()
