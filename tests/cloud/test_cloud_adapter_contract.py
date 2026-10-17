"""Cloud adapter dispatcher contract (provider-neutral shell operations)."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = REPO_ROOT / "scripts" / "cloud" / "adapter.sh"
CONTRACT = REPO_ROOT / "scripts" / "cloud" / "contract.sh"

REQUIRED_OPS = (
    "cloud_validate_environment",
    "cloud_fetch_secret",
    "cloud_fetch_managed_db_credentials",
    "cloud_fetch_runtime_config",
    "cloud_resolve_data_device",
    "cloud_get_instance_resource_id",
    "cloud_publish_runtime_metrics",
)


class CloudAdapterContractTests(unittest.TestCase):
    def test_adapter_and_contract_exist(self):
        self.assertTrue(ADAPTER.is_file(), f"missing {ADAPTER}")
        self.assertTrue(CONTRACT.is_file(), f"missing {CONTRACT}")

    def test_contract_documents_required_operations(self):
        text = CONTRACT.read_text()
        for op in REQUIRED_OPS:
            self.assertIn(op, text)

    def test_adapter_rejects_unknown_provider(self):
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            export CLOUD_PROVIDER=azure
            export DATA_DIR="{tempfile.mkdtemp()}"
            source "{ADAPTER}"
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
        self.assertRegex(
            (result.stderr + result.stdout).lower(),
            r"unknown|unsupported|invalid",
        )

    def test_adapter_loads_aws_and_exports_operations(self):
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            export CLOUD_PROVIDER=aws
            export CLOUD_REGION=us-east-1
            export AWS_REGION=us-east-1
            export SECRETS_PREFIX=ts-harness
            export DATA_DIR="{tempfile.mkdtemp()}"
            source "{ADAPTER}"
            for op in {' '.join(REQUIRED_OPS)}; do
              declare -F "$op" >/dev/null
            done
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
        self.assertIn("OK", result.stdout)

    def test_adapter_rejects_provider_change_vs_install_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            setup = data_dir / ".setup"
            setup.mkdir(parents=True)
            (setup / "install-metadata.json").write_text(
                '{"cloud_provider":"aws","install_contract_version":"0.1.0"}\n',
                encoding="utf-8",
            )
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                export CLOUD_PROVIDER=gcp
                export CLOUD_REGION=us-central1
                export DATA_DIR="{data_dir}"
                source "{ADAPTER}"
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
            combined = (result.stderr + result.stdout).lower()
            self.assertRegex(combined, r"provider|mismatch|metadata")


if __name__ == "__main__":
    unittest.main()
