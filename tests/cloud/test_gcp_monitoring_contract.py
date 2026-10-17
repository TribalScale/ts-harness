"""Assert GCP monitoring alert/metric names align with AWS monitoring contract."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GCP_MONITORING = REPO_ROOT / "infra" / "gcp" / "monitoring.tf"
AWS_MONITORING = REPO_ROOT / "infra" / "aws" / "monitoring.tf"
GCP_BUDGET = REPO_ROOT / "infra" / "gcp" / "budget.tf"

SHARED_METRICS = (
    "GatewayHealthy",
    "WorkflowRunnerHealthy",
    "WorkflowTasksPending",
)


class GcpMonitoringContractTests(unittest.TestCase):
    def test_monitoring_files_exist(self):
        self.assertTrue(GCP_MONITORING.is_file())
        self.assertTrue(AWS_MONITORING.is_file())

    def test_metric_names_match_aws(self):
        gcp = GCP_MONITORING.read_text()
        aws = AWS_MONITORING.read_text()
        for metric in SHARED_METRICS:
            self.assertIn(metric, gcp, msg=f"GCP missing metric {metric}")
            self.assertIn(f'metric_name         = "{metric}"', aws)
            self.assertIn(f"/{metric}", gcp)

    def test_namespace_parity(self):
        gcp = GCP_MONITORING.read_text()
        aws = AWS_MONITORING.read_text()
        self.assertIn("TSHarness", gcp)
        self.assertIn('namespace           = "TSHarness"', aws)

    def test_email_notification_channel(self):
        gcp = GCP_MONITORING.read_text()
        self.assertIn("google_monitoring_notification_channel", gcp)
        self.assertIn('type         = "email"', gcp)

    def test_alert_policy_names_aligned(self):
        gcp = GCP_MONITORING.read_text()
        for suffix in (
            "gateway-unhealthy",
            "workflow-runner-unhealthy",
            "workflow-tasks-pending",
        ):
            self.assertIn(suffix, gcp)

    def test_budget_documents_billing_account(self):
        budget = GCP_BUDGET.read_text()
        self.assertIn("billing_account", budget)
        self.assertIn("google_billing_budget", budget)
        self.assertIn("google-beta", budget)
        self.assertIn("threshold_percent", budget)


if __name__ == "__main__":
    unittest.main()
