"""Install metadata read/write and provider mismatch guards."""

from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
METADATA_SH = REPO_ROOT / "scripts" / "bootstrap" / "install-metadata.sh"
ADAPTER = REPO_ROOT / "scripts" / "cloud" / "adapter.sh"
EBS_PHASE = REPO_ROOT / "scripts" / "phases" / "01-ebs.sh"
FINALIZE = REPO_ROOT / "scripts" / "phases" / "99-finalize.sh"

REQUIRED_FIELDS = (
    "cloud_provider",
    "account_or_project",
    "cloud_region",
    "environment",
    "compute_resource_id",
    "data_disk_resource_id",
    "terraform_state_identity",
    "install_contract_version",
)


class InstallMetadataTests(unittest.TestCase):
    def test_metadata_helper_exists(self):
        self.assertTrue(METADATA_SH.is_file())
        text = METADATA_SH.read_text()
        self.assertIn("install_metadata_write", text)
        self.assertIn("install_metadata_get", text)
        self.assertIn("install_metadata_assert_provider", text)
        self.assertIn("install-metadata.json", text)

    def test_write_and_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export DATA_DIR="{tmp}"
                export CLOUD_PROVIDER=aws
                export ACCOUNT_OR_PROJECT=123456789012
                export CLOUD_REGION=us-east-1
                export ENVIRONMENT=prod
                export COMPUTE_RESOURCE_ID=i-abc123
                export DATA_DISK_RESOURCE_ID=vol-abc123
                export TERRAFORM_STATE_IDENTITY=s3://bucket/prod/terraform.tfstate
                export INSTALL_CONTRACT_VERSION=0.1.0
                source "{METADATA_SH}"
                install_metadata_write
                [[ "$(install_metadata_get cloud_provider)" == "aws" ]]
                [[ "$(install_metadata_get data_disk_resource_id)" == "vol-abc123" ]]
                [[ "$(install_metadata_get install_contract_version)" == "0.1.0" ]]
                echo OK
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                (Path(tmp) / ".setup" / "install-metadata.json").read_text()
            )
            for field in REQUIRED_FIELDS:
                self.assertIn(field, payload)
                self.assertTrue(str(payload[field]).strip())

    def test_assert_provider_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup = Path(tmp) / ".setup"
            setup.mkdir(parents=True)
            (setup / "install-metadata.json").write_text(
                json.dumps({"cloud_provider": "aws", "install_contract_version": "0.1.0"}),
                encoding="utf-8",
            )
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export DATA_DIR="{tmp}"
                source "{METADATA_SH}"
                install_metadata_assert_provider gcp
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not match", result.stderr)

    def test_ebs_and_finalize_persist_metadata(self):
        ebs = EBS_PHASE.read_text()
        finalize = FINALIZE.read_text()
        self.assertIn("install-metadata", ebs)
        self.assertIn("install_metadata_write", ebs)
        self.assertIn("install_metadata_assert_provider", ebs)
        self.assertIn("install_metadata_write", finalize)


if __name__ == "__main__":
    unittest.main()
