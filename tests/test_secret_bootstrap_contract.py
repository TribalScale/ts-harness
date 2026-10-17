import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON_SH = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"
FETCH_SECRETS = REPO_ROOT / "scripts" / "phases" / "10-fetch-secrets.sh"
USER_DATA = REPO_ROOT / "scripts" / "user-data.sh"
CLONE_ENTERPRISE = REPO_ROOT / "scripts" / "phases" / "09-clone-enterprise.sh"
ROTATE_SECRETS = REPO_ROOT / "scripts" / "ops" / "rotate-secrets.sh"
SYNC_COMPOSE_DB = REPO_ROOT / "scripts" / "ops" / "sync-compose-db-env.sh"


class SecretBootstrapContractTests(unittest.TestCase):
    def test_write_env_var_round_trips_shell_metacharacters(self):
        values = {
            "DOLLAR_VALUE": "pa$$word",
            "BACKTICK_VALUE": "value`uname`",
            "QUOTE_VALUE": "quote'\"mix",
            "SPACE_VALUE": " leading and trailing ",
            "SEMICOLON_VALUE": "alpha; touch /tmp/nope",
            "NEWLINE_VALUE": "line1\nline2",
        }

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "secrets.env"
            check_script = Path(tmp) / "check.sh"
            writer_lines = [
                "set -euo pipefail",
                f"source {COMMON_SH}",
                f": > {env_file}",
            ]
            for key, value in values.items():
                writer_lines.append(
                    f"write_env_var {key} {shlex.quote(value)} >> {env_file}"
                )
            writer_lines.append(f"source {env_file}")
            for key, value in values.items():
                writer_lines.append(
                    f"[[ \"${{{key}}}\" == {shlex.quote(value)} ]]"
                )
            check_script.write_text("\n".join(writer_lines) + "\n")

            result = subprocess.run(
                ["bash", str(check_script)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fetch_secrets_writes_source_safe_env_file(self):
        phase = FETCH_SECRETS.read_text()

        self.assertIn("write_env_var ANTHROPIC_API_KEY", phase)
        self.assertIn("write_env_var TS_HARNESS_DB_PASSWORD", phase)
        self.assertNotIn('cat > "$SECRETS_ENV" <<', phase)

    def test_local_backend_requires_persistent_db_password(self):
        phase = FETCH_SECRETS.read_text()

        self.assertNotIn("openssl rand -base64", phase)
        self.assertIn("db-password must be populated before bootstrap", phase)

    def test_bootstrap_clone_does_not_persist_github_token_in_remote_url(self):
        user_data = USER_DATA.read_text()

        self.assertNotIn("${BOOT_GH_TOKEN}@github.com", user_data)
        self.assertIn("x-access-token:%s", user_data)
        self.assertIn(
            "extraheader=Authorization: Basic ${BOOT_GH_BASIC_AUTH}",
            user_data,
        )
        self.assertIn('remote set-url origin "$ENTERPRISE_REPO_URL"', user_data)

    def test_enterprise_clone_phase_restores_clean_remote_url(self):
        phase = CLONE_ENTERPRISE.read_text()

        self.assertIn('remote set-url origin "$ENTERPRISE_REPO_URL"', phase)
        self.assertLess(
            phase.index('remote set-url origin "$ENTERPRISE_REPO_URL"'),
            phase.index("pull --ff-only"),
        )

    def test_rotate_secrets_validates_required_environment(self):
        script = ROTATE_SECRETS.read_text()

        for name in (
            "AWS_REGION",
            "SECRETS_PREFIX",
            "TS_HARNESS_CONFIG_DIR",
            "MEMORY_BACKEND",
            "EMBEDDINGS_PROVIDER",
        ):
            self.assertIn(f'require_env "{name}"', script)
        self.assertIn('DATA_DIR="${DATA_DIR:-/vol/ts-harness-data}"', script)

    def test_rotate_secrets_exports_phase_environment_before_refetch(self):
        script = ROTATE_SECRETS.read_text()

        export_index = script.index(
            "export AWS_REGION SECRETS_PREFIX DATA_DIR TS_HARNESS_CONFIG_DIR MEMORY_BACKEND EMBEDDINGS_PROVIDER"
        )
        refetch_index = script.index('bash "$PHASES_DIR/10-fetch-secrets.sh"')
        self.assertLess(export_index, refetch_index)

    def test_enterprise_clone_skip_path_uses_admin_remote_cleanup(self):
        phase = CLONE_ENTERPRISE.read_text()

        chown_index = phase.index('chown -R "$ADMIN_USER:$ADMIN_USER" "$ENTERPRISE_ASSETS_DIR"')
        cleanup_index = phase.index('sudo -u "$ADMIN_USER" git -C "$ENTERPRISE_ASSETS_DIR" remote set-url origin "$ENTERPRISE_REPO_URL"')
        marker_index = phase.index('if [[ -f "$PHASE_DONE" ]]')
        self.assertLess(chown_index, cleanup_index)
        self.assertLess(cleanup_index, marker_index)

    def test_sync_compose_db_env_fetches_live_managed_secret(self):
        script = SYNC_COMPOSE_DB.read_text()

        self.assertIn("cloud_fetch_managed_db_credentials", script)
        self.assertIn("write_runtime_env_file", script)
        self.assertIn("persist_env_key", script)
        self.assertIn("TS_HARNESS_DB_PASSWORD", script)
        self.assertIn("TS_HARNESS_DB_PASSWORD", script)
        source_index = script.index('source "$SECRETS_ENV"')
        fetch_index = script.index("cloud_fetch_managed_db_credentials")
        writer_index = script.index("write_runtime_env_file")
        self.assertLess(source_index, fetch_index)
        self.assertLess(fetch_index, writer_index)

    def test_rotate_secrets_uses_shared_runtime_env_writer(self):
        script = ROTATE_SECRETS.read_text()

        self.assertIn('write_runtime_env_file "$RUNTIME_ENV"', script)

    def test_rotate_secrets_refreshes_all_runtime_consumers(self):
        script = ROTATE_SECRETS.read_text()

        for key in (
            "GATEWAY_TOKEN",
            "TS_HARNESS_DB_PASSWORD",
            "OPENAI_API_KEY",
            "VOYAGE_API_KEY",
            "GEMINI_API_KEY",
            "MISTRAL_API_KEY",
            "CODING_BACKEND_API_KEY",
            "CODING_BACKEND_PROVIDER",
            "CODING_BACKEND_MODEL",
            "GITHUB_TOKEN",
        ):
            self.assertIn(key, script)
        self.assertIn("write_runtime_env_file", script)
        self.assertIn("agent-runtime", script)
        self.assertIn('source "$COMPOSE_ENV"', script)
        self.assertIn('source "$SECRETS_ENV"', script)
        self.assertIn("up -d --force-recreate agent-runtime", script)
        self.assertIn("up -d --force-recreate memory-api", script)

    def test_fetch_secrets_fails_closed_when_coding_backend_missing(self):
        phase = FETCH_SECRETS.read_text()
        self.assertIn("ERROR: CODING_BACKEND_API_KEY missing", phase)
        self.assertIn("ERROR: CODING_BACKEND_PROVIDER missing", phase)
        self.assertIn("coding-backend-api-key", phase)
        self.assertIn("coding-backend-provider", phase)
        self.assertIn("will omit CODING_BACKEND_API_KEY from runtime .env", phase)

    def test_secrets_tf_preserves_manual_secret_values(self):
        secrets_tf = (REPO_ROOT / "infra" / "aws" / "secrets.tf").read_text()
        self.assertIn("ignore_changes", secrets_tf)
        self.assertIn("secret_string", secrets_tf)


if __name__ == "__main__":
    unittest.main()
