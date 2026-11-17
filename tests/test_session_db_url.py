"""Bootstrap derives the runtime session DB URL from TS_HARNESS_DB_*."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"

DB_ENV = {
    "MEMORY_BACKEND": "rds",
    "TS_HARNESS_DB_HOST": "db.example.rds.amazonaws.com",
    "TS_HARNESS_DB_PORT": "5432",
    "TS_HARNESS_DB_USER": "ts_harness",
    "TS_HARNESS_DB_NAME": "ts_harness",
    "TS_HARNESS_DB_PASSWORD": "p@ss/w:rd?#%",
}


def run_common(script: str, **env: str) -> str:
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TS_HARNESS_", "PGSSLMODE", "MEMORY_BACKEND"))
    }
    return subprocess.run(
        ["bash", "-c", f'source "{COMMON}"\n{script}'],
        check=True,
        capture_output=True,
        text=True,
        env={**base, **env},
    ).stdout


class SessionDbUrlTests(unittest.TestCase):
    def test_managed_db_url_encodes_password_and_requires_tls(self):
        url = run_common("session_db_url", **DB_ENV)
        parts = urlsplit(url)
        self.assertEqual(parts.scheme, "postgresql")
        self.assertEqual(parts.hostname, DB_ENV["TS_HARNESS_DB_HOST"])
        self.assertEqual(parts.port, 5432)
        self.assertEqual(unquote(parts.username), "ts_harness")
        self.assertEqual(unquote(parts.password), DB_ENV["TS_HARNESS_DB_PASSWORD"])
        self.assertEqual(parts.path, "/ts_harness")
        self.assertEqual(parts.query, "sslmode=require")

    def test_local_db_disables_tls_and_pgsslmode_overrides(self):
        local = run_common("session_db_url", **{**DB_ENV, "MEMORY_BACKEND": "local"})
        self.assertTrue(local.endswith("?sslmode=disable"))
        custom = run_common("session_db_url", **{**DB_ENV, "PGSSLMODE": "verify-full"})
        self.assertTrue(custom.endswith("?sslmode=verify-full"))

    def test_explicit_url_wins_and_incomplete_identity_prints_nothing(self):
        explicit = "postgresql://other/db"
        self.assertEqual(
            run_common("session_db_url", **DB_ENV, TS_HARNESS_SESSION_DB_URL=explicit),
            explicit,
        )
        no_password = {k: v for k, v in DB_ENV.items() if k != "TS_HARNESS_DB_PASSWORD"}
        self.assertEqual(run_common("session_db_url", **no_password), "")
        placeholder = {**DB_ENV, "TS_HARNESS_DB_HOST": "__TF_RDS_ENDPOINT__"}
        self.assertEqual(run_common("session_db_url", **placeholder), "")

    def test_compose_helper_passes_current_password_on_every_call(self):
        script = """
sudo() { printf '%s\\n' "$@" | grep '^TS_HARNESS_SESSION_DB_URL='; }
run_docker_compose_as_admin up -d
TS_HARNESS_DB_PASSWORD=rotated
run_docker_compose_as_admin up -d
"""
        first, second = run_common(script, **DB_ENV, ADMIN_USER="athena").splitlines()
        self.assertIn("p%40ss%2Fw%3Ard%3F%23%25@", first)
        self.assertIn(":rotated@", second)

    def test_ops_scripts_export_session_url_before_recreating_runtime(self):
        for name, recreate in (
            ("rotate-secrets.sh", "\nrecreate_secret_consumers\n"),
            ("sync-compose-db-env.sh", "\nrecreate_ts_harness_services agent-runtime memory-api\n"),
        ):
            script = (REPO_ROOT / "scripts" / "ops" / name).read_text()
            export = script.index('TS_HARNESS_SESSION_DB_URL="$(session_db_url)"')
            self.assertLess(script.rindex('source "$COMPOSE_ENV"'), export)
            self.assertLess(export, script.rindex(recreate))


if __name__ == "__main__":
    unittest.main()
