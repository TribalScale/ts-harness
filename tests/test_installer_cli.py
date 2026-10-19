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
    CODE_SECRET_PREFIX_CHANGED,
    CODE_TERRAFORM_TOO_OLD,
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


def _ok_preflight():
    return mock.patch.object(
        install_mod,
        "run_preflight",
        return_value=[CheckResult(True, "OK", "ok")],
    )


def _subcommand(cmd: list[str]) -> str:
    return next(c for c in cmd[1:] if not c.startswith("-"))


class InstallerPipelineTests(unittest.TestCase):
    """Behaviour needed to run plan/apply from CI."""

    def _run(self, argv, *, returncodes=None):
        calls: list[list[str]] = []
        returncodes = returncodes or {}

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            rc = returncodes.get(_subcommand(list(cmd)), 0)
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

        with mock.patch.object(install_mod, "run_terraform", side_effect=fake_run):
            with _ok_preflight():
                code = install_mod.main(argv)
        return code, calls

    def test_backend_config_args_s3_defaults_to_native_lockfile(self):
        cfg = load_install_config(AWS_EXAMPLE)
        self.assertEqual(
            install_mod.backend_config_args(cfg),
            [
                "-backend-config=bucket=example-ts-harness-tfstate",
                "-backend-config=key=prod/aws/terraform.tfstate",
                "-backend-config=region=us-east-1",
                "-backend-config=encrypt=true",
                "-backend-config=use_lockfile=true",
            ],
        )

    def test_backend_config_args_s3_uses_lock_table_when_set(self):
        cfg = load_install_config(AWS_EXAMPLE)
        cfg["terraform_state"] = {**cfg["terraform_state"], "lock_table": "terraform-locks"}
        args = install_mod.backend_config_args(cfg)
        self.assertIn("-backend-config=dynamodb_table=terraform-locks", args)
        self.assertNotIn("-backend-config=use_lockfile=true", args)

    def test_backend_config_args_gcs_maps_key_to_prefix(self):
        cfg = load_install_config(GCP_EXAMPLE)
        state = cfg["terraform_state"]
        self.assertEqual(
            install_mod.backend_config_args(cfg),
            [
                f"-backend-config=bucket={state['bucket']}",
                f"-backend-config=prefix={state['key']}",
            ],
        )

    def test_contract_accepts_lock_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = AWS_EXAMPLE.read_text(encoding="utf-8").replace(
                "\n  region: us-east-1\n",
                "\n  region: us-east-1\n  lock_table: terraform-locks\n",
                1,
            )
            self.assertIn("lock_table", text)
            path = Path(tmp) / "aws.yaml"
            path.write_text(text, encoding="utf-8")
            cfg = load_install_config(path)
        self.assertEqual(cfg["terraform_state"]["lock_table"], "terraform-locks")

    def test_contract_rejects_lock_table_on_gcs(self):
        from install_contract import InstallContractError

        with tempfile.TemporaryDirectory() as tmp:
            text = GCP_EXAMPLE.read_text(encoding="utf-8")
            marker = "backend_type: gcs\n"
            self.assertIn(marker, text)
            path = Path(tmp) / "gcp.yaml"
            path.write_text(
                text.replace(marker, marker + "  lock_table: terraform-locks\n"),
                encoding="utf-8",
            )
            with self.assertRaises(InstallContractError):
                load_install_config(path)

    def test_init_receives_backend_config_for_every_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ["--cloud", "aws", "--config", str(AWS_EXAMPLE), "--state-dir", tmp]
            for argv in (
                ["plan", *base],
                ["apply", *base],
                ["destroy", *base, "--confirm", "prod"],
            ):
                code, calls = self._run(argv)
                self.assertEqual(code, 0, argv)
                init = next(c for c in calls if _subcommand(c) == "init")
                self.assertIn("-backend-config=bucket=example-ts-harness-tfstate", init)

    def test_plan_with_plan_file_saves_plan_to_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            rel = os.path.relpath(Path(tmp) / "prod.tfplan")
            code, calls = self._run(
                ["plan", "--cloud", "aws", "--config", str(AWS_EXAMPLE),
                 "--state-dir", tmp, "--plan-file", rel]
            )
        self.assertEqual(code, 0)
        plan = next(c for c in calls if _subcommand(c) == "plan")
        self.assertIn(f"-out={Path(rel).resolve()}", plan)

    def test_apply_with_plan_file_applies_saved_plan_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            rel = os.path.relpath(Path(tmp) / "prod.tfplan")
            code, calls = self._run(
                ["apply", "--cloud", "aws", "--config", str(AWS_EXAMPLE),
                 "--state-dir", tmp, "--plan-file", rel]
            )
        self.assertEqual(code, 0)
        apply = next(c for c in calls if _subcommand(c) == "apply")
        self.assertEqual(apply[-1], str(Path(rel).resolve()))
        self.assertNotIn("-auto-approve", apply)
        self.assertFalse(any(c.startswith("-var-file=") for c in apply))

    def test_apply_without_plan_file_keeps_auto_approve(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, calls = self._run(
                ["apply", "--cloud", "aws", "--config", str(AWS_EXAMPLE), "--state-dir", tmp]
            )
        self.assertEqual(code, 0)
        apply = next(c for c in calls if _subcommand(c) == "apply")
        self.assertIn("-auto-approve", apply)

    def test_plan_detailed_exitcode_returns_2_when_changes_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, calls = self._run(
                ["plan", "--cloud", "aws", "--config", str(AWS_EXAMPLE),
                 "--state-dir", tmp, "--detailed-exitcode"],
                returncodes={"plan": 2},
            )
            meta = json.loads(
                (Path(tmp) / "aws" / "prod" / "install-metadata.json").read_text("utf-8")
            )
        self.assertEqual(code, 2)
        self.assertEqual(meta["status"], "planned")
        plan = next(c for c in calls if _subcommand(c) == "plan")
        self.assertIn("-detailed-exitcode", plan)

    def test_plan_detailed_exitcode_returns_0_when_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self._run(
                ["plan", "--cloud", "aws", "--config", str(AWS_EXAMPLE),
                 "--state-dir", tmp, "--detailed-exitcode"],
            )
            meta = json.loads(
                (Path(tmp) / "aws" / "prod" / "install-metadata.json").read_text("utf-8")
            )
        self.assertEqual(code, 0)
        self.assertEqual(meta["status"], "planned_no_changes")

    def test_plan_exit_2_without_detailed_flag_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self._run(
                ["plan", "--cloud", "aws", "--config", str(AWS_EXAMPLE), "--state-dir", tmp],
                returncodes={"plan": 2},
            )
        self.assertEqual(code, 2)

    def test_var_file_is_absolute_with_relative_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            rel = os.path.relpath(tmp)
            code, calls = self._run(
                ["plan", "--cloud", "aws", "--config", str(AWS_EXAMPLE), "--state-dir", rel]
            )
        self.assertEqual(code, 0)
        plan = next(c for c in calls if _subcommand(c) == "plan")
        var_file = next(c for c in plan if c.startswith("-var-file="))
        self.assertTrue(Path(var_file.split("=", 1)[1]).is_absolute(), var_file)

    def test_generate_tfvars_aws_passes_account_id(self):
        cfg = load_install_config(AWS_EXAMPLE)
        tfvars = install_mod.generate_tfvars(cfg, cloud="aws")
        self.assertEqual(tfvars["aws_account_id"], "123456789012")

    def test_generate_tfvars_aws_passes_secret_prefix(self):
        cfg = load_install_config(AWS_EXAMPLE)
        cfg["secret_prefix"] = "acme"
        tfvars = install_mod.generate_tfvars(cfg, cloud="aws")
        self.assertEqual(tfvars["secret_prefix"], "acme")

    def test_run_terraform_streams_output(self):
        with mock.patch.object(install_mod.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["terraform"], 0)
            install_mod.run_terraform(["terraform", "version"], cwd=REPO_ROOT, check=False)
        kwargs = run.call_args.kwargs
        self.assertNotIn("capture_output", kwargs)
        self.assertNotIn("stdout", kwargs)


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

    def test_preflight_rejects_old_terraform_for_s3_native_locking(self):
        cfg = load_install_config(AWS_EXAMPLE)
        results = run_preflight(
            cloud="aws",
            config=cfg,
            which=lambda n: f"/bin/{n}",
            terraform_version=lambda: (1, 6, 1),
        )
        codes = {r.code for r in results if not r.ok}
        self.assertIn(CODE_TERRAFORM_TOO_OLD, codes)

    def test_preflight_accepts_terraform_1_10_for_s3_native_locking(self):
        cfg = load_install_config(AWS_EXAMPLE)
        results = run_preflight(
            cloud="aws",
            config=cfg,
            which=lambda n: f"/bin/{n}",
            terraform_version=lambda: (1, 10, 0),
        )
        self.assertFalse([r for r in results if not r.ok])

    def test_preflight_accepts_old_terraform_with_lock_table(self):
        cfg = load_install_config(AWS_EXAMPLE)
        cfg["terraform_state"] = {**cfg["terraform_state"], "lock_table": "terraform-locks"}
        results = run_preflight(
            cloud="aws",
            config=cfg,
            which=lambda n: f"/bin/{n}",
            terraform_version=lambda: (1, 6, 1),
        )
        self.assertFalse([r for r in results if not r.ok])

    def test_preflight_skips_version_check_when_unknown(self):
        cfg = load_install_config(AWS_EXAMPLE)
        results = run_preflight(
            cloud="aws",
            config=cfg,
            which=lambda n: f"/bin/{n}",
            terraform_version=lambda: None,
        )
        self.assertFalse([r for r in results if not r.ok])

    def _aws_cfg_with_prefix(self, prefix):
        cfg = load_install_config(AWS_EXAMPLE)
        cfg["secret_prefix"] = prefix
        return cfg

    def _preflight_codes(self, cfg, secret_exists):
        results = run_preflight(
            cloud="aws",
            config=cfg,
            which=lambda n: f"/bin/{n}",
            terraform_version=lambda: (1, 10, 0),
            secret_exists=secret_exists,
        )
        return {r.code for r in results if not r.ok}

    def test_preflight_blocks_secret_prefix_change_on_existing_install(self):
        existing = {"ts-harness/gateway-token"}
        codes = self._preflight_codes(
            self._aws_cfg_with_prefix("acme"),
            lambda secret_id, region: secret_id in existing,
        )
        self.assertIn(CODE_SECRET_PREFIX_CHANGED, codes)

    def test_preflight_allows_custom_secret_prefix_on_new_install(self):
        codes = self._preflight_codes(
            self._aws_cfg_with_prefix("acme"),
            lambda secret_id, region: False,
        )
        self.assertNotIn(CODE_SECRET_PREFIX_CHANGED, codes)

    def test_preflight_allows_prefix_once_new_secrets_exist(self):
        existing = {"ts-harness/gateway-token", "acme/gateway-token"}
        codes = self._preflight_codes(
            self._aws_cfg_with_prefix("acme"),
            lambda secret_id, region: secret_id in existing,
        )
        self.assertNotIn(CODE_SECRET_PREFIX_CHANGED, codes)

    def test_preflight_skips_secret_lookup_when_prefix_matches_project(self):
        def boom(secret_id, region):
            raise AssertionError("should not look up secrets")

        codes = self._preflight_codes(self._aws_cfg_with_prefix("ts-harness"), boom)
        self.assertEqual(codes, set())

    def test_preflight_tolerates_unknown_secret_lookup(self):
        codes = self._preflight_codes(
            self._aws_cfg_with_prefix("acme"),
            lambda secret_id, region: None,
        )
        self.assertNotIn(CODE_SECRET_PREFIX_CHANGED, codes)

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
