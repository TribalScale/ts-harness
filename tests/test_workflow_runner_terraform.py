# flake8: noqa
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VARIABLES = REPO_ROOT / "infra" / "aws" / "variables.tf"
EC2 = REPO_ROOT / "infra" / "aws" / "ec2.tf"
USER_DATA = REPO_ROOT / "scripts" / "user-data.sh"
TFVARS_EXAMPLE = REPO_ROOT / "infra" / "aws" / "terraform.tfvars.example"
TFVARS = REPO_ROOT / "infra" / "aws" / "terraform.tfvars"


class WorkflowRunnerTerraformTests(unittest.TestCase):
    def test_variables_define_runner_defaults(self):
        variables = VARIABLES.read_text()

        self.assertIn('variable "workflow_runner_enabled"', variables)
        self.assertIn("default     = false", variables)
        self.assertIn('variable "workflow_runner_poll_interval_seconds"', variables)
        self.assertIn("default     = 10", variables)
        self.assertIn('variable "workflow_runner_batch_size"', variables)
        self.assertIn("default     = 5", variables)
        self.assertIn('variable "workflow_runner_lease_seconds"', variables)
        self.assertIn("default     = 1800", variables)
        self.assertIn('variable "workflow_runner_stale_policy"', variables)
        self.assertIn('contains(["block", "fail"], var.workflow_runner_stale_policy)', variables)

    def test_user_data_placeholders_are_substituted_and_exported(self):
        user_data = USER_DATA.read_text()
        ec2 = EC2.read_text()
        runtime = (REPO_ROOT / "infra" / "aws" / "runtime_config.tf").read_text()

        for token in (
            "__TF_WORKFLOW_RUNNER_ENABLED__",
            "__TF_WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS__",
            "__TF_WORKFLOW_RUNNER_BATCH_SIZE__",
            "__TF_WORKFLOW_RUNNER_LEASE_SECONDS__",
            "__TF_WORKFLOW_RUNNER_STALE_POLICY__",
        ):
            self.assertNotIn(token, user_data)
            self.assertNotIn(token, ec2)
        self.assertIn("WORKFLOW_RUNNER_ENABLED", runtime)
        self.assertIn("WORKFLOW_RUNNER_STALE_POLICY", runtime)

    def test_tfvars_document_runner_without_enabling_live_env(self):
        example = TFVARS_EXAMPLE.read_text()

        self.assertIn("workflow_runner_enabled", example)
        self.assertIn("requires multi_agent_enabled = true", example)
        if TFVARS.is_file():
            live = TFVARS.read_text()
            self.assertIn("# workflow_runner_enabled = false", live)


if __name__ == "__main__":
    unittest.main()
