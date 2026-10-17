"""Assert GCP Cloud SQL Terraform matches managed-DB install contract."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SQL_TF = REPO_ROOT / "infra" / "gcp" / "sql.tf"
NETWORK_TF = REPO_ROOT / "infra" / "gcp" / "network.tf"
VARIABLES_TF = REPO_ROOT / "infra" / "gcp" / "variables.tf"


class GcpDatabaseContractTests(unittest.TestCase):
    def test_sql_file_exists(self):
        self.assertTrue(SQL_TF.is_file())

    def test_deletion_protection_and_backups(self):
        sql = SQL_TF.read_text()
        self.assertIn("deletion_protection", sql)
        self.assertIn("backup_configuration", sql)
        self.assertIn("enabled", sql)
        self.assertIn("point_in_time_recovery_enabled", sql)
        self.assertIn("backup_retention_settings", sql)

    def test_private_networking_preferred(self):
        sql = SQL_TF.read_text()
        network = NETWORK_TF.read_text()
        variables = VARIABLES_TF.read_text()

        self.assertIn("private_network", sql)
        self.assertIn("cloud_sql_private_ip", sql)
        self.assertIn("servicenetworking", network.lower() + SQL_TF.read_text().lower())
        self.assertIn("google_service_networking_connection", network)
        self.assertIn('variable "cloud_sql_private_ip"', variables)
        self.assertIn("default     = true", variables)
        # Documented public-IP fallback gated by authorized networks.
        self.assertIn("authorized_networks", sql)
        self.assertIn('variable "cloud_sql_authorized_networks"', variables)

    def test_credentials_stored_in_secret_manager(self):
        sql = SQL_TF.read_text()
        self.assertIn("google_secret_manager_secret", sql)
        self.assertIn("managed-db-credentials", sql)
        self.assertIn("random_password", sql)

    def test_postgres_16_engine(self):
        sql = SQL_TF.read_text()
        self.assertIn('database_version = "POSTGRES_16"', sql)


if __name__ == "__main__":
    unittest.main()
