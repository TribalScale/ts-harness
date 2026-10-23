"""TDD coverage for the machine-readable install-time cloud contract."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from install_contract import (
    InstallContractError,
    load_install_config,
    normalize_install_outputs,
    validate_install_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AWS_EXAMPLE = REPO_ROOT / "config" / "install" / "aws.example.yaml"
GCP_EXAMPLE = REPO_ROOT / "config" / "install" / "gcp.example.yaml"
SCHEMA = REPO_ROOT / "config" / "install-contract.schema.json"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


class InstallContractTests(unittest.TestCase):
    def test_schema_and_examples_exist(self):
        self.assertTrue(SCHEMA.is_file())
        self.assertTrue(AWS_EXAMPLE.is_file())
        self.assertTrue(GCP_EXAMPLE.is_file())

    def test_valid_aws_example_loads(self):
        cfg = load_install_config(AWS_EXAMPLE)
        self.assertEqual(cfg["cloud_provider"], "aws")
        self.assertEqual(cfg["aws_account_id"], "123456789012")
        self.assertIn("managed_db", cfg)
        self.assertEqual(cfg["managed_db"]["db_name"], "ts_harness")

    def test_valid_gcp_example_loads(self):
        cfg = load_install_config(GCP_EXAMPLE)
        self.assertEqual(cfg["cloud_provider"], "gcp")
        self.assertEqual(cfg["gcp_project_id"], "example-gcp-project")
        self.assertIn("managed_db", cfg)

    def test_examples_default_to_single_host(self):
        self.assertEqual(load_install_config(AWS_EXAMPLE)["deployment_profile"], "single_host")
        self.assertEqual(load_install_config(GCP_EXAMPLE)["deployment_profile"], "single_host")

    def test_aws_accepts_scale_out(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        cfg["deployment_profile"] = "scale_out"
        validate_install_config(cfg)

    def test_gcp_rejects_scale_out(self):
        cfg = _load_yaml(GCP_EXAMPLE)
        cfg["deployment_profile"] = "scale_out"
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("scale_out", str(ctx.exception))

    def test_reject_unknown_deployment_profile(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        cfg["deployment_profile"] = "kubernetes"
        with self.assertRaises(InstallContractError):
            validate_install_config(cfg)

    def test_reject_unknown_provider(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        cfg["cloud_provider"] = "azure"
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("cloud_provider", str(ctx.exception).lower())

    def test_reject_mixed_provider_fields_aws_with_gcp_project(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        cfg["gcp_project_id"] = "should-not-be-here"
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("mixed", str(ctx.exception).lower())

    def test_reject_mixed_provider_fields_gcp_with_aws_account(self):
        cfg = _load_yaml(GCP_EXAMPLE)
        cfg["aws_account_id"] = "123456789012"
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("mixed", str(ctx.exception).lower())

    def test_reject_missing_managed_db(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        del cfg["managed_db"]
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("managed_db", str(ctx.exception).lower())

    def test_reject_ambiguous_state_location_missing_bucket(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        cfg["terraform_state"]["bucket"] = ""
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("state", str(ctx.exception).lower())

    def test_reject_ambiguous_state_backend_mismatch(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        cfg["terraform_state"]["backend_type"] = "gcs"
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("state", str(ctx.exception).lower())

    def test_reject_aws_missing_account_id(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        del cfg["aws_account_id"]
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("aws_account_id", str(ctx.exception).lower())

    def test_reject_gcp_missing_project_id(self):
        cfg = _load_yaml(GCP_EXAMPLE)
        del cfg["gcp_project_id"]
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("gcp_project_id", str(ctx.exception).lower())

    def test_load_from_temp_yaml_round_trip(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "install.yaml"
            path.write_text(yaml.safe_dump(cfg))
            loaded = load_install_config(path)
        self.assertEqual(loaded["cloud_provider"], "aws")

    def test_normalize_install_outputs_aws_shape(self):
        raw = {
            "cloud_provider": "aws",
            "cloud_region": "us-east-1",
            "instance_id": "i-0123456789abcdef0",
            "ebs_volume_id": "vol-0abc",
            "data_disk_device_hint": "/dev/sdf",
            "secret_prefix": "ts-harness",
            "rds_address": "db.example.rds.amazonaws.com",
            "rds_port": 5432,
            "rds_db_name": "ts_harness",
            "rds_username": "ts_harness",
            "rds_master_secret_arn": "arn:aws:secretsmanager:us-east-1:123:secret:rds",
            "agent_credentials_kms_key_alias": "alias/ts-harness-agent-credentials",
            "runtime_private_url": "http://100.64.0.1:18789",
            "ssh_command": "ssh -p 2222 artemis@1.2.3.4",
            "monitoring_namespace": "TSHarness",
            "notification_topic_arn": "arn:aws:sns:us-east-1:123:alerts",
        }
        out = normalize_install_outputs(raw)
        self.assertEqual(out["cloud_provider"], "aws")
        self.assertEqual(out["cloud_region"], "us-east-1")
        self.assertEqual(out["compute_resource_id"], "i-0123456789abcdef0")
        self.assertEqual(out["data_disk_resource_id"], "vol-0abc")
        self.assertEqual(out["data_disk_device_hint"], "/dev/sdf")
        self.assertEqual(out["secret_prefix"], "ts-harness")
        self.assertEqual(out["managed_db_host"], "db.example.rds.amazonaws.com")
        self.assertEqual(out["managed_db_port"], 5432)
        self.assertEqual(out["managed_db_name"], "ts_harness")
        self.assertEqual(out["managed_db_user"], "ts_harness")
        self.assertEqual(
            out["managed_db_credential_ref"],
            "arn:aws:secretsmanager:us-east-1:123:secret:rds",
        )
        self.assertEqual(
            out["credentials_encryption_key_id"],
            "alias/ts-harness-agent-credentials",
        )
        self.assertEqual(out["runtime_private_url"], "http://100.64.0.1:18789")
        self.assertEqual(out["operator_connection_command"], "ssh -p 2222 artemis@1.2.3.4")
        self.assertEqual(out["monitoring_namespace"], "TSHarness")
        self.assertEqual(out["notification_channel_id"], "arn:aws:sns:us-east-1:123:alerts")

    def test_normalize_install_outputs_gcp_shape(self):
        raw = {
            "cloud_provider": "gcp",
            "cloud_region": "us-central1",
            "instance_id": "projects/p/zones/us-central1-a/instances/ts-harness",
            "data_disk_id": "projects/p/zones/us-central1-a/disks/ts-harness-data",
            "data_disk_device_hint": "/dev/disk/by-id/google-ts-harness-data",
            "secret_prefix": "ts-harness",
            "managed_db_host": "10.1.2.3",
            "managed_db_port": 5432,
            "managed_db_name": "ts_harness",
            "managed_db_user": "ts_harness",
            "managed_db_credential_ref": "projects/p/secrets/ts-harness-db/versions/latest",
            "credentials_encryption_key_id": (
                "projects/p/locations/us-central1/keyRings/ts-harness/"
                "cryptoKeys/agent-credentials"
            ),
            "runtime_private_url": "http://100.64.0.2:18789",
            "operator_connection_command": (
                "gcloud compute ssh ts-harness --zone us-central1-a --project p"
            ),
            "monitoring_namespace": "ts_harness",
            "notification_channel_id": "projects/p/notificationChannels/123",
        }
        out = normalize_install_outputs(raw)
        self.assertEqual(out["compute_resource_id"], raw["instance_id"])
        self.assertEqual(out["data_disk_resource_id"], raw["data_disk_id"])
        self.assertEqual(out["managed_db_host"], "10.1.2.3")
        self.assertEqual(
            out["operator_connection_command"],
            raw["operator_connection_command"],
        )

    def test_normalize_requires_cloud_provider(self):
        with self.assertRaises(InstallContractError):
            normalize_install_outputs({"cloud_region": "us-east-1"})

    def test_validate_does_not_mutate_input(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        before = copy.deepcopy(cfg)
        validate_install_config(cfg)
        self.assertEqual(cfg, before)

    def test_oidc_requires_issuer_and_client_when_enabled(self):
        cfg = _load_yaml(AWS_EXAMPLE)
        cfg["runtime"]["identity_provider"] = "oidc"
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("oidc", str(ctx.exception).lower())

        cfg["runtime"].update(
            {
                "oidc_issuer": "https://tenant.auth0.com/",
                "oidc_audience": "ts-harness",
                "oidc_jwks_url": "https://tenant.auth0.com/.well-known/jwks.json",
                "oidc_client_id": "spa-client-id",
                "oidc_authorization_endpoint": "https://tenant.auth0.com/authorize",
                "oidc_token_endpoint": "https://tenant.auth0.com/oauth/token",
            }
        )
        validate_install_config(cfg)


if __name__ == "__main__":
    unittest.main()
