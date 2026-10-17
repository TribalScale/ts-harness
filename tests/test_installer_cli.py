"""Unit tests for the unified AWS/GCP installer CLI (Phase 6)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install_checks import (
    CODE_AWS_CLI_MISSING,
    CODE_CLOUD_FLAG_MISMATCH,
    CODE_CONFIG_IDENTITY_MISSING,
    CODE_GCLOUD_CLI_MISSING,
    CODE_STATE_PROVIDER_CONFLICT,
    CODE_TERRAFORM_MISSING,
    CheckResult,
    run_preflight,
)
from install_contract import load_install_config
from install_post_apply import (
    REQUIRED_SECRET_KEYS,
    format_secret_population_commands,
    list_required_secrets,
    render_post_apply_next_steps,
)
import install as install_mod


REPO_ROOT = Path(__file__).resolve().parents[1]
AWS_EXAMPLE = REPO_ROOT / "config" / "install" / "aws.example.yaml"
GCP_EXAMPLE = REPO_ROOT / "config" / "install" / "gcp.example.yaml"
SCRIPTS = REPO_ROOT / "scripts"


class InstallerCliTests(unittest.TestCase):
    def test_install_scripts_exist_and_are_executable_friendly(self):
        self.assertTrue((SCRIPTS / "install.py").is_file())
        self.assertTrue((SCRIPTS / "install.sh").is_file())
        self.assertTrue((SCRIPTS / "install_checks.py").is_file())
        self.assertTrue((SCRIPTS / "install_post_apply.py").is_file())
        sh = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
        self.assertIn("install.py", sh)

    def test_validate_aws_example_succeeds(self):
        with mock.patch.object(
            install_mod,
            "run_preflight",
            return_value=[CheckResult(True, "OK", "ok")],
        ):
            code = install_mod.main(
                ["validate", "--cloud", "aws", "--config", str(AWS_EXAMPLE)]
            )
        self.assertEqual(code, 0)

    def test_validate_gcp_example_succeeds(self):
        with mock.patch.object(
            install_mod,
            "run_preflight",
            return_value=[CheckResult(True, "OK", "ok")],
        ):
            code = install_mod.main(
                ["validate", "--cloud", "gcp", "--config", str(GCP_EXAMPLE)]
            )
        self.assertEqual(code, 0)

    def test_validate_rejects_cloud_flag_mismatch(self):
        code = install_mod.main(
            ["validate", "--cloud", "gcp", "--config", str(AWS_EXAMPLE)]
        )
        self.assertNotEqual(code, 0)

    def test_select_terraform_root_never_both(self):
        aws_root = install_mod.select_terraform_root("aws")
        gcp_root = install_mod.select_terraform_root("gcp")
        self.assertEqual(aws_root, REPO_ROOT / "infra" / "aws")
        self.assertEqual(gcp_root, REPO_ROOT / "infra" / "gcp")
        self.assertNotEqual(aws_root, gcp_root)

    def test_generate_tfvars_aws_maps_core_fields(self):
        cfg = load_install_config(AWS_EXAMPLE)
        tfvars = install_mod.generate_tfvars(cfg, cloud="aws")
        self.assertEqual(tfvars["aws_region"], "us-east-1")
        self.assertEqual(tfvars["project_name"], "ts-harness")
        self.assertEqual(tfvars["instance_type"], "t3.medium")
        self.assertEqual(tfvars["ebs_size"], 60)
        self.assertEqual(tfvars["root_volume_size"], 40)
        self.assertEqual(tfvars["ssh_key_name"], "ts-harness-prod")
        self.assertEqual(tfvars["memory_backend"], "rds")
        self.assertEqual(tfvars["rds_instance_class"], "db.t3.micro")
        self.assertEqual(tfvars["budget_alert_email"], "ops@example.com")
        self.assertNotIn("gcp_project_id", tfvars)

    def test_generate_tfvars_maps_oidc_runtime_fields(self):
        cfg = load_install_config(AWS_EXAMPLE)
        tfvars = install_mod.generate_tfvars(cfg, cloud="aws")
        self.assertEqual(tfvars["identity_provider"], "static")
        self.assertEqual(tfvars.get("oidc_issuer", ""), "")

        cfg["runtime"] = dict(cfg["runtime"])
        cfg["runtime"].update(
            {
                "identity_provider": "oidc",
                "oidc_issuer": "https://tenant.auth0.com/",
                "oidc_audience": "ts-harness",
                "oidc_jwks_url": "https://tenant.auth0.com/.well-known/jwks.json",
                "oidc_client_id": "spa-client-id",
                "oidc_authorization_endpoint": "https://tenant.auth0.com/authorize",
                "oidc_token_endpoint": "https://tenant.auth0.com/oauth/token",
            }
        )
        tfvars = install_mod.generate_tfvars(cfg, cloud="aws")
        self.assertEqual(tfvars["identity_provider"], "oidc")
        self.assertEqual(tfvars["oidc_issuer"], "https://tenant.auth0.com/")
        self.assertEqual(tfvars["oidc_client_id"], "spa-client-id")
        gcp = load_install_config(GCP_EXAMPLE)
        gcp_tfvars = install_mod.generate_tfvars(gcp, cloud="gcp")
        self.assertEqual(gcp_tfvars["identity_provider"], "static")

    def test_generate_tfvars_gcp_maps_core_fields(self):
        cfg = load_install_config(GCP_EXAMPLE)
        tfvars = install_mod.generate_tfvars(cfg, cloud="gcp")
        self.assertEqual(tfvars["cloud_provider"], "gcp")
        self.assertEqual(tfvars["gcp_project_id"], "example-gcp-project")
        self.assertEqual(tfvars["cloud_region"], "us-central1")
        self.assertEqual(tfvars["vm_instance_type"], "e2-medium")
        self.assertEqual(tfvars["memory_backend"], "managed")
        self.assertEqual(tfvars["managed_db_name"], "ts_harness")
        self.assertNotIn("aws_region", tfvars)
        self.assertNotIn("aws_account_id", tfvars)

    def test_plan_runs_terraform_only_in_selected_root(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="plan ok\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            state_base = Path(tmp) / ".install"
            with mock.patch.object(install_mod, "run_terraform", side_effect=fake_run):
                with mock.patch.object(
                    install_mod,
                    "run_preflight",
                    return_value=[CheckResult(True, "OK", "ok")],
                ):
                    code = install_mod.main(
                        [
                            "plan",
                            "--cloud",
                            "aws",
                            "--config",
                            str(AWS_EXAMPLE),
                            "--state-dir",
                            str(state_base),
                            "--dry-run",
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertTrue(calls)
            for cmd in calls:
                joined = " ".join(cmd)
                self.assertIn("infra/aws", joined)
                self.assertNotIn("infra/gcp", joined)

            meta_path = state_base / "aws" / "prod" / "install-metadata.json"
            self.assertTrue(meta_path.is_file())
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["cloud_provider"], "aws")
            self.assertEqual(meta["environment"], "prod")
            self.assertEqual(meta["terraform_root"], "infra/aws")

    def test_apply_updates_metadata_and_prints_next_steps(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="apply ok\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            state_base = Path(tmp) / ".install"
            buf: list[str] = []

            def capture_print(*args, **_kwargs):
                buf.append(" ".join(str(a) for a in args))

            with mock.patch.object(install_mod, "run_terraform", side_effect=fake_run):
                with mock.patch.object(
                    install_mod,
                    "run_preflight",
                    return_value=[CheckResult(True, "OK", "ok")],
                ):
                    with mock.patch("builtins.print", side_effect=capture_print):
                        code = install_mod.main(
                            [
                                "apply",
                                "--cloud",
                                "gcp",
                                "--config",
                                str(GCP_EXAMPLE),
                                "--state-dir",
                                str(state_base),
                                "--dry-run",
                            ]
                        )
            self.assertEqual(code, 0)
            meta = json.loads(
                (state_base / "gcp" / "prod" / "install-metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(meta["cloud_provider"], "gcp")
            self.assertEqual(meta["last_command"], "apply")
            joined = "\n".join(buf)
            self.assertIn("secret", joined.lower())
            self.assertIn("post-deploy", joined.lower())

    def test_destroy_requires_confirm_matching_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_base = Path(tmp) / ".install"
            with mock.patch.object(install_mod, "run_terraform") as tf:
                code = install_mod.main(
                    [
                        "destroy",
                        "--cloud",
                        "aws",
                        "--config",
                        str(AWS_EXAMPLE),
                        "--state-dir",
                        str(state_base),
                        "--confirm",
                        "staging",
                        "--dry-run",
                    ]
                )
            self.assertNotEqual(code, 0)
            tf.assert_not_called()

    def test_destroy_with_confirm_runs_terraform(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            state_base = Path(tmp) / ".install"
            with mock.patch.object(install_mod, "run_terraform", side_effect=fake_run):
                with mock.patch.object(
                    install_mod,
                    "run_preflight",
                    return_value=[CheckResult(True, "OK", "ok")],
                ):
                    code = install_mod.main(
                        [
                            "destroy",
                            "--cloud",
                            "aws",
                            "--config",
                            str(AWS_EXAMPLE),
                            "--state-dir",
                            str(state_base),
                            "--confirm",
                            "prod",
                            "--dry-run",
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertTrue(any("destroy" in c for cmd in calls for c in cmd))

    def test_status_from_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_dir = Path(tmp) / "prod"
            env_dir.mkdir(parents=True)
            (env_dir / "install-metadata.json").write_text(
                json.dumps(
                    {
                        "cloud_provider": "aws",
                        "environment": "prod",
                        "terraform_root": "infra/aws",
                        "last_command": "apply",
                        "status": "applied",
                    }
                ),
                encoding="utf-8",
            )
            buf: list[str] = []
            with mock.patch(
                "builtins.print",
                side_effect=lambda *a, **k: buf.append(" ".join(map(str, a))),
            ):
                code = install_mod.main(["status", "--state-dir", str(env_dir)])
            self.assertEqual(code, 0)
            joined = "\n".join(buf)
            self.assertIn("aws", joined)
            self.assertIn("prod", joined)

    def test_aws_and_gcp_state_dirs_do_not_collide(self):
        aws_cfg = load_install_config(AWS_EXAMPLE)
        gcp_cfg = load_install_config(GCP_EXAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / ".install"
            aws_dir = install_mod.resolve_state_dir(
                config=aws_cfg, state_dir=base, cloud="aws"
            )
            gcp_dir = install_mod.resolve_state_dir(
                config=gcp_cfg, state_dir=base, cloud="gcp"
            )
            self.assertNotEqual(aws_dir, gcp_dir)
            self.assertEqual(aws_dir, base / "aws" / "prod")
            self.assertEqual(gcp_dir, base / "gcp" / "prod")

    def test_status_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_base = Path(tmp) / ".install"
            env_dir = state_base / "gcp" / "prod"
            env_dir.mkdir(parents=True)
            (env_dir / "install-metadata.json").write_text(
                json.dumps(
                    {
                        "cloud_provider": "gcp",
                        "environment": "prod",
                        "terraform_root": "infra/gcp",
                        "status": "planned",
                    }
                ),
                encoding="utf-8",
            )
            buf: list[str] = []
            with mock.patch(
                "builtins.print",
                side_effect=lambda *a, **k: buf.append(" ".join(map(str, a))),
            ):
                code = install_mod.main(
                    [
                        "status",
                        "--config",
                        str(GCP_EXAMPLE),
                        "--state-dir",
                        str(state_base),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("gcp", "\n".join(buf))


class InstallChecksTests(unittest.TestCase):
    def test_check_result_shape(self):
        r = CheckResult(ok=False, code=CODE_TERRAFORM_MISSING, message="missing")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "TERRAFORM_MISSING")

    def test_preflight_detects_missing_terraform(self):
        cfg = load_install_config(AWS_EXAMPLE)
        results = run_preflight(
            cloud="aws",
            config=cfg,
            state_dir=None,
            which=lambda name: None if name == "terraform" else f"/bin/{name}",
        )
        codes = {r.code for r in results if not r.ok}
        self.assertIn(CODE_TERRAFORM_MISSING, codes)

    def test_preflight_detects_missing_provider_cli(self):
        cfg = load_install_config(AWS_EXAMPLE)

        def which(name: str):
            if name == "aws":
                return None
            return f"/bin/{name}"

        results = run_preflight(cloud="aws", config=cfg, state_dir=None, which=which)
        codes = {r.code for r in results if not r.ok}
        self.assertIn(CODE_AWS_CLI_MISSING, codes)

        cfg_gcp = load_install_config(GCP_EXAMPLE)

        def which_gcp(name: str):
            if name == "gcloud":
                return None
            return f"/bin/{name}"

        results_gcp = run_preflight(
            cloud="gcp", config=cfg_gcp, state_dir=None, which=which_gcp
        )
        codes_gcp = {r.code for r in results_gcp if not r.ok}
        self.assertIn(CODE_GCLOUD_CLI_MISSING, codes_gcp)

    def test_preflight_config_identity(self):
        cfg = dict(load_install_config(AWS_EXAMPLE))
        cfg.pop("aws_account_id", None)
        results = run_preflight(
            cloud="aws",
            config=cfg,
            state_dir=None,
            which=lambda n: f"/bin/{n}",
            skip_schema=True,
        )
        codes = {r.code for r in results if not r.ok}
        self.assertIn(CODE_CONFIG_IDENTITY_MISSING, codes)

    def test_preflight_state_provider_conflict(self):
        cfg = load_install_config(AWS_EXAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "install-metadata.json").write_text(
                json.dumps({"cloud_provider": "gcp", "environment": "prod"}),
                encoding="utf-8",
            )
            results = run_preflight(
                cloud="aws",
                config=cfg,
                state_dir=state_dir,
                which=lambda n: f"/bin/{n}",
            )
            codes = {r.code for r in results if not r.ok}
            self.assertIn(CODE_STATE_PROVIDER_CONFLICT, codes)

    def test_preflight_cloud_flag_mismatch(self):
        cfg = load_install_config(AWS_EXAMPLE)
        results = run_preflight(
            cloud="gcp",
            config=cfg,
            state_dir=None,
            which=lambda n: f"/bin/{n}",
        )
        codes = {r.code for r in results if not r.ok}
        self.assertIn(CODE_CLOUD_FLAG_MISMATCH, codes)

    def test_validate_wires_preflight_failures(self):
        with mock.patch.object(
            install_mod,
            "run_preflight",
            return_value=[
                CheckResult(False, CODE_TERRAFORM_MISSING, "terraform not found"),
            ],
        ):
            code = install_mod.main(
                ["validate", "--cloud", "aws", "--config", str(AWS_EXAMPLE)]
            )
        self.assertNotEqual(code, 0)


class InstallPostApplyTests(unittest.TestCase):
    def test_required_secrets_include_catalog_and_embeddings(self):
        secrets = list_required_secrets(embeddings_provider="openai")
        for key in ("anthropic-api-key", "gateway-token", "db-password", "openai-api-key"):
            self.assertIn(key, secrets)
        self.assertTrue(set(REQUIRED_SECRET_KEYS).issubset(set(secrets)))

    def test_aws_secret_commands_use_put_secret_value(self):
        text = format_secret_population_commands(
            cloud="aws",
            secret_prefix="ts-harness",
            project_id=None,
            secrets=["gateway-token"],
        )
        self.assertIn("aws secretsmanager put-secret-value", text)
        self.assertIn("ts-harness/gateway-token", text)

    def test_gcp_secret_commands_use_gcloud(self):
        text = format_secret_population_commands(
            cloud="gcp",
            secret_prefix="ts-harness",
            project_id="example-gcp-project",
            secrets=["gateway-token"],
        )
        self.assertIn("gcloud secrets versions add", text)
        self.assertIn("ts-harness-gateway-token", text)

    def test_next_steps_mention_smoke_and_no_tf_secret_values(self):
        text = render_post_apply_next_steps(
            cloud="aws",
            secret_prefix="ts-harness",
            project_id=None,
            embeddings_provider="openai",
        )
        self.assertIn("post-deploy-smoke-test", text)
        self.assertIn("Do not write secret values into Terraform state", text)
        self.assertIn("aws secretsmanager put-secret-value", text)

    def test_install_sh_invokes_python(self):
        if not shutil.which("bash"):
            self.skipTest("bash required")
        result = subprocess.run(
            ["bash", str(SCRIPTS / "install.sh"), "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("validate", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
