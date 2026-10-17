"""Database tier defaults to Multi-AZ. Dev accounts can still opt out."""

from __future__ import annotations

import unittest
from pathlib import Path

from install import generate_tfvars
from install_contract import load_install_config


REPO_ROOT = Path(__file__).resolve().parents[2]


class MultiAzDefaultTests(unittest.TestCase):
    def test_rds_and_cloud_sql_default_to_multi_az(self):
        rds = (REPO_ROOT / "infra" / "aws" / "rds.tf").read_text()
        aws_vars = (REPO_ROOT / "infra" / "aws" / "variables.tf").read_text()
        sql = (REPO_ROOT / "infra" / "gcp" / "sql.tf").read_text()
        gcp_vars = (REPO_ROOT / "infra" / "gcp" / "variables.tf").read_text()

        self.assertIn("multi_az            = var.rds_multi_az", rds)
        self.assertIn("apply_immediately   = var.rds_apply_immediately", rds)
        self.assertIn('variable "rds_multi_az"', aws_vars)
        self.assertRegex(aws_vars, r'variable "rds_multi_az" \{[^}]*default\s+=\s+true')
        self.assertRegex(aws_vars, r'variable "rds_apply_immediately" \{[^}]*default\s+=\s+false')
        self.assertRegex(aws_vars, r'variable "redis_multi_az" \{[^}]*default\s+=\s+true')
        self.assertIn("availability_type = var.cloud_sql_availability_type", sql)
        self.assertRegex(
            gcp_vars,
            r'variable "cloud_sql_availability_type" \{[^}]*default\s+=\s+"REGIONAL"',
        )

    def test_install_yaml_can_keep_a_single_zone_database(self):
        aws = load_install_config(REPO_ROOT / "config" / "install" / "aws.example.yaml")
        gcp = load_install_config(REPO_ROOT / "config" / "install" / "gcp.example.yaml")
        self.assertTrue(generate_tfvars(aws, cloud="aws")["rds_multi_az"])
        self.assertFalse(generate_tfvars(aws, cloud="aws")["rds_apply_immediately"])
        self.assertEqual(
            generate_tfvars(gcp, cloud="gcp")["cloud_sql_availability_type"],
            "REGIONAL",
        )

        aws["managed_db"] = dict(aws["managed_db"])
        aws["managed_db"]["multi_az"] = False
        aws["managed_db"]["apply_immediately"] = True
        aws_tfvars = generate_tfvars(aws, cloud="aws")
        self.assertFalse(aws_tfvars["rds_multi_az"])
        self.assertTrue(aws_tfvars["rds_apply_immediately"])

        gcp["managed_db"] = dict(gcp["managed_db"])
        gcp["managed_db"]["multi_az"] = False
        self.assertEqual(
            generate_tfvars(gcp, cloud="gcp")["cloud_sql_availability_type"],
            "ZONAL",
        )


if __name__ == "__main__":
    unittest.main()
