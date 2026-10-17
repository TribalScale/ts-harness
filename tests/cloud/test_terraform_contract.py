"""Assert infra/contract documents required install-contract Terraform names."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "infra" / "contract"
VARIABLES_TF = CONTRACT / "variables.tf"
OUTPUTS_TF = CONTRACT / "outputs.tf"
README = CONTRACT / "README.md"
AWS_ROOT = REPO_ROOT / "infra" / "aws"
GCP_ROOT = REPO_ROOT / "infra" / "gcp"

REQUIRED_INPUT_NAMES = (
    "cloud_provider",
    "environment",
    "project_name",
    "cloud_region",
    "cloud_zone",
    "secret_prefix",
    "notification_email",
    "monthly_budget_limit",
    "managed_db_instance_class",
    "managed_db_name",
    "managed_db_user",
    "managed_db_backup_retention_days",
    "credentials_encryption_key_id",
    "vm_instance_type",
    "vm_root_volume_size_gb",
    "vm_data_disk_size_gb",
)

REQUIRED_OUTPUT_NAMES = (
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


class TerraformContractTests(unittest.TestCase):
    def test_contract_files_exist(self):
        self.assertTrue(README.is_file())
        self.assertTrue(VARIABLES_TF.is_file())
        self.assertTrue(OUTPUTS_TF.is_file())

    def test_contract_readme_forbids_mega_module(self):
        text = README.read_text()
        self.assertIn("not", text.lower())
        self.assertIn("mega-module", text.lower())
        self.assertIn("infra/aws", text)
        self.assertIn("infra/gcp", text)

    def test_required_input_names_documented(self):
        text = VARIABLES_TF.read_text()
        for name in REQUIRED_INPUT_NAMES:
            self.assertIn(f'variable "{name}"', text, msg=f"missing input {name}")

    def test_required_output_names_documented(self):
        text = OUTPUTS_TF.read_text()
        for name in REQUIRED_OUTPUT_NAMES:
            self.assertIn(f'output "{name}"', text, msg=f"missing output {name}")

    def test_managed_db_and_encryption_key_covered(self):
        variables = VARIABLES_TF.read_text()
        outputs = OUTPUTS_TF.read_text()
        self.assertIn("managed_db_", variables)
        self.assertIn("managed_db_", outputs)
        self.assertIn("credentials_encryption_key_id", variables)
        self.assertIn("credentials_encryption_key_id", outputs)

    def test_provider_roots_emit_normalized_outputs(self):
        for root in (AWS_ROOT, GCP_ROOT):
            self.assertTrue(root.is_dir(), msg=f"missing root {root}")
            outputs = (root / "outputs.tf").read_text()
            for name in REQUIRED_OUTPUT_NAMES:
                self.assertIn(
                    f'output "{name}"',
                    outputs,
                    msg=f"{root.name} missing output {name}",
                )

    def test_gcp_root_accepts_contract_identity_inputs(self):
        variables = (GCP_ROOT / "variables.tf").read_text()
        for name in (
            "cloud_provider",
            "gcp_project_id",
            "cloud_region",
            "cloud_zone",
            "secret_prefix",
            "notification_email",
            "monthly_budget_limit",
            "managed_db_instance_class",
            "vm_instance_type",
            "vm_data_disk_size_gb",
        ):
            self.assertIn(f'variable "{name}"', variables, msg=f"gcp missing {name}")


if __name__ == "__main__":
    unittest.main()
