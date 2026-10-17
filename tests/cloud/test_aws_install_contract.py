"""Freeze AWS install contracts after terraform/ → infra/aws move."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from install_contract import normalize_install_outputs

REPO_ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = REPO_ROOT / "infra" / "aws"
OUTPUTS_TF = TERRAFORM / "outputs.tf"
SECRETS_TF = TERRAFORM / "secrets.tf"
RUNTIME_CONFIG_TF = TERRAFORM / "runtime_config.tf"
IAM_TF = TERRAFORM / "iam.tf"
EC2_TF = TERRAFORM / "ec2.tf"
VARIABLES_TF = TERRAFORM / "variables.tf"
MONITORING_TF = TERRAFORM / "monitoring.tf"
EBS_TF = TERRAFORM / "ebs.tf"
USER_DATA = REPO_ROOT / "scripts" / "user-data.sh"
FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "existing_aws_install_metadata.json"
)

FROZEN_OUTPUTS = (
    "instance_id",
    "elastic_ip",
    "ebs_volume_id",
    "secret_arns",
    "agent_credentials_kms_key_arn",
    "agent_credentials_kms_key_alias",
    "rds_address",
    "rds_endpoint",
    "rds_master_secret_arn",
    "ssh_command",
)

NORMALIZED_OUTPUTS = (
    "cloud_provider",
    "cloud_region",
    "compute_resource_id",
    "data_disk_resource_id",
    "data_disk_device_hint",
    "secret_prefix",
    "managed_db_host",
    "managed_db_port",
    "managed_db_name",
    "managed_db_user",
    "managed_db_credential_ref",
    "credentials_encryption_key_id",
    "runtime_private_url",
    "operator_connection_command",
    "monitoring_namespace",
    "notification_channel_id",
)

FROZEN_TF_TOKENS = (
    "__TF_ADMIN_USER__",
    "__TF_AGENT_RUNTIME_VERSION__",
    "__TF_AWS_REGION__",
    "__TF_CLOUD_PROVIDER__",
    "__TF_CLOUD_REGION__",
    "__TF_SECRETS_PREFIX__",
    "__TF_ENTERPRISE_REPO__",
    "__TF_MEMORY_BACKEND__",
    "__TF_RDS_ENDPOINT__",
    "__TF_RDS_SECRET_ARN__",
    "__TF_RDS_DB_NAME__",
    "__TF_MANAGED_DB_HOST__",
    "__TF_MANAGED_DB_PORT__",
    "__TF_MANAGED_DB_NAME__",
    "__TF_MANAGED_DB_USER__",
    "__TF_MANAGED_DB_CREDENTIAL_REF__",
    "__TF_CREDENTIALS_ENCRYPTION_KEY_ID__",
    "__TF_INSTANCE_RESOURCE_ID__",
)

RUNTIME_CONFIG_USERDATA_TOKENS = (
    "__TF_MULTI_AGENT_ENABLED__",
    "__TF_AGENT_TIMEZONE__",
    "__TF_SLACK_STANDUP_CHANNEL__",
    "__TF_SLACK_OPS_CHANNEL__",
    "__TF_SLACK_DEV_CHANNEL__",
    "__TF_SLACK_TEAM_CHANNEL__",
    "__TF_SLACK_MARKETING_CHANNEL__",
    "__TF_SLACK_SALES_CHANNEL__",
    "__TF_APPROVED_SLACK_CHANNELS__",
    "__TF_EMBEDDINGS_PROVIDER__",
    "__TF_EMBEDDINGS_MODEL__",
    "__TF_EMBEDDINGS_DIMENSIONS__",
    "__TF_EMBEDDINGS_FALLBACK__",
    "__TF_WORKFLOW_RUNNER_ENABLED__",
    "__TF_WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS__",
    "__TF_WORKFLOW_RUNNER_BATCH_SIZE__",
    "__TF_WORKFLOW_RUNNER_LEASE_SECONDS__",
    "__TF_WORKFLOW_RUNNER_STALE_POLICY__",
    "__TF_IMPLEMENT_WORK_ORDERS_REPO_URL__",
    "__TF_IMPLEMENT_WORK_ORDERS_BASE_BRANCH__",
    "__TF_IMPLEMENT_WORK_ORDERS_OPENCODE_VERSION__",
    "__TF_IDENTITY_PROVIDER__",
    "__TF_OIDC_ISSUER__",
    "__TF_OIDC_AUDIENCE__",
    "__TF_OIDC_JWKS_URL__",
    "__TF_OIDC_CLIENT_ID__",
    "__TF_OIDC_AUTHORIZATION_ENDPOINT__",
    "__TF_OIDC_TOKEN_ENDPOINT__",
)

FROZEN_SECRET_KEYS = (
    "anthropic-api-key",
    "db-password",
    "gateway-token",
    "agent-credentials-kms-key-id",
)


class AwsInstallContractFreezeTests(unittest.TestCase):
    def test_aws_root_lives_under_infra_aws(self):
        self.assertTrue(TERRAFORM.is_dir())
        self.assertTrue((TERRAFORM / "main.tf").is_file())
        self.assertTrue((REPO_ROOT / "terraform" / "README.md").is_file())
        redirect = (REPO_ROOT / "terraform" / "README.md").read_text()
        self.assertIn("infra/aws", redirect)

    def test_frozen_outputs_present(self):
        text = OUTPUTS_TF.read_text()
        for name in FROZEN_OUTPUTS:
            self.assertIn(f'output "{name}"', text)
        for name in NORMALIZED_OUTPUTS:
            self.assertIn(f'output "{name}"', text)
        self.assertIn("aws_instance.harness.id", text)
        self.assertIn("aws_eip.harness.public_ip", text)
        self.assertIn("ssh -p 2222", text)

    def test_secret_name_pattern_uses_project_prefix(self):
        secrets = SECRETS_TF.read_text()
        self.assertIn('name     = "${var.project_name}/${each.key}"', secrets)
        for key in FROZEN_SECRET_KEYS:
            self.assertIn(f'"{key}"', secrets)

    def test_runtime_config_parameter_and_iam(self):
        runtime = RUNTIME_CONFIG_TF.read_text()
        iam = IAM_TF.read_text() + runtime
        secrets = SECRETS_TF.read_text()
        self.assertTrue(RUNTIME_CONFIG_TF.is_file())
        self.assertIn('name        = "/${var.project_name}/runtime-config"', runtime)
        self.assertIn("aws_ssm_parameter", runtime)
        self.assertIn("SLACK_DEV_CHANNEL", runtime)
        self.assertIn("OIDC_ISSUER", runtime)
        self.assertIn("IDENTITY_PROVIDER", runtime)
        self.assertIn("ssm:GetParameter", iam)
        self.assertNotIn("runtime-config", secrets)

    def test_user_data_tf_tokens_match_ec2_substitutions(self):
        user_data = USER_DATA.read_text()
        ec2 = EC2_TF.read_text()
        runtime = RUNTIME_CONFIG_TF.read_text()
        variables = VARIABLES_TF.read_text()
        for token in FROZEN_TF_TOKENS:
            self.assertIn(token, user_data)
            self.assertIn(token, ec2)
        for token in RUNTIME_CONFIG_USERDATA_TOKENS:
            self.assertNotIn(token, user_data)
            self.assertNotIn(token, ec2)
        for key in (
            "SLACK_DEV_CHANNEL",
            "OIDC_ISSUER",
            "IDENTITY_PROVIDER",
            "AGENT_TIMEZONE",
            "MULTI_AGENT_ENABLED",
            "EMBEDDINGS_PROVIDER",
            "WORKFLOW_RUNNER_ENABLED",
            "IMPLEMENT_WORK_ORDERS_REPO_URL",
        ):
            self.assertIn(key, runtime)
        self.assertIn('variable "slack_dev_channel"', variables)
        self.assertIn('variable "identity_provider"', variables)
        self.assertIn("../../scripts/user-data.sh", ec2)
        self.assertIn('DATA_DIR="/vol/ts-harness-data"', user_data)
        self.assertIn('COMPLETION_MARKER="/vol/ts-harness-data/.setup-complete"', user_data)
        self.assertIn("ignore_changes = [launch_template]", ec2)

    def test_ebs_mount_and_device_contract(self):
        ebs = EBS_TF.read_text()
        variables = VARIABLES_TF.read_text()
        user_data = USER_DATA.read_text()

        self.assertIn('device_name = "/dev/sdf"', ebs)
        self.assertIn("aws_ebs_volume.harness_data", ebs)
        self.assertIn("/vol/ts-harness-data", variables)
        self.assertIn("/vol/ts-harness-data", user_data)

    def test_monitoring_metric_names_frozen(self):
        monitoring = MONITORING_TF.read_text()
        for metric in (
            "GatewayHealthy",
            "WorkflowRunnerHealthy",
            "WorkflowTasksPending",
        ):
            self.assertIn(f'metric_name         = "{metric}"', monitoring)
        self.assertIn('namespace           = "TSHarness"', monitoring)

    def test_rds_defaults_frozen(self):
        variables = VARIABLES_TF.read_text()
        self.assertIn('variable "memory_backend"', variables)
        self.assertIn('default     = "local"', variables)
        self.assertIn('variable "rds_instance_class"', variables)
        self.assertIn('default     = "db.t3.micro"', variables)
        self.assertIn('variable "rds_backup_retention_days"', variables)
        self.assertIn("default     = 7", variables)

    def test_ssh_output_contract(self):
        outputs = OUTPUTS_TF.read_text()
        match = re.search(
            r'output "ssh_command"\s*\{.*?value\s*=\s*"([^"]+)"',
            outputs,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertEqual(
            match.group(1),
            "ssh -p 2222 ${var.admin_username}@${aws_eip.harness.public_ip}",
        )

    def test_existing_aws_installation_metadata_fixture(self):
        self.assertTrue(FIXTURE.is_file())
        meta = json.loads(FIXTURE.read_text())

        self.assertEqual(meta["cloud_provider"], "aws")
        self.assertEqual(meta["data_mountpoint"], "/vol/ts-harness-data")
        self.assertEqual(meta["data_disk_device_hint"], "/dev/sdf")
        self.assertEqual(meta["monitoring_namespace"], "TSHarness")
        self.assertIn("aws_instance.harness", meta["frozen_resource_addresses"])
        self.assertIn("aws_ebs_volume.harness_data", meta["frozen_resource_addresses"])

        legacy = meta["legacy_outputs"]
        normalized = normalize_install_outputs(
            {
                "cloud_provider": "aws",
                "cloud_region": meta["cloud_region"],
                "instance_id": legacy["instance_id"],
                "ebs_volume_id": legacy["ebs_volume_id"],
                "data_disk_device_hint": meta["data_disk_device_hint"],
                "secret_prefix": meta["secret_prefix"],
                "rds_address": legacy["rds_address"],
                "rds_port": meta["managed_db_port"],
                "rds_db_name": meta["managed_db_name"],
                "rds_username": meta["managed_db_user"],
                "rds_master_secret_arn": legacy["rds_master_secret_arn"],
                "agent_credentials_kms_key_alias": meta["credentials_encryption_key_id"],
                "runtime_private_url": meta["runtime_private_url"],
                "ssh_command": legacy["ssh_command"],
                "monitoring_namespace": meta["monitoring_namespace"],
                "notification_topic_arn": meta["notification_channel_id"],
            }
        )
        self.assertEqual(normalized["compute_resource_id"], meta["compute_resource_id"])
        self.assertEqual(normalized["data_disk_resource_id"], meta["data_disk_resource_id"])
        self.assertEqual(
            normalized["operator_connection_command"],
            meta["operator_connection_command"],
        )


if __name__ == "__main__":
    unittest.main()
