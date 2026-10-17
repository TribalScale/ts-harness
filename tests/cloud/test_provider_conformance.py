"""Cross-provider conformance: AWS and GCP yield the same shared runtime surface."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.cloud.conformance import (
    COMPOSE_MANAGED_DB_FILES,
    DATA_MOUNTPOINT,
    GATEWAY_PORT,
    HEALTH_PROBE_PATHS,
    MANAGED_DB_ENV_KEYS,
    MEMORY_API_PORT,
    bootstrap_env_from_fixture,
    cloud_neutral_runtime_surface,
    gcp_logical_secret_catalog_keys,
    logical_secret_catalog_keys,
    select_compose_files,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class ProviderConformanceTests(unittest.TestCase):
    def test_managed_compose_selection_identical(self):
        aws = bootstrap_env_from_fixture("aws")
        gcp = bootstrap_env_from_fixture("gcp")
        expected = list(COMPOSE_MANAGED_DB_FILES)
        self.assertEqual(aws["compose_files"], expected)
        self.assertEqual(gcp["compose_files"], expected)
        self.assertEqual(select_compose_files(memory_backend="managed"), expected)
        self.assertIn("docker-compose.managed-db.yml", aws["compose_files"])
        self.assertNotIn("docker-compose.local-db.yml", aws["compose_files"])

    def test_ports_and_mountpoint_identical(self):
        aws = bootstrap_env_from_fixture("aws")
        gcp = bootstrap_env_from_fixture("gcp")
        for env in (aws, gcp):
            self.assertEqual(env["GATEWAY_PORT"], GATEWAY_PORT)
            self.assertEqual(env["MEMORY_API_PORT"], MEMORY_API_PORT)
            self.assertEqual(env["AGENT_RUNTIME_PORT"], GATEWAY_PORT)
            self.assertEqual(env["DATA_DIR"], DATA_MOUNTPOINT)
            self.assertEqual(env["data_mountpoint"], "/vol/ts-harness-data")

        self.assertEqual(
            cloud_neutral_runtime_surface(aws)["GATEWAY_PORT"],
            cloud_neutral_runtime_surface(gcp)["GATEWAY_PORT"],
        )
        self.assertEqual(
            cloud_neutral_runtime_surface(aws)["data_mountpoint"],
            cloud_neutral_runtime_surface(gcp)["data_mountpoint"],
        )

    def test_secret_catalog_logical_keys_match(self):
        aws_keys = logical_secret_catalog_keys()
        gcp_keys = gcp_logical_secret_catalog_keys()
        self.assertEqual(aws_keys, gcp_keys)
        for required in (
            "anthropic-api-key",
            "db-password",
            "gateway-token",
            "agent-credentials-kms-key-id",
        ):
            self.assertIn(required, aws_keys)

        aws_env = bootstrap_env_from_fixture("aws")
        gcp_env = bootstrap_env_from_fixture("gcp")
        self.assertEqual(aws_env["secret_catalog_keys"], gcp_env["secret_catalog_keys"])
        self.assertEqual(set(aws_env["secret_catalog_keys"]), aws_keys)

    def test_health_probe_paths_identical(self):
        aws = bootstrap_env_from_fixture("aws")
        gcp = bootstrap_env_from_fixture("gcp")
        self.assertEqual(aws["health_probe_paths"], list(HEALTH_PROBE_PATHS))
        self.assertEqual(gcp["health_probe_paths"], list(HEALTH_PROBE_PATHS))
        self.assertIn("/healthz", aws["health_probe_paths"])
        self.assertIn("/health", aws["health_probe_paths"])

        # Compose contracts still advertise the same probe paths on disk.
        agent = (REPO_ROOT / "docker" / "docker-compose.agent-runtime.yml").read_text()
        memory = (REPO_ROOT / "docker" / "docker-compose.memory-api.yml").read_text()
        self.assertIn("/healthz", agent)
        self.assertIn("/health", memory)

    def test_cloud_provider_set_and_managed_db_populated(self):
        aws = bootstrap_env_from_fixture("aws")
        gcp = bootstrap_env_from_fixture("gcp")

        self.assertEqual(aws["CLOUD_PROVIDER"], "aws")
        self.assertEqual(gcp["CLOUD_PROVIDER"], "gcp")

        for env in (aws, gcp):
            for key in MANAGED_DB_ENV_KEYS:
                value = env.get(key)
                self.assertTrue(
                    value not in (None, "", []),
                    msg=f"{env['CLOUD_PROVIDER']} missing {key}",
                )
            self.assertEqual(int(env["MANAGED_DB_PORT"]), 5432)
            self.assertEqual(env["MANAGED_DB_NAME"], "ts_harness")

        # Provider-specific credential refs differ; host/user may differ.
        self.assertNotEqual(aws["MANAGED_DB_HOST"], gcp["MANAGED_DB_HOST"])
        self.assertNotEqual(
            aws["MANAGED_DB_CREDENTIAL_REF"],
            gcp["MANAGED_DB_CREDENTIAL_REF"],
        )

    def test_cloud_neutral_surface_equal(self):
        aws = cloud_neutral_runtime_surface(bootstrap_env_from_fixture("aws"))
        gcp = cloud_neutral_runtime_surface(bootstrap_env_from_fixture("gcp"))
        self.assertEqual(aws, gcp)


if __name__ == "__main__":
    unittest.main()
