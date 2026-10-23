import glob
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_AUTH = REPO_ROOT / "scripts" / "phases" / "11b-agent-auth-init.sh"
CHANNELS_PHASE = REPO_ROOT / "scripts" / "phases" / "14-channels-init.sh"
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"
CRON_PHASE = REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh"
HEALTH_CHECK = REPO_ROOT / "scripts" / "ops" / "gateway-health-check.sh"
ROTATE_SECRETS = REPO_ROOT / "scripts" / "ops" / "rotate-secrets.sh"
USER_DATA = REPO_ROOT / "scripts" / "user-data.sh"


class OpsHardeningContractTests(unittest.TestCase):
    def test_shellcheck_globs_only_include_shell_scripts(self):
        shellcheck_patterns = (
            "scripts/*.sh",
            "scripts/phases/*.sh",
            "scripts/ops/*.sh",
        )
        for pattern in shellcheck_patterns:
            for script in glob.glob(str(REPO_ROOT / pattern)):
                shebang = Path(script).read_text().splitlines()[0]
                self.assertRegex(
                    shebang,
                    r"^#!.*(ba)?sh\b",
                    (
                        f"{Path(script).relative_to(REPO_ROOT)} is matched "
                        "by ShellCheck but is not a shell script"
                    ),
                )

    def test_agent_auth_phase_sources_common_for_standalone_runs(self):
        phase = AGENT_AUTH.read_text()

        self.assertIn('source "${SCRIPT_DIR_LIB}/lib/common.sh"', phase)

    def test_channel_onboarding_uses_reconstructed_compose_files(self):
        phase = CHANNELS_PHASE.read_text()

        self.assertIn(
            "recreate_ts_harness_services agent-runtime memory-api",
            phase,
        )

    def test_common_has_secret_rotation_helpers(self):
        common = COMMON.read_text()

        self.assertIn("persist_env_key()", common)
        self.assertIn("write_runtime_env_file()", common)
        self.assertIn("sync_google_workspace_sa_json()", common)
        self.assertIn("restart_ts_harness_services()", common)
        self.assertIn("recreate_ts_harness_services()", common)

    def test_write_runtime_env_file_includes_db_credentials(self):
        common = COMMON.read_text()
        writer = common[
            common.index("write_runtime_env_file()"):
            common.index("enterprise_assets_commit()")
        ]

        for key in (
            "TS_HARNESS_DB_HOST",
            "TS_HARNESS_DB_PORT",
            "TS_HARNESS_DB_USER",
            "TS_HARNESS_DB_NAME",
            "TS_HARNESS_DB_PASSWORD",
            "TS_HARNESS_DB_HOST",
            "TS_HARNESS_DB_PORT",
            "TS_HARNESS_DB_USER",
            "TS_HARNESS_DB_NAME",
            "TS_HARNESS_DB_PASSWORD",
            "PGSSLMODE",
        ):
            self.assertIn(key, writer)

    def test_channels_phase_preserves_google_workspace_runtime_env(self):
        phase = CHANNELS_PHASE.read_text()

        self.assertIn("write_runtime_env_file", phase)
        self.assertIn("GOOGLE_WORKSPACE_SA_JSON_PATH", phase)
        self.assertIn("GOOGLE_OAUTH_CLIENT_ID", phase)

    def test_agent_runtime_compose_mounts_google_workspace_sa_json(self):
        compose = (
            REPO_ROOT / "docker" / "docker-compose.agent-runtime.yml"
        ).read_text()

        self.assertIn(
            "google-workspace-sa.json:/config/google-workspace-sa.json:ro",
            compose,
        )

    def test_agent_runtime_compose_mounts_coding_tools_root(self):
        compose = (
            REPO_ROOT / "docker" / "docker-compose.agent-runtime.yml"
        ).read_text()
        common = COMMON.read_text()

        self.assertIn("TS_HARNESS_TOOLS_ROOT_HOST", compose)
        self.assertIn(":/tools:ro", compose)
        self.assertIn("CODING_BACKEND_API_KEY", common)
        self.assertIn("CODING_BACKEND_MODEL", common)
        self.assertIn("GITHUB_TOKEN", common)

    def test_agent_runtime_compose_persists_workflow_definitions(self):
        compose = (
            REPO_ROOT / "docker" / "docker-compose.agent-runtime.yml"
        ).read_text()

        self.assertIn("ts-harness-workflow-definitions:", compose)
        self.assertGreaterEqual(
            compose.count(
                "ts-harness-workflow-definitions:"
                "/workflow-definitions:rw"
            ),
            2,
        )
        self.assertGreaterEqual(
            compose.count(
                "TS_HARNESS_WORKFLOW_DEFINITION_STORE: "
                "/workflow-definitions"
            ),
            2,
        )
        self.assertGreaterEqual(
            compose.count(
                "TS_HARNESS_MANAGED_WORKFLOWS_DIR: "
                "/workflow-definitions/active"
            ),
            2,
        )
        self.assertGreaterEqual(
            compose.count(
                "TS_HARNESS_PROGRAM_DEFINITION_STORE: "
                "/workflow-definitions/programs"
            ),
            2,
        )

    def test_runtime_restart_exports_memory_api_compose_environment(self):
        common = COMMON.read_text()
        helper = common[
            common.index("run_docker_compose_as_admin()"):
            common.index("restart_agent_runtime()")
        ]

        for name in (
            "TS_HARNESS_CONFIG_DIR",
            "ENTERPRISE_ASSETS_DIR",
            "TS_HARNESS_TOOLS_ROOT_HOST",
            "TS_HARNESS_TOOLS_ROOT",
            "AWS_REGION",
            "EMBEDDINGS_PROVIDER",
            "EMBEDDINGS_MODEL",
            "EMBEDDINGS_DIMENSIONS",
            "OPENAI_API_KEY",
            "VOYAGE_API_KEY",
            "GEMINI_API_KEY",
            "MISTRAL_API_KEY",
        ):
            self.assertIn(f"{name}=", helper)

    def test_rotation_refreshes_runtime_secret_env(self):
        rotate = ROTATE_SECRETS.read_text()

        self.assertIn("persist_runtime_secret_keys", rotate)
        self.assertIn("RUNTIME_ENV", rotate)
        for key in (
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
            "OPENAI_API_KEY",
            "VOYAGE_API_KEY",
            "GEMINI_API_KEY",
            "MISTRAL_API_KEY",
        ):
            self.assertIn(key, rotate)

    def test_rotation_handles_gateway_token_and_runtime_recreate(self):
        rotate = ROTATE_SECRETS.read_text()

        self.assertIn("GATEWAY_TOKEN", rotate)
        self.assertIn("refresh_compose_env", rotate)
        self.assertIn("agent-runtime", rotate)
        self.assertIn("memory-api", rotate)

    def test_health_cron_sets_path_for_aws_cli(self):
        phase = CRON_PHASE.read_text()

        self.assertIn("PATH=/usr/local/bin:/usr/bin:/bin", phase)

    def test_health_check_logs_aws_cli_failures(self):
        health = HEALTH_CHECK.read_text()

        self.assertIn("AWS_BIN", health)
        self.assertIn("/usr/local/bin/aws", health)
        self.assertIn("CloudWatch metric publish failed", health)

    def test_user_data_bootstrap_aws_cli_is_arch_aware(self):
        user_data = USER_DATA.read_text()

        self.assertIn("uname -m", user_data)
        self.assertIn("awscli-exe-linux-aarch64.zip", user_data)
        self.assertIn("awscli-exe-linux-x86_64.zip", user_data)
        self.assertNotIn(
            (
                'curl -s "https://awscli.amazonaws.com/'
                'awscli-exe-linux-x86_64.zip"'
            ),
            user_data,
        )

    def test_restore_drill_smoke_script_checks_db_and_config(self):
        script = (
            REPO_ROOT / "scripts" / "ops" / "restore-drill-smoke.sh"
        ).read_text()
        self.assertIn("pg_isready", script)
        self.assertIn("TS_HARNESS_CONFIG", script)

    def test_migrate_db_script_tracks_schema_migrations(self):
        script = (REPO_ROOT / "scripts" / "ops" / "migrate-db.sh").read_text()
        self.assertIn("schema_migrations", script)
        self.assertIn("007_tenant_isolation.sql", script)
        self.assertIn("009_scim.sql", script)
        self.assertIn("012_runtime_transcripts.sql", script)
        self.assertIn("013_workflows_source_channel.sql", script)
        self.assertIn("014_workflow_wait_tokens.sql", script)
        self.assertIn("015_workflow_superseded.sql", script)

    def test_images_build_security_tools_from_pinned_commits(self):
        for dockerfile in (REPO_ROOT / "docker" / "Dockerfile.agent-runtime",):
            with self.subTest(dockerfile=dockerfile.name):
                text = dockerfile.read_text()
                for tool in ("GH", "GITLEAKS", "OSV_SCANNER"):
                    self.assertRegex(text, rf"ARG {tool}_VERSION=\S+")
                    self.assertRegex(text, rf"ARG {tool}_COMMIT=[0-9a-f]{{40}}\n")
                self.assertIn('test "$(git -C "/src/$4" rev-parse HEAD)" = "$3"', text)
                self.assertIn("COPY --from=go-tools /out/gh /out/gitleaks /out/osv-scanner", text)
                self.assertIn("uv tool install --constraints /tmp/constraints.txt pip-audit", text)


if __name__ == "__main__":
    unittest.main()
