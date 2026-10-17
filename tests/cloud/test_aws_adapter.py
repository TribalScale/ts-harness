"""AWS cloud adapter behavior with mocked aws CLI / metadata."""

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
AWS_ADAPTER = REPO_ROOT / "scripts" / "cloud" / "aws" / "adapter.sh"
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"


def _write_fake_aws(bin_dir: Path, behavior: str) -> Path:
    """Create a fake aws CLI script implementing Secrets Manager / CloudWatch."""
    path = bin_dir / "aws"
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            BEHAVIOR={behavior!r}
            log="{bin_dir / 'aws-calls.log'}"
            printf '%s\\n' "$*" >> "$log"
            case "$1" in
              ssm)
                name=""
                prev=""
                for arg in "$@"; do
                  if [[ "$prev" == "--name" ]]; then name="$arg"; fi
                  prev="$arg"
                done
                case "$BEHAVIOR" in
                  secrets_ok)
                    if [[ "$name" == */runtime-config ]]; then
                      echo '{{"IDENTITY_PROVIDER":"oidc","SLACK_DEV_CHANNEL":"C0DEV"}}'
                      exit 0
                    fi
                    echo "An error occurred (ParameterNotFound) when calling" >&2
                    exit 254
                    ;;
                  secrets_missing)
                    echo "An error occurred (ParameterNotFound) when calling" >&2
                    exit 254
                    ;;
                esac
                ;;
              secretsmanager)
                # args: secretsmanager get-secret-value --region R --secret-id ID --query ... --output text
                secret_id=""
                prev=""
                for arg in "$@"; do
                  if [[ "$prev" == "--secret-id" ]]; then secret_id="$arg"; fi
                  prev="$arg"
                done
                case "$BEHAVIOR" in
                  secrets_ok)
                    if [[ "$secret_id" == */missing ]]; then
                      echo "An error occurred (ResourceNotFoundException) when calling" >&2
                      exit 254
                    fi
                    if [[ "$secret_id" == arn:* || "$secret_id" == */rds* ]]; then
                      echo '{{"username":"ts_harness","password":"s3cret"}}'
                      exit 0
                    fi
                    echo "super-secret-value"
                    exit 0
                    ;;
                  secrets_missing)
                    echo "An error occurred (ResourceNotFoundException) when calling" >&2
                    exit 254
                    ;;
                  secrets_error)
                    echo "An error occurred (AccessDeniedException)" >&2
                    exit 254
                    ;;
                esac
                ;;
              cloudwatch)
                if [[ "$BEHAVIOR" == "cw_fail" ]]; then
                  echo "Throttling" >&2
                  exit 1
                fi
                exit 0
                ;;
            esac
            echo "unexpected aws invocation: $*" >&2
            exit 2
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class AwsAdapterTests(unittest.TestCase):
    def test_aws_adapter_file_exists(self):
        self.assertTrue(AWS_ADAPTER.is_file())

    def test_fetch_secret_required_and_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_aws(bin_dir, "secrets_ok")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "aws",
                "CLOUD_REGION": "us-east-1",
                "AWS_REGION": "us-east-1",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "AWS_BIN": str(bin_dir / "aws"),
            }
            out_file = Path(tmp) / "out.txt"
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
                cloud_fetch_secret missing required >"{out_file}" 2>"{err_file}"
                ec=$?
                set -e
                [[ "$ec" -ne 0 ]]
                grep -qi 'required secret not found' "{err_file}"
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

    def test_fetch_managed_db_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_aws(bin_dir, "secrets_ok")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "aws",
                "CLOUD_REGION": "us-east-1",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "AWS_BIN": str(bin_dir / "aws"),
            }
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source "{ADAPTER}"
                json="$(cloud_fetch_managed_db_credentials arn:aws:secretsmanager:us-east-1:123:secret:rds!db)"
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

    def test_fetch_runtime_config_from_ssm(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_aws(bin_dir, "secrets_ok")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "aws",
                "CLOUD_REGION": "us-east-1",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "AWS_BIN": str(bin_dir / "aws"),
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
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)
            calls = (bin_dir / "aws-calls.log").read_text()
            self.assertIn("ssm get-parameter", calls)
            self.assertIn("ts-harness/runtime-config", calls)

    def test_fetch_runtime_config_fails_closed_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_aws(bin_dir, "secrets_missing")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "aws",
                "CLOUD_REGION": "us-east-1",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "AWS_BIN": str(bin_dir / "aws"),
            }
            err_file = Path(tmp) / "err.txt"
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source "{ADAPTER}"
                set +e
                cloud_fetch_runtime_config > /dev/null 2>"{err_file}"
                ec=$?
                set -e
                [[ "$ec" -ne 0 ]]
                grep -qi 'runtime-config' "{err_file}"
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

    def test_publish_runtime_metrics_invokes_cloudwatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_aws(bin_dir, "secrets_ok")
            metrics = Path(tmp) / "metrics.json"
            metrics.write_text(
                json.dumps(
                    {
                        "namespace": "TSHarness",
                        "dimensions": {"InstanceId": "i-abc"},
                        "metrics": [
                            {"name": "GatewayHealthy", "value": 1, "unit": "None"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "aws",
                "CLOUD_REGION": "us-east-1",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "AWS_BIN": str(bin_dir / "aws"),
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
            calls = (bin_dir / "aws-calls.log").read_text()
            self.assertIn("cloudwatch put-metric-data", calls)
            self.assertIn("GatewayHealthy", calls)
            self.assertIn("InstanceId=i-abc", calls)

    def test_common_fetch_secret_wrapper_uses_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _write_fake_aws(bin_dir, "secrets_ok")
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CLOUD_PROVIDER": "aws",
                "CLOUD_REGION": "us-east-1",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "AWS_BIN": str(bin_dir / "aws"),
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

    def test_get_instance_resource_id_prefers_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "CLOUD_PROVIDER": "aws",
                "CLOUD_REGION": "us-east-1",
                "SECRETS_PREFIX": "ts-harness",
                "DATA_DIR": tmp,
                "INSTANCE_RESOURCE_ID": "i-from-env",
            }
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source "{ADAPTER}"
                id="$(cloud_get_instance_resource_id)"
                [[ "$id" == "i-from-env" ]]
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


if __name__ == "__main__":
    unittest.main()
