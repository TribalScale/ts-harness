"""Compose database contract: local-db vs managed-db overlays."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKER = REPO_ROOT / "docker"
LOCAL_DB = DOCKER / "docker-compose.local-db.yml"
MANAGED_DB = DOCKER / "docker-compose.managed-db.yml"
MEMORY_API = DOCKER / "docker-compose.memory-api.yml"
COMPOSE_PHASE = REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh"
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"
LEGACY_NAMES = (
    "docker-compose.override.yml",
    "docker-compose.rds.yml",
)


class DatabaseComposeContractTests(unittest.TestCase):
    def test_local_db_compose_defines_ts_harness_db_with_pgvector(self):
        self.assertTrue(LOCAL_DB.is_file(), f"missing {LOCAL_DB}")
        text = LOCAL_DB.read_text()
        self.assertIn("ts-harness-db:", text)
        self.assertIn("pgvector", text.lower())
        self.assertIn("POSTGRES_USER", text)
        self.assertIn("POSTGRES_PASSWORD", text)
        self.assertIn("POSTGRES_DB", text)
        self.assertIn("healthcheck:", text)

    def test_managed_db_compose_has_no_local_postgres_container(self):
        self.assertTrue(MANAGED_DB.is_file(), f"missing {MANAGED_DB}")
        text = MANAGED_DB.read_text()
        self.assertNotRegex(text, r"(?m)^\s*ts-harness-db:")
        self.assertIn("MANAGED_DB_", text)
        self.assertIn("TS_HARNESS_DB_", text)

    def test_compose_phase_selects_local_or_managed_overlay(self):
        phase = COMPOSE_PHASE.read_text()
        common = COMMON.read_text()
        surface = "\n".join((phase, common))

        self.assertIn("docker-compose.local-db.yml", surface)
        self.assertIn("docker-compose.managed-db.yml", surface)
        for legacy in LEGACY_NAMES:
            self.assertNotIn(legacy, surface)

    def test_managed_path_maps_managed_db_into_ts_harness_env(self):
        phase = COMPOSE_PHASE.read_text()
        self.assertIn("MANAGED_DB_HOST", phase)
        self.assertIn("MANAGED_DB_PORT", phase)
        self.assertIn("MANAGED_DB_NAME", phase)
        self.assertIn("MANAGED_DB_USER", phase)
        self.assertIn("MANAGED_DB_PASSWORD", phase)
        self.assertIn("MANAGED_DB_CREDENTIAL_REF", phase)
        self.assertIn('export TS_HARNESS_DB_HOST=', phase)
        self.assertIn("TS_HARNESS_DB_HOST", phase)
        # Production/managed path still accepts legacy MEMORY_BACKEND=rds via helper.
        common = COMMON.read_text()
        self.assertIn("is_managed_memory_backend", common)
        self.assertIn("rds|managed", common)

    def test_reconstruct_compose_files_uses_new_overlays(self):
        common = COMMON.read_text()
        self.assertIn("reconstruct_compose_files", common)
        self.assertIn("docker-compose.local-db.yml", common)
        self.assertIn("docker-compose.managed-db.yml", common)
        for legacy in LEGACY_NAMES:
            self.assertNotIn(legacy, common)

    def test_memory_api_compose_remains_host_agnostic(self):
        text = MEMORY_API.read_text()
        self.assertIn("TS_HARNESS_DB_HOST:", text)
        self.assertIn("TS_HARNESS_DB_HOST:", text)
        # memory-api itself must not embed a Postgres service definition.
        self.assertNotRegex(text, r"(?m)^\s*ts-harness-db:")


if __name__ == "__main__":
    unittest.main()
