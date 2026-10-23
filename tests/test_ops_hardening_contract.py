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
                # gitleaks 8.30.1 only. A shared patch would downgrade gh and osv-scanner.
                self.assertIn("go get github.com/klauspost/compress@v1.18.7", text)

    def test_agent_runtime_ships_pinned_test_toolchains(self):
        text = (REPO_ROOT / "docker" / "Dockerfile.agent-runtime").read_text()
        self.assertIn("COPY --from=go-tools /usr/local/go /usr/local/go", text)
        for name in ("NODE_VERSION", "MAVEN_VERSION", "GRADLE_VERSION", "RUSTUP_VERSION", "RUST_TOOLCHAIN"):
            self.assertRegex(text, rf"ARG {name}=\d+\.\d+\.\d+\n")
        for name in ("NODE_SHA256_AMD64", "NODE_SHA256_ARM64", "GRADLE_SHA256", "RUSTUP_SHA256_AMD64", "RUSTUP_SHA256_ARM64"):
            self.assertRegex(text, rf"ARG {name}=[0-9a-f]{{64}}\n")
        self.assertRegex(text, r"ARG MAVEN_SHA512=[0-9a-f]{128}\n")
        self.assertEqual(text.count("sha256sum -c -"), 3)
        self.assertIn("sha512sum -c -", text)
        for package in ("openjdk-21-jdk-headless", "make", "gcc", "libc6-dev"):
            self.assertRegex(text, rf"\n\s+{package} \\\n")
        self.assertIn("--profile minimal", text)
        self.assertIn("-d /opt/ts-harness/wheels", text)
        requirements = (REPO_ROOT / "docker" / "test-runner-requirements.txt").read_text()
        self.assertRegex(requirements, r"(?m)^pytest==\S+$")
        self.assertRegex(requirements, r"(?m)^behave==\S+$")



def _compose(name: str) -> dict:
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    for tag in ("!reset", "!override"):
        Loader.add_constructor(tag, lambda loader, node: {"tag": node.tag, "value": loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else None})
    return yaml.load((REPO_ROOT / "docker" / name).read_text(), Loader=Loader)


class RuntimeReplicasContractTests(unittest.TestCase):
    def test_second_runtime_has_its_own_memory_api_and_no_published_port(self):
        services = _compose("docker-compose.replicas.yml")["services"]
        runtime = services["agent-runtime-2"]
        self.assertEqual(runtime["extends"], {"file": "docker-compose.agent-runtime.yml", "service": "agent-runtime"})
        self.assertEqual(runtime["ports"]["tag"], "!reset")
        memory = services["memory-api-2"]
        self.assertEqual(memory["extends"]["service"], "memory-api")
        self.assertEqual(memory["network_mode"], "service:agent-runtime-2")
        self.assertEqual(memory["depends_on"]["value"], ["agent-runtime-2", "pgbouncer"])

    def test_memory_apis_reach_postgres_through_pgbouncer_in_transaction_mode(self):
        services = _compose("docker-compose.replicas.yml")["services"]
        bouncer = services["pgbouncer"]
        self.assertEqual(bouncer["environment"]["POOL_MODE"], "transaction")
        self.assertRegex(bouncer["image"], r"pgbouncer:v\d")
        for name in ("memory-api", "memory-api-2"):
            env = services[name]["environment"]
            self.assertEqual((env["TS_HARNESS_DB_HOST"], env["TS_HARNESS_DB_PORT"]), ("pgbouncer", "6432"))
            self.assertEqual(env["MEMORY_API_DB_POOLER"], "transaction")
        gateway = (REPO_ROOT / "scripts" / "memory-gateway.py").read_text()
        self.assertIn('kwargs["prepare_threshold"] = None', gateway)

    def test_compose_phase_adds_replicas_for_scale_out_only(self):
        phase = (REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh").read_text()
        scale_out = phase.index('"${DEPLOYMENT_PROFILE:-single_host}" == "scale_out"')
        replicas = phase.index("docker-compose.replicas.yml")
        self.assertLess(scale_out, replicas)
        self.assertLess(replicas, phase.index("PHASE_FINGERPRINT="))
        self.assertIn("AGENT_RUNTIME_REPLICAS:-2", phase)
        self.assertIn("2.24.4", phase)
        self.assertIn("runtime replicas need a session database", phase)

    def test_ops_recreate_and_restart_cover_both_runtimes(self):
        common = COMMON.read_text()
        self.assertIn("with_runtime_replicas", common)
        for function in ("recreate_ts_harness_services()", "restart_agent_runtime()"):
            body = common[common.index(function):]
            body = body[: body.index("\n}\n")]
            self.assertIn("with_runtime_replicas", body)
        self.assertNotIn("mapfile", common)

    def test_admin_console_balances_across_runtimes(self):
        conf = (REPO_ROOT / "docker" / "nginx-admin-ui.conf").read_text()
        self.assertIn("server agent-runtime:18789 resolve;", conf)
        self.assertIn("server agent-runtime-2:18789 resolve;", conf)


class LlmProxyContractTests(unittest.TestCase):
    def test_proxy_shares_the_runtime_namespace_and_holds_the_provider_keys(self):
        services = _compose("docker-compose.llm-proxy.yml")["services"]
        proxy = services["llm-proxy"]
        self.assertEqual(proxy["network_mode"], "service:agent-runtime")
        self.assertEqual(proxy["command"], ["python3", "-m", "ts_harness.llm_proxy"])
        self.assertEqual(proxy["environment"]["LLM_PROXY_PORT"], "18795")
        self.assertIn("TS_HARNESS_SESSION_DB_URL", proxy["environment"]["LLM_PROXY_DB_URL"])
        runtime = services["agent-runtime"]
        self.assertEqual(runtime["environment"]["LLM_PROXY_URL"], "http://127.0.0.1:18795/llm")
        self.assertEqual(runtime["ports"], ["${LLM_PROXY_HOST_PORT:-127.0.0.1:18795}:18795"])

    def test_replica_and_launcher_take_the_proxy_url_from_phase_12(self):
        replica = _compose("docker-compose.replicas.yml")["services"]["agent-runtime-2"]
        self.assertEqual(replica["environment"]["LLM_PROXY_URL"], "${LLM_PROXY_REPLICA_URL:-}")
        launcher = _compose("docker-compose.coding-jobs.yml")["services"]["coding-launcher"]["environment"]
        self.assertEqual(launcher["LLM_PROXY_URL"], "${LLM_PROXY_RUNTIME_URL:-}")
        self.assertEqual(launcher["CODING_WORKER_LLM_PROXY_URL"], "${CODING_WORKER_LLM_PROXY_URL:-}")

    def test_compose_phase_includes_the_proxy_when_enabled(self):
        phase = (REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh").read_text()
        enabled = phase.index('"${LLM_PROXY_ENABLED:-false}" == "true"')
        self.assertLess(enabled, phase.index("docker-compose.llm-proxy.yml"))
        self.assertLess(phase.index("docker-compose.llm-proxy.yml"), phase.index("PHASE_FINGERPRINT="))
        self.assertIn("needs LLM_PROXY_SECRET", phase)
        self.assertIn('LLM_PROXY_REPLICA_URL="http://agent-runtime:18795/llm"', phase)
        common = COMMON.read_text()
        for name in ("LLM_PROXY_SECRET \\", "LLM_PROXY_ENABLED", "CODING_WORKER_LLM_PROXY_URL"):
            self.assertIn(name, common)
        self.assertIn("printf '%s\\n' llm-proxy", common)
        fetch = (REPO_ROOT / "scripts" / "phases" / "10-fetch-secrets.sh").read_text()
        self.assertIn('write_env_var LLM_PROXY_SECRET "$LLM_PROXY_SECRET"', fetch)


if __name__ == "__main__":
    unittest.main()
