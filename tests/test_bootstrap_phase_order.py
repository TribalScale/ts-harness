import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
USER_DATA = REPO_ROOT / "scripts" / "user-data.sh"
BOOTSTRAP_PHASES = REPO_ROOT / "scripts" / "bootstrap" / "phases.sh"


class BootstrapPhaseOrderTests(unittest.TestCase):
    def test_user_data_runs_agent_runtime_setup_before_compose_up(self):
        text = USER_DATA.read_text()
        phases = BOOTSTRAP_PHASES.read_text()
        self.assertNotIn("08-clone-upstream.sh", text)
        self.assertNotIn("08-clone-upstream.sh", phases)
        self.assertIn("11-agent-runtime-setup.sh", phases)
        self.assertIn("12-compose-up.sh", phases)
        self.assertLess(
            phases.index("11-agent-runtime-setup.sh"),
            phases.index("12-compose-up.sh"),
        )
        self.assertIn("bootstrap/run.sh", text)

    def test_user_data_runs_coding_tools_install_after_auth_before_compose(self):
        text = USER_DATA.read_text()
        phases = BOOTSTRAP_PHASES.read_text()
        self.assertIn("11h-coding-tools-install.sh", phases)
        self.assertIn("11c-mcporter-init.sh", phases)
        self.assertLess(
            phases.index("11b-agent-auth-init.sh"),
            phases.index("11h-coding-tools-install.sh"),
        )
        self.assertLess(
            phases.index("11h-coding-tools-install.sh"),
            phases.index("12-compose-up.sh"),
        )
        self.assertIn("bootstrap/run.sh", text)
        phase = (REPO_ROOT / "scripts" / "phases" / "11h-coding-tools-install.sh").read_text()
        self.assertIn("TS_HARNESS_TOOLS_ROOT_HOST", phase)
        self.assertIn("https://opencode.ai/install", phase)
        self.assertIn("opencode-ai", phase)
        # Upstream install hardcodes ~/.opencode/bin; phase must recover from there.
        self.assertIn(".opencode/bin/opencode", phase)
        self.assertIn("find_opencode_binary", phase)
        # Absolute current -> /vol/... breaks after bind-mount to /tools in the container.
        self.assertIn('ln -sfn "${OPENCODE_VERSION}" "$CURRENT_LINK"', phase)
        self.assertIn("ensure_container_safe_layout", phase)
        self.assertIn("materialize_absolute_symlink", phase)
        self.assertNotIn('ln -sfn "$INSTALL_DIR" "$CURRENT_LINK"', phase)
        compose = (REPO_ROOT / "docker" / "docker-compose.agent-runtime.yml").read_text()
        self.assertIn("TS_HARNESS_TOOLS_ROOT_HOST", compose)
        self.assertIn("TS_HARNESS_DISPATCH_REQUEST_TIMEOUT", compose)
        self.assertIn(":/tools:ro", compose)
        common = (REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh").read_text()
        self.assertIn("CODING_BACKEND_API_KEY", common)
        self.assertIn("CODING_BACKEND_MODEL", common)
        self.assertIn("GITHUB_TOKEN", common)
        secrets_tf = (REPO_ROOT / "infra" / "aws" / "secrets.tf").read_text()
        self.assertIn("coding-backend-model", secrets_tf)
        dockerfile = (REPO_ROOT / "docker" / "Dockerfile.agent-runtime").read_text()
        self.assertIn("git", dockerfile)
        self.assertIn("gh", dockerfile)

    def test_hydrate_runtime_config_runs_after_secrets_before_yaml_render(self):
        phases = BOOTSTRAP_PHASES.read_text()
        self.assertIn("10a-hydrate-runtime-config.sh", phases)
        self.assertLess(
            phases.index("10-fetch-secrets.sh"),
            phases.index("10a-hydrate-runtime-config.sh"),
        )
        self.assertLess(
            phases.index("10a-hydrate-runtime-config.sh"),
            phases.index("11-agent-runtime-setup.sh"),
        )
        phase = (
            REPO_ROOT / "scripts" / "phases" / "10a-hydrate-runtime-config.sh"
        ).read_text()
        self.assertIn("cloud_fetch_runtime_config", phase)
        self.assertIn("write_instance_env", phase)

    def test_user_data_wires_implement_repo_from_terraform_tokens(self):
        text = USER_DATA.read_text()
        ec2 = (REPO_ROOT / "infra" / "aws" / "ec2.tf").read_text()
        runtime = (REPO_ROOT / "infra" / "aws" / "runtime_config.tf").read_text()
        variables = (REPO_ROOT / "infra" / "aws" / "variables.tf").read_text()
        self.assertNotIn("__TF_IMPLEMENT_WORK_ORDERS_REPO_URL__", text)
        self.assertNotIn("__TF_IMPLEMENT_WORK_ORDERS_BASE_BRANCH__", text)
        self.assertNotIn("__TF_IMPLEMENT_WORK_ORDERS_OPENCODE_VERSION__", text)
        self.assertNotIn("__TF_IMPLEMENT_WORK_ORDERS_REPO_URL__", ec2)
        self.assertIn("IMPLEMENT_WORK_ORDERS_REPO_URL", runtime)
        self.assertIn("OPENCODE_VERSION", runtime)
        self.assertIn('variable "implement_work_orders_repo_url"', variables)
        self.assertIn('variable "implement_work_orders_opencode_version"', variables)
        keys = (REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh").read_text()
        self.assertIn("IMPLEMENT_WORK_ORDERS_REPO_URL", keys)
        self.assertIn("OPENCODE_VERSION", keys)
        tmpl = (REPO_ROOT / "config" / "ts-harness-runtime.yaml.tmpl").read_text()
        self.assertIn('version: "{{OPENCODE_VERSION}}"', tmpl)

    def test_user_data_wires_oidc_from_terraform_tokens(self):
        text = USER_DATA.read_text()
        ec2 = (REPO_ROOT / "infra" / "aws" / "ec2.tf").read_text()
        gcp_startup = (REPO_ROOT / "scripts" / "gcp-startup.sh").read_text()
        gcp_compute = (REPO_ROOT / "infra" / "gcp" / "compute.tf").read_text()
        runtime = (REPO_ROOT / "infra" / "aws" / "runtime_config.tf").read_text()
        keys = (REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh").read_text()
        phase11 = (REPO_ROOT / "scripts" / "phases" / "11-agent-runtime-setup.sh").read_text()
        example = (REPO_ROOT / "infra" / "aws" / "terraform.tfvars.example").read_text()
        for token in (
            "__TF_IDENTITY_PROVIDER__",
            "__TF_OIDC_ISSUER__",
            "__TF_OIDC_AUDIENCE__",
            "__TF_OIDC_JWKS_URL__",
            "__TF_OIDC_CLIENT_ID__",
            "__TF_OIDC_AUTHORIZATION_ENDPOINT__",
            "__TF_OIDC_TOKEN_ENDPOINT__",
        ):
            self.assertNotIn(token, text)
            self.assertNotIn(token, ec2)
            self.assertNotIn(token, gcp_startup)
            self.assertNotIn(token, gcp_compute)
        for key in (
            "IDENTITY_PROVIDER",
            "OIDC_ISSUER",
            "OIDC_AUDIENCE",
            "OIDC_JWKS_URL",
            "OIDC_CLIENT_ID",
            "OIDC_AUTHORIZATION_ENDPOINT",
            "OIDC_TOKEN_ENDPOINT",
        ):
            self.assertIn(key, keys)
            self.assertIn(key, runtime)
        fingerprint = phase11[
            phase11.index("PHASE_FINGERPRINT="):
            phase11.index("if phase_marker_matches")
        ]
        self.assertIn("instance_env_path", fingerprint)
        self.assertIn("identity_provider", example)
        self.assertIn("oidc_issuer", example)
        self.assertIn("oidc_client_id", example)

    def test_runner_reaches_final_phase_when_a_phase_drains_stdin(self):
        listed = subprocess.run(
            ["bash", "-c", f'source "{BOOTSTRAP_PHASES}"; bootstrap_phase_list'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        with tempfile.TemporaryDirectory() as tmp:
            phases_dir = Path(tmp) / "phases"
            phases_dir.mkdir()
            ran_log = Path(tmp) / "ran.log"
            for name in listed:
                drain = "cat >/dev/null\n" if name == "14-channels-init.sh" else ""
                (phases_dir / name).write_text(
                    f'#!/bin/bash\n{drain}echo "{name}" >> "{ran_log}"\n'
                )
            env = {
                **os.environ,
                "PHASES_DIR": str(phases_dir),
                "DATA_DIR": str(Path(tmp) / "data"),
                "MULTI_AGENT_ENABLED": "true",
            }
            subprocess.run(
                ["bash", str(REPO_ROOT / "scripts" / "bootstrap" / "run.sh")],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(ran_log.read_text().split(), listed)

    def test_runtime_image_ships_eval_datasets_for_release_gates(self):
        dockerfile = (REPO_ROOT / "docker" / "Dockerfile.agent-runtime").read_text()
        self.assertIn("COPY evals ./evals", dockerfile)
        for dataset in ("agent_redteam", "agent_output"):
            self.assertTrue((REPO_ROOT / "evals" / dataset / "dataset.json").is_file())

    def test_agents_init_uses_ts_harness_config_dir(self):
        phase = (REPO_ROOT / "scripts" / "phases" / "11a-agents-init.sh").read_text()
        self.assertIn("TS_HARNESS_CONFIG_DIR", phase)
        self.assertIn("ts-harness-runtime.yaml.tmpl", phase)


if __name__ == "__main__":
    unittest.main()
