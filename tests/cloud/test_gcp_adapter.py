"""GCP cloud adapter behavior with mocked gcloud / metadata."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = REPO_ROOT / "scripts" / "cloud" / "adapter.sh"
GCP_ADAPTER = REPO_ROOT / "scripts" / "cloud" / "gcp" / "adapter.sh"
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"


def _write_fake_gcloud(bin_dir: Path, behavior: str) -> Path:
    path = bin_dir / "gcloud"
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            BEHAVIOR={behavior!r}
            log="{bin_dir / 'gcloud-calls.log'}"
            printf '%s\\n' "$*" >> "$log"
            if [[ "$1" == "auth" && "$2" == "print-access-token" ]]; then
              echo "fake-access-token"
              exit 0
            fi
            if [[ "$1" == "secrets" && "$2" == "versions" && "$3" == "access" ]]; then
              secret=""
              prev=""
              for arg in "$@"; do
                if [[ "$prev" == "--secret" ]]; then secret="$arg"; fi
                prev="$arg"
              done
              case "$BEHAVIOR" in
                secrets_ok)
                  if [[ "$secret" == *missing* ]]; then
                    echo "ERROR: (gcloud.secrets.versions.access) NOT_FOUND: Secret [missing] not found" >&2
                    exit 1
                  fi
                  if [[ "$secret" == *runtime-config* ]]; then
                    echo '{{"IDENTITY_PROVIDER":"oidc","SLACK_DEV_CHANNEL":"C0DEV"}}'
                    exit 0
                  fi
                  if [[ "$secret" == *managed-db* || "$secret" == projects/*/secrets/* ]]; then
                    echo '{{"username":"ts_harness","password":"s3cret"}}'
                    exit 0
                  fi
                  echo "super-secret-value"
                  exit 0
                  ;;
                secrets_missing)
                  echo "ERROR: NOT_FOUND: Secret not found" >&2
                  exit 1
                  ;;
                secrets_error)
                  echo "ERROR: PERMISSION_DENIED" >&2
                  exit 1
                  ;;
              esac
            fi
            echo "unexpected gcloud invocation: $*" >&2
            exit 2
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _write_fake_curl(bin_dir: Path, *, instance_id: str = "1234567890123456789") -> Path:
    path = bin_dir / "curl"
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            log="{bin_dir / 'curl-calls.log'}"
            printf '%s\\n' "$*" >> "$log"
            url=""
            for arg in "$@"; do
              if [[ "$arg" == http* ]]; then url="$arg"; fi
            done
            case "$url" in
              *computeMetadata/v1/instance/id)
                echo "{instance_id}"
                exit 0
                ;;
              *computeMetadata/v1/instance/zone)
                echo "projects/123/zones/us-central1-a"
                exit 0
                ;;
              *computeMetadata/v1/project/project-id)
                echo "example-gcp-project"
                exit 0
                ;;
              *monitoring.googleapis.com*)
                exit 0
                ;;
              *)
                echo "unexpected curl url: $url" >&2
                exit 2
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class GcpAdapterTests(unittest.TestCase):
    def test_gcp_adapter_file_exists(self):
        self.assertTrue(GCP_ADAPTER.is_file())

    def test_dispatcher_loads_gcp_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_gcloud(bin_dir, "secrets_ok")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "gcp",
                "CLOUD_REGION": "us-central1",
                "GCP_PROJECT_ID": "example-gcp-project",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "GCLOUD_BIN": str(bin_dir / "gcloud"),
            }
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source "{ADAPTER}"
                for op in cloud_validate_environment cloud_fetch_secret \\
                  cloud_fetch_managed_db_credentials cloud_fetch_runtime_config \\
                  cloud_resolve_data_device \\
                  cloud_get_instance_resource_id cloud_publish_runtime_metrics; do
                  declare -F "$op" >/dev/null
                done
                cloud_validate_environment
                echo OK
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_fetch_secret_prefix_dash_and_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_gcloud(bin_dir, "secrets_ok")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "gcp",
                "CLOUD_REGION": "us-central1",
                "GCP_PROJECT_ID": "example-gcp-project",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "GCLOUD_BIN": str(bin_dir / "gcloud"),
            }
            err_file = Path(tmp) / "err.txt"
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source "{ADAPTER}"
                val="$(cloud_fetch_secret anthropic-api-key required)"
                [[ "$val" == "super-secret-value" ]]
                miss="$(cloud_fetch_secret missing optional 2>/dev/null || true)"
                [[ -z "${{miss//[[:space:]]/}}" ]]
                set +e
                cloud_fetch_secret missing required >/dev/null 2>"{err_file}"
                ec=$?
                set -e
                [[ "$ec" -ne 0 ]]
                grep -qi 'required secret not found' "{err_file}"
                # Secret id uses prefix-key (slash converted).
                grep -q -- 'ts-harness-anthropic-api-key' "{bin_dir / 'gcloud-calls.log'}"
                echo OK
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("OK", result.stdout)

    def test_fetch_runtime_config_from_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_gcloud(bin_dir, "secrets_ok")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "gcp",
                "CLOUD_REGION": "us-central1",
                "GCP_PROJECT_ID": "example-gcp-project",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "GCLOUD_BIN": str(bin_dir / "gcloud"),
            }
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source "{ADAPTER}"
                json="$(cloud_fetch_runtime_config)"
                echo "$json" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["IDENTITY_PROVIDER"]=="oidc"'
                echo OK
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("OK", result.stdout)
            calls = (bin_dir / "gcloud-calls.log").read_text()
            self.assertIn("ts-harness-runtime-config", calls)

    def test_fetch_managed_db_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_gcloud(bin_dir, "secrets_ok")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "gcp",
                "CLOUD_REGION": "us-central1",
                "GCP_PROJECT_ID": "example-gcp-project",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "GCLOUD_BIN": str(bin_dir / "gcloud"),
            }
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source "{ADAPTER}"
                json="$(cloud_fetch_managed_db_credentials projects/example-gcp-project/secrets/ts-harness-managed-db-credentials)"
                echo "$json" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["password"]=="s3cret"'
                echo OK
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_publish_runtime_metrics_posts_custom_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_gcloud(bin_dir, "secrets_ok")
            _write_fake_curl(bin_dir)
            metrics = Path(tmp) / "metrics.json"
            metrics.write_text(
                json.dumps(
                    {
                        "namespace": "TSHarness",
                        "dimensions": {"InstanceId": "1234567890123456789"},
                        "metrics": [
                            {"name": "GatewayHealthy", "value": 1, "unit": "None"},
                            {"name": "WorkflowRunnerHealthy", "value": 1, "unit": "None"},
                            {"name": "WorkflowTasksPending", "value": 0, "unit": "None"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "gcp",
                "CLOUD_REGION": "us-central1",
                "GCP_PROJECT_ID": "example-gcp-project",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "GCLOUD_BIN": str(bin_dir / "gcloud"),
            }
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source "{ADAPTER}"
                cloud_publish_runtime_metrics "{metrics}"
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (bin_dir / "curl-calls.log").read_text()
            self.assertIn("monitoring.googleapis.com", calls)
            self.assertIn("TSHarness/GatewayHealthy", calls)

    def test_get_instance_resource_id_from_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_gcloud(bin_dir, "secrets_ok")
            _write_fake_curl(bin_dir, instance_id="998877665544332211")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "gcp",
                "CLOUD_REGION": "us-central1",
                "GCP_PROJECT_ID": "example-gcp-project",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "GCLOUD_BIN": str(bin_dir / "gcloud"),
                "INSTANCE_RESOURCE_ID": "",
            }
            # Clear env override if parent shell exported one.
            env.pop("INSTANCE_RESOURCE_ID", None)
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                unset INSTANCE_RESOURCE_ID || true
                source "{ADAPTER}"
                id="$(cloud_get_instance_resource_id)"
                [[ "$id" == "998877665544332211" ]]
                echo OK
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_common_fetch_secret_wrapper_uses_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_gcloud(bin_dir, "secrets_ok")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "gcp",
                "CLOUD_REGION": "us-central1",
                "GCP_PROJECT_ID": "example-gcp-project",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "GCLOUD_BIN": str(bin_dir / "gcloud"),
            }
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source "{COMMON}"
                ensure_cloud_adapter
                val="$(fetch_secret anthropic-api-key false)"
                [[ "$val" == "super-secret-value" ]]
                echo OK
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_resolve_data_device_matches_google_by_id(self):
        text = GCP_ADAPTER.read_text(encoding="utf-8")
        self.assertIn("/dev/disk/by-id/google-", text)
        self.assertIn("cloud_resolve_data_device", text)
        # Identity match uses disk name from projects/.../disks/<name>.
        self.assertIn('disk_name="${resource_id##*/}"', text)


if __name__ == "__main__":
    unittest.main()
