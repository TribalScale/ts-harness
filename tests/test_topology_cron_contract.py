import re
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_TEMPLATE = REPO_ROOT / "config" / "ts-harness-runtime.yaml.tmpl"
VERIFY_MULTI_AGENT = REPO_ROOT / "scripts" / "ops" / "verify-multi-agent.sh"
AUTOMATION_CRON = REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh"
VARIABLES_TF = REPO_ROOT / "infra" / "aws" / "variables.tf"
EC2_TF = REPO_ROOT / "infra" / "aws" / "ec2.tf"
USER_DATA = REPO_ROOT / "scripts" / "user-data.sh"
AGENTS_INIT = REPO_ROOT / "scripts" / "phases" / "11a-agents-init.sh"


def configured_agent_ids():
    text = RUNTIME_TEMPLATE.read_text()
    replacements = {
        "GATEWAY_TOKEN": "test-token",
        "AGENT_TIMEZONE": "America/Toronto",
        "SLACK_OPS_CHANNEL": "C123",
    }
    for key, value in replacements.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    cfg = yaml.safe_load(text)
    return [agent["id"] for agent in cfg["agents"]["list"]]


class TopologyCronContractTests(unittest.TestCase):
    def test_configured_topology_is_eleven_agents(self):
        self.assertEqual(
            configured_agent_ids(),
            [
                "orchestrator",
                "dev-agent",
                "ops-agent",
                "qa-agent",
                "review-agent",
                "analyst-agent",
                "pm-agent",
                "comms-agent",
                "marketing-agent",
                "sales-agent",
                "builder-agent",
            ],
        )

    def test_verify_multi_agent_expects_all_configured_agents(self):
        script = VERIFY_MULTI_AGENT.read_text()

        self.assertNotIn("expect 7 agents", script)
        self.assertNotIn("Expected 7 agents", script)
        self.assertIn("EXPECTED_AGENTS=(", script)
        for agent_id in configured_agent_ids():
            self.assertIn(agent_id, script)

    def test_verify_multi_agent_checks_all_declared_cron_jobs(self):
        script = VERIFY_MULTI_AGENT.read_text()

        expected = set(re.findall(r'job\("([^"]+)"', AUTOMATION_CRON.read_text()))
        for job in expected:
            self.assertIn(job, script)
        self.assertEqual(len(expected), 14)

    def test_verify_multi_agent_scans_all_agent_workspaces(self):
        script = VERIFY_MULTI_AGENT.read_text()

        self.assertIn(
            'config/ts-harness-runtime.yaml.tmpl',
            script,
        )
        for agent_id in configured_agent_ids():
            self.assertIn(agent_id, script)

    def test_agents_init_materializes_all_configured_agents(self):
        phase = AGENTS_INIT.read_text()

        self.assertNotIn("expected 7 agents", phase)
        self.assertIn("AGENT_IDS=(", phase)
        for agent_id in configured_agent_ids():
            self.assertIn(agent_id, phase)
        self.assertEqual(len(configured_agent_ids()), 11)

    def test_slack_channel_variables_are_wired_through_terraform_and_user_data(
        self,
    ):
        variables = VARIABLES_TF.read_text()
        runtime = (REPO_ROOT / "infra" / "aws" / "runtime_config.tf").read_text()
        user_data = USER_DATA.read_text()

        for var_name, token, env_name in (
            (
                "slack_dev_channel",
                "__TF_SLACK_DEV_CHANNEL__",
                "SLACK_DEV_CHANNEL",
            ),
            (
                "slack_team_channel",
                "__TF_SLACK_TEAM_CHANNEL__",
                "SLACK_TEAM_CHANNEL",
            ),
            (
                "slack_marketing_channel",
                "__TF_SLACK_MARKETING_CHANNEL__",
                "SLACK_MARKETING_CHANNEL",
            ),
            (
                "slack_sales_channel",
                "__TF_SLACK_SALES_CHANNEL__",
                "SLACK_SALES_CHANNEL",
            ),
        ):
            self.assertIn(f'variable "{var_name}"', variables)
            self.assertNotIn(token, user_data)
            self.assertIn(env_name, runtime)

    def test_automation_cron_requires_channel_destinations(self):
        phase = AUTOMATION_CRON.read_text()

        self.assertIn("require_slack_channel_env", phase)
        for variable in (
            "STANDUP_CHAN",
            "OPS_CHAN",
            "DEV_CHAN",
            "TEAM_CHAN",
            "MARKETING_CHAN",
            "SALES_CHAN",
        ):
            self.assertIn(variable, phase)

    def test_automation_cron_writes_runtime_scheduler_config_before_restart(self):
        phase = AUTOMATION_CRON.read_text()

        self.assertIn("runtime-cron-jobs.json", phase)
        self.assertIn("recreate_ts_harness_services", phase)
        self.assertIn("agent-runtime", phase)
        self.assertIn("poll-github-merges", phase)
        self.assertIn('"kind": "system"', phase)
        self.assertIn("poll_github_merges", phase)
        self.assertIn("interval_seconds", phase)
        self.assertLess(
            phase.index('python3 "$MERGE_CRON"'),
            phase.index("Restarting agent-runtime"),
        )

    def test_checked_in_runtime_cron_jobs_include_github_merge_poller(self):
        cron_path = REPO_ROOT / "config" / "runtime-cron-jobs.json"
        data = yaml.safe_load(cron_path.read_text()) if cron_path.exists() else {}
        # Durable default: merge waits must resume even when phase 15 has not
        # regenerated the full host cron list. Phase 15 may still overwrite with
        # the richer standing-order job set.
        jobs = data.get("jobs") or []
        pollers = [
            job
            for job in jobs
            if isinstance(job, dict)
            and job.get("name") == "poll-github-merges"
            and job.get("handler") == "poll_github_merges"
        ]
        self.assertEqual(len(pollers), 1)
        self.assertEqual(pollers[0].get("kind"), "system")
        self.assertEqual(pollers[0].get("interval_seconds"), 60)
        phase = AUTOMATION_CRON.read_text()
        self.assertIn("poll-github-merges", phase)
        self.assertIn("poll_github_merges", phase)
        self.assertIn('"handler": "poll_github_merges"', phase)
        self.assertIn('"interval_seconds": 60', phase)


if __name__ == "__main__":
    unittest.main()
