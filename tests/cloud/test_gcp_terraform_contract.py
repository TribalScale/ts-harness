"""Validate infra/gcp Terraform root against the install contract."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GCP = REPO_ROOT / "infra" / "gcp"
AWS_SECRETS = REPO_ROOT / "infra" / "aws" / "secrets.tf"
STARTUP = REPO_ROOT / "scripts" / "gcp-startup.sh"

REQUIRED_FILES = (
    "versions.tf",
    "backend.tf",
    "variables.tf",
    "terraform.tfvars.example",
    "apis.tf",
    "network.tf",
    "compute.tf",
    "disk.tf",
    "iam.tf",
    "sql.tf",
    "secrets.tf",
    "kms.tf",
    "monitoring.tf",
    "budget.tf",
    "outputs.tf",
    "migrations.tf",
    "runtime_config.tf",
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

FROZEN_SECRET_KEYS = (
    "anthropic-api-key",
    "db-password",
    "gateway-token",
    "agent-credentials-kms-key-id",
)


def _aws_secret_keys() -> set[str]:
    text = AWS_SECRETS.read_text()
    # Keys in secrets_map / all_secrets literal map entries.
    return set(re.findall(r'"([a-z0-9-]+)"\s*=\s*\{\s*consumers', text))


class GcpTerraformContractTests(unittest.TestCase):
    def test_gcp_root_files_exist(self):
        self.assertTrue(GCP.is_dir())
        for name in REQUIRED_FILES:
            self.assertTrue((GCP / name).is_file(), msg=f"missing {name}")
        self.assertTrue(STARTUP.is_file())

    def test_normalized_outputs_present(self):
        text = (GCP / "outputs.tf").read_text()
        for name in NORMALIZED_OUTPUTS:
            self.assertIn(f'output "{name}"', text, msg=f"missing output {name}")
        self.assertIn('value       = "gcp"', text)
        self.assertIn("gcloud compute ssh", text)
        self.assertIn("TSHarness", text)

    def test_variables_include_contract_identity(self):
        text = (GCP / "variables.tf").read_text()
        self.assertIn('variable "cloud_provider"', text)
        self.assertIn('variable "gcp_project_id"', text)
        self.assertIn('variable "cloud_region"', text)
        self.assertIn('variable "cloud_zone"', text)
        self.assertIn('variable "secret_prefix"', text)
        self.assertIn('variable "notification_email"', text)
        self.assertIn('variable "monthly_budget_limit"', text)
        self.assertIn('variable "billing_account_id"', text)
        self.assertIn('variable "managed_db_instance_class"', text)
        self.assertIn('variable "vm_instance_type"', text)
        self.assertIn('variable "vm_data_disk_size_gb"', text)

    def test_sql_uses_postgres_16(self):
        sql = (GCP / "sql.tf").read_text()
        self.assertIn("POSTGRES_16", sql)
        self.assertIn("google_sql_database_instance", sql)

    def test_secrets_catalog_parity_with_aws_keys(self):
        gcp_secrets = (GCP / "secrets.tf").read_text()
        aws_keys = _aws_secret_keys()
        self.assertTrue(aws_keys, msg="failed to parse AWS secret keys")
        for key in aws_keys:
            self.assertIn(f'"{key}"', gcp_secrets, msg=f"GCP secrets missing AWS key {key}")
        for key in FROZEN_SECRET_KEYS:
            self.assertIn(f'"{key}"', gcp_secrets)

    def test_startup_script_wired_and_tokens(self):
        compute = (GCP / "compute.tf").read_text()
        startup = STARTUP.read_text()
        self.assertIn("../../scripts/gcp-startup.sh", compute)
        self.assertIn('CLOUD_PROVIDER="__TF_CLOUD_PROVIDER__"', startup)
        self.assertIn('DATA_DIR="/vol/ts-harness-data"', startup)
        self.assertIn("scripts/bootstrap/run.sh", startup)
        for token in (
            "__TF_CLOUD_PROVIDER__",
            "__TF_CLOUD_REGION__",
            "__TF_SECRETS_PREFIX__",
            "__TF_MANAGED_DB_HOST__",
            "__TF_MANAGED_DB_CREDENTIAL_REF__",
            "__TF_CREDENTIALS_ENCRYPTION_KEY_ID__",
        ):
            self.assertIn(token, startup)
            self.assertIn(token, compute)
        for token in (
            "__TF_IMPLEMENT_WORK_ORDERS_OPENCODE_VERSION__",
            "__TF_IDENTITY_PROVIDER__",
            "__TF_OIDC_ISSUER__",
            "__TF_OIDC_CLIENT_ID__",
            "__TF_OIDC_TOKEN_ENDPOINT__",
            "__TF_SLACK_DEV_CHANNEL__",
            "__TF_AGENT_TIMEZONE__",
        ):
            self.assertNotIn(token, startup)
            self.assertNotIn(token, compute)
        self.assertIn("ignore_changes = [metadata_startup_script]", compute)

    def test_runtime_config_secret_not_in_credentials_catalog(self):
        runtime = (GCP / "runtime_config.tf").read_text()
        secrets = (GCP / "secrets.tf").read_text()
        self.assertIn("${local.secret_prefix_effective}-runtime-config", runtime)
        self.assertIn("google_secret_manager_secret", runtime)
        self.assertIn("SLACK_DEV_CHANNEL", runtime)
        self.assertIn("OIDC_ISSUER", runtime)
        version_block = runtime[
            runtime.index("google_secret_manager_secret_version"):
        ]
        self.assertNotIn("ignore_changes", version_block)
        self.assertNotIn('"runtime-config"', secrets)

    def test_versions_uses_google_provider(self):
        versions = (GCP / "versions.tf").read_text()
        self.assertIn('source  = "hashicorp/google"', versions)
        self.assertIn("google-beta", versions)

    def test_backend_is_partial_gcs(self):
        backend = (GCP / "backend.tf").read_text()
        self.assertIn('backend "gcs" {}', backend)


if __name__ == "__main__":
    unittest.main()
