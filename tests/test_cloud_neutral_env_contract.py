"""Cloud-neutral bootstrap env names with legacy AWS aliases."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_DEFAULTS = REPO_ROOT / "scripts" / "phases" / "lib" / "env-defaults.sh"
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"
USER_DATA = REPO_ROOT / "scripts" / "user-data.sh"
INIT_PHASE = REPO_ROOT / "scripts" / "phases" / "00-init.sh"
FETCH_SECRETS = REPO_ROOT / "scripts" / "phases" / "10-fetch-secrets.sh"
COMPOSE_PHASE = REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh"
RUNTIME_TMPL = REPO_ROOT / "config" / "ts-harness-runtime.yaml.tmpl"

NEUTRAL_KEYS = (
    "CLOUD_PROVIDER",
    "CLOUD_REGION",
    "MANAGED_DB_HOST",
    "MANAGED_DB_PORT",
    "MANAGED_DB_NAME",
    "MANAGED_DB_USER",
    "MANAGED_DB_PASSWORD",
    "MANAGED_DB_CREDENTIAL_REF",
    "CREDENTIALS_ENCRYPTION_KEY_ID",
    "INSTANCE_RESOURCE_ID",
)


class CloudNeutralEnvContractTests(unittest.TestCase):
    def test_helper_and_neutral_names_exist(self):
        common = COMMON.read_text()
        env_defaults = ENV_DEFAULTS.read_text()
        surface = "\n".join((common, env_defaults))

        self.assertIn("resolve_cloud_neutral_env", surface)
        for key in NEUTRAL_KEYS:
            self.assertIn(key, surface)

    def test_legacy_aws_aliases_still_referenced(self):
        surface = "\n".join(
            (
                USER_DATA.read_text(),
                ENV_DEFAULTS.read_text(),
                COMMON.read_text(),
                INIT_PHASE.read_text(),
                FETCH_SECRETS.read_text(),
                COMPOSE_PHASE.read_text(),
            )
        )
        for legacy in (
            "AWS_REGION",
            "RDS_ENDPOINT",
            "RDS_SECRET_ARN",
            "RDS_DB_NAME",
            "AGENT_CREDENTIALS_KMS_KEY_ID",
        ):
            self.assertIn(legacy, surface)

    def test_resolve_maps_legacy_rds_into_managed_db(self):
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source "{COMMON}"
            unset CLOUD_REGION MANAGED_DB_HOST MANAGED_DB_PORT MANAGED_DB_NAME
            unset MANAGED_DB_CREDENTIAL_REF CREDENTIALS_ENCRYPTION_KEY_ID
            export AWS_REGION=us-west-2
            export RDS_ENDPOINT=db.example.internal
            export RDS_DB_NAME=ts_harness
            export RDS_SECRET_ARN=arn:aws:secretsmanager:us-west-2:123:secret:rds
            export AGENT_CREDENTIALS_KMS_KEY_ID=alias/ts-harness-agent-credentials
            resolve_cloud_neutral_env
            printf '%s\\n' "$CLOUD_PROVIDER" "$CLOUD_REGION" "$MANAGED_DB_HOST" \\
              "$MANAGED_DB_NAME" "$MANAGED_DB_CREDENTIAL_REF" "$CREDENTIALS_ENCRYPTION_KEY_ID"
            """
        )
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(script)
            path = fh.name
        result = subprocess.run(
            ["bash", path],
            check=True,
            capture_output=True,
            text=True,
        )
        lines = result.stdout.strip().splitlines()
        self.assertEqual(
            lines,
            [
                "aws",
                "us-west-2",
                "db.example.internal",
                "ts_harness",
                "arn:aws:secretsmanager:us-west-2:123:secret:rds",
                "alias/ts-harness-agent-credentials",
            ],
        )

    def test_resolve_maps_neutral_names_back_to_legacy_aws(self):
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source "{COMMON}"
            unset AWS_REGION RDS_ENDPOINT RDS_SECRET_ARN RDS_DB_NAME
            unset AGENT_CREDENTIALS_KMS_KEY_ID
            export CLOUD_PROVIDER=aws
            export CLOUD_REGION=eu-central-1
            export MANAGED_DB_HOST=cloudsql.internal
            export MANAGED_DB_PORT=5432
            export MANAGED_DB_NAME=enterprise
            export MANAGED_DB_CREDENTIAL_REF=projects/p/secrets/db/versions/1
            export CREDENTIALS_ENCRYPTION_KEY_ID=projects/p/locations/global/keyRings/r/cryptoKeys/k
            resolve_cloud_neutral_env
            printf '%s\\n' "$AWS_REGION" "$RDS_ENDPOINT" "$RDS_DB_NAME" \\
              "$RDS_SECRET_ARN" "$AGENT_CREDENTIALS_KMS_KEY_ID"
            """
        )
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(script)
            path = fh.name
        result = subprocess.run(
            ["bash", path],
            check=True,
            capture_output=True,
            text=True,
        )
        lines = result.stdout.strip().splitlines()
        self.assertEqual(
            lines,
            [
                "eu-central-1",
                "cloudsql.internal",
                "enterprise",
                "projects/p/secrets/db/versions/1",
                "projects/p/locations/global/keyRings/r/cryptoKeys/k",
            ],
        )

    def test_runtime_template_accepts_credentials_encryption_key_id(self):
        tmpl = RUNTIME_TMPL.read_text()
        self.assertTrue(
            "CREDENTIALS_ENCRYPTION_KEY_ID" in tmpl
            or "AGENT_CREDENTIALS_KMS_KEY_ID" in tmpl
        )
        # Prefer neutral placeholder when present; legacy must remain resolvable.
        if "CREDENTIALS_ENCRYPTION_KEY_ID" in tmpl:
            self.assertIn("{{CREDENTIALS_ENCRYPTION_KEY_ID}}", tmpl)

    def test_user_data_and_phases_invoke_resolver(self):
        user_data = USER_DATA.read_text()
        env_defaults = ENV_DEFAULTS.read_text()
        surface = "\n".join((user_data, env_defaults, COMPOSE_PHASE.read_text()))
        self.assertIn("resolve_cloud_neutral_env", surface)
        self.assertIn("CLOUD_PROVIDER", user_data)


if __name__ == "__main__":
    unittest.main()
