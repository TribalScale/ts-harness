"""A host can be rebuilt from the repo and the stored install config: config storage, branch,
stable Tailscale name and version, stale tailnet machines, and the Google OAuth callback."""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import install as install_mod
import install_config_store as store
from install_contract import load_install_config

REPO = Path(__file__).resolve().parents[1]
AWS_EXAMPLE = REPO / "config" / "install" / "aws.example.yaml"
GCP_EXAMPLE = REPO / "config" / "install" / "gcp.example.yaml"
PHASES = REPO / "scripts" / "phases"

spec = importlib.util.spec_from_file_location("tailscale_stale_device", REPO / "scripts" / "ops" / "tailscale-stale-device.py")
stale = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stale)


def done(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class StoredConfigTests(unittest.TestCase):
    def test_uri_sits_beside_the_state(self):
        aws = {"terraform_state": {"backend_type": "s3", "bucket": "b", "key": "dev/aws/terraform.tfstate"}}
        self.assertEqual(store.stored_config_uri(aws), "s3://b/dev/aws/install-config.yaml")
        gcs = {"terraform_state": {"backend_type": "gcs", "bucket": "g", "key": "prod/gcp"}}
        self.assertEqual(store.stored_config_uri(gcs), "gs://g/prod/gcp/install-config.yaml")
        flat = {"terraform_state": {"backend_type": "s3", "bucket": "b", "key": "terraform.tfstate"}}
        self.assertEqual(store.stored_config_uri(flat), "s3://b/install-config.yaml")

    def test_push_and_fetch_use_the_cloud_cli(self):
        calls = []

        def run(cmd):
            calls.append(cmd)
            if cmd[-2].startswith("s3://"):  # download
                Path(cmd[-3] if cmd[-2] == "--region" else cmd[-1]).write_text("a: 1\n")
            return done()

        config = {"terraform_state": {"backend_type": "s3", "bucket": "b", "key": "k/t", "region": "us-east-1"}}
        self.assertEqual(store.push(Path("x.yaml"), config, run=run), "s3://b/k/install-config.yaml")
        self.assertEqual(calls[0][:3], ["aws", "s3", "cp"])
        self.assertIn("--region", calls[0])

    def test_fetch_missing_object_is_none(self):
        self.assertIsNone(store.fetch("s3://b/k/install-config.yaml", run=lambda cmd: done(1, stderr="An error occurred (404) when calling the HeadObject operation: Not Found")))
        with self.assertRaises(RuntimeError):
            store.fetch("s3://b/k/install-config.yaml", run=lambda cmd: done(1, stderr="AccessDenied"))

    def test_diff(self):
        self.assertEqual(store.diff("a\n", "a\n", uri="u"), "")
        self.assertIn("nothing stored", store.diff("a\n", None, uri="u"))
        self.assertIn("+b", store.diff("a\nb\n", "a\n", uri="u"))

    def test_config_pull_writes_and_refuses_to_clobber(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dev.yaml"
            with mock.patch.object(install_mod.install_config_store, "fetch", return_value="stored: yes\n"):
                args = ["config", "pull", "--config", str(target), "--source", "s3://b/k/install-config.yaml"]
                self.assertEqual(install_mod.main(args), 0)
                self.assertEqual(target.read_text(), "stored: yes\n")
                target.write_text("local: edit\n")
                self.assertEqual(install_mod.main(args), 1)
                self.assertEqual(install_mod.main([*args, "--force"]), 0)
                self.assertEqual(target.read_text(), "stored: yes\n")

    def test_config_diff_exit_code(self):
        with mock.patch.object(install_mod.install_config_store, "fetch", return_value=AWS_EXAMPLE.read_text()):
            self.assertEqual(install_mod.main(["config", "diff", "--config", str(AWS_EXAMPLE)]), 0)
        with mock.patch.object(install_mod.install_config_store, "fetch", return_value="other: 1\n"):
            self.assertEqual(install_mod.main(["config", "diff", "--config", str(AWS_EXAMPLE)]), 1)


class RebuildSettingsTests(unittest.TestCase):
    def tfvars(self, example, cloud, vm_lines, runtime_line):
        text = example.read_text()
        text = text.replace("  admin_username:", vm_lines + "  admin_username:", 1)
        text = text.replace("  enterprise_repo_url:", runtime_line + "  enterprise_repo_url:", 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text(text)
            return install_mod.generate_tfvars(load_install_config(path), cloud=cloud)

    def test_branch_hostname_and_tags_map_to_terraform(self):
        for example, cloud in ((AWS_EXAMPLE, "aws"), (GCP_EXAMPLE, "gcp")):
            tfvars = self.tfvars(
                example,
                cloud,
                '  tailscale_hostname: ts-harness-dev\n  tailscale_tags: ["tag:ts-harness", "tag:dev"]\n',
                "  enterprise_repo_ref: dev\n",
            )
            self.assertEqual(tfvars["enterprise_repo_ref"], "dev", cloud)
            self.assertEqual(tfvars["tailscale_hostname"], "ts-harness-dev", cloud)
            self.assertEqual(tfvars["tailscale_tags"], "tag:ts-harness,tag:dev", cloud)

    def test_unset_settings_are_left_to_terraform_defaults(self):
        with tempfile.TemporaryDirectory():
            tfvars = install_mod.generate_tfvars(load_install_config(AWS_EXAMPLE), cloud="aws")
        for key in ("enterprise_repo_ref", "tailscale_hostname", "tailscale_tags"):
            self.assertNotIn(key, tfvars)

    def test_terraform_and_startup_scripts_carry_the_settings(self):
        for tf, script in (("infra/aws/ec2.tf", "scripts/user-data.sh"), ("infra/gcp/compute.tf", "scripts/gcp-startup.sh")):
            tf_text, script_text = (REPO / tf).read_text(), (REPO / script).read_text()
            for token in ("__TF_ENTERPRISE_REPO_REF__", "__TF_TAILSCALE_HOSTNAME__", "__TF_TAILSCALE_TAGS__"):
                self.assertIn(token, tf_text, tf)
                self.assertIn(token, script_text, script)
            self.assertIn('--branch "$ENTERPRISE_REPO_REF"', script_text)
            self.assertIn("var.tailscale_hostname != \"\" ? var.tailscale_hostname : var.project_name", tf_text)


class ReplacePlanTests(unittest.TestCase):
    def test_plan_passes_replace_to_terraform(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(install_mod, "run_preflight", return_value=[]), \
                mock.patch.object(install_mod, "run_terraform", return_value=done()) as run, \
                mock.patch.object(install_mod.install_config_store, "fetch", return_value=None):
            code = install_mod.main(["plan", "--cloud", "aws", "--config", str(AWS_EXAMPLE), "--state-dir", tmp,
                                     "--replace", "aws_instance.harness", "--plan-file", str(Path(tmp) / "p.tfplan")])
        self.assertEqual(code, 0)
        plan_cmd = next(call.args[0] for call in run.call_args_list if "plan" in call.args[0])
        self.assertIn("-replace=aws_instance.harness", plan_cmd)


class PhaseScriptTests(unittest.TestCase):
    def test_phase_09_checks_out_the_configured_branch(self):
        text = (PHASES / "09-clone-enterprise.sh").read_text()
        self.assertIn('checkout --quiet -B "$ENTERPRISE_REPO_REF" "origin/$ENTERPRISE_REPO_REF"', text)

    def test_phase_06_pins_and_holds_tailscale(self):
        text = (PHASES / "06-tailscale.sh").read_text()
        self.assertIn('TAILSCALE_VERSION="${TAILSCALE_VERSION:-1.104.6}"', text)
        self.assertIn('"tailscale=${TAILSCALE_VERSION}"', text)
        self.assertIn("apt-mark hold tailscale", text)
        self.assertIn('--hostname="$TAILSCALE_HOSTNAME"', text)
        self.assertIn("tailscale-stale-device.py", text)
        subprocess.run(["bash", "-n", str(PHASES / "06-tailscale.sh")], check=True)

    def resolve(self, client_id, callback, fqdn):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".setup").mkdir()
            (Path(tmp) / ".setup" / "tailscale-fqdn").write_text(fqdn + "\n")
            script = (
                f'source "{PHASES}/lib/env-defaults.sh"; source "{PHASES}/lib/common.sh"; '
                'resolve_google_oauth_callback; printf "%s" "$GOOGLE_OAUTH_CALLBACK_URL"'
            )
            env = {"PATH": "/usr/bin:/bin", "DATA_DIR": tmp, "GOOGLE_OAUTH_CLIENT_ID": client_id, "GOOGLE_OAUTH_CALLBACK_URL": callback}
            return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True).stdout

    def test_google_callback_follows_the_tailnet_name(self):
        # Placeholder and old tailnet URLs follow the current host; a custom domain is kept.
        self.assertEqual(
            self.resolve("cid", "https://enterprise-claw.tailnet-name.ts.net/oauth/google/callback", "ts-harness-dev.tail1.ts.net"),
            "https://ts-harness-dev.tail1.ts.net/oauth/google/callback",
        )
        self.assertEqual(self.resolve("cid", "", "h.tail1.ts.net"), "https://h.tail1.ts.net/oauth/google/callback")
        self.assertEqual(self.resolve("cid", "https://harness.acme.com/oauth/google/callback", "h.tail1.ts.net"), "https://harness.acme.com/oauth/google/callback")

    def test_google_callback_cleared_without_google_oauth(self):
        self.assertEqual(self.resolve("", "https://enterprise-claw.tailnet-name.ts.net/oauth/google/callback", "h.tail1.ts.net"), "")


class NewHostMarkerTests(unittest.TestCase):
    def run_reset(self, data_dir, host_id):
        script = f'source "{PHASES}/lib/env-defaults.sh"; source "{PHASES}/lib/common.sh"; reset_markers_for_new_host "{data_dir}" "{host_id}"'
        return subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin", "DATA_DIR": str(data_dir)}, capture_output=True, text=True, check=True).stdout

    def markers(self, data_dir):
        return sorted(p.name for p in (Path(data_dir) / ".setup").glob("phase-*.done"))

    def seed(self, data_dir, phases):
        setup = Path(data_dir) / ".setup"
        setup.mkdir(parents=True, exist_ok=True)
        for phase in phases:
            (setup / f"phase-{phase}.done").write_text("x\n")
        (Path(data_dir) / ".setup-complete").write_text("x\n")

    def test_replaced_instance_reruns_host_level_phases_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.seed(tmp, ["01-ebs", "02-admin-user", "05-docker", "06-tailscale", "10-fetch-secrets", "11h-coding-tools-install", "13-memory-db-init", "15-automation-cron"])
            (Path(tmp) / ".setup" / "host-id").write_text("old-machine\n")
            out = self.run_reset(tmp, "new-machine")
            self.assertIn("re-running host-level phases", out)
            self.assertEqual(self.markers(tmp), ["phase-01-ebs.done", "phase-10-fetch-secrets.done", "phase-11h-coding-tools-install.done", "phase-13-memory-db-init.done"])
            self.assertFalse((Path(tmp) / ".setup-complete").exists())
            self.assertEqual((Path(tmp) / ".setup" / "host-id").read_text().strip(), "new-machine")

    def test_same_instance_keeps_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.seed(tmp, ["02-admin-user", "06-tailscale"])
            (Path(tmp) / ".setup" / "host-id").write_text("m1\n")
            self.assertEqual(self.run_reset(tmp, "m1"), "")
            self.assertEqual(len(self.markers(tmp)), 2)

    def test_disk_from_a_host_without_host_id_counts_as_another_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.seed(tmp, ["02-admin-user", "13-memory-db-init"])
            self.run_reset(tmp, "m2")
            self.assertEqual(self.markers(tmp), ["phase-13-memory-db-init.done"])

    def test_fresh_disk_records_the_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_reset(tmp, "m3"), "")
            self.assertEqual((Path(tmp) / ".setup" / "host-id").read_text().strip(), "m3")

    def test_runner_checks_right_after_the_data_disk_mounts(self):
        text = (REPO / "scripts" / "bootstrap" / "run.sh").read_text()
        self.assertIn('if [[ "$phase_script" == "01-ebs.sh" ]]', text)
        self.assertIn("reset_markers_for_new_host", text)


class StaleTailnetDeviceTests(unittest.TestCase):
    NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

    def test_client_id_from_secret(self):
        self.assertEqual(stale.client_id_from_secret("tskey-client-k123CNTRL-abcdef"), "k123CNTRL")
        self.assertEqual(stale.client_id_from_secret("tskey-auth-xyz"), "")

    def test_only_offline_machines_with_the_exact_name(self):
        devices = [
            {"id": "1", "name": "ts-harness-dev.tail1.ts.net", "lastSeen": "2026-11-15T10:00:00Z"},
            {"id": "2", "name": "ts-harness-dev.tail1.ts.net", "connectedToControl": True, "lastSeen": "2026-11-15T10:00:00Z"},
            {"id": "3", "name": "ts-harness-dev-1.tail1.ts.net", "lastSeen": "2026-11-15T10:00:00Z"},
            {"id": "4", "name": "ts-harness-dev.tail1.ts.net", "lastSeen": "2026-11-16T11:58:00Z"},
            {"id": "5", "name": "laptop.tail1.ts.net", "lastSeen": "2026-11-01T10:00:00Z"},
        ]
        found = stale.stale_devices(devices, "ts-harness-dev", now=self.NOW)
        self.assertEqual([d["id"] for d in found], ["1"])

    def test_without_oauth_client_it_skips(self):
        with mock.patch.dict("os.environ", {"TS_OAUTH_CLIENT_SECRET": "tskey-auth-abc"}, clear=False):
            self.assertEqual(stale.main(["--hostname", "h"]), 0)


if __name__ == "__main__":
    unittest.main()
