"""The managed DB credential watch syncs only when the live secret differs from memory-api's password."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCH = REPO_ROOT / "scripts" / "ops" / "watch-db-credentials.sh"
PHASE = REPO_ROOT / "scripts" / "phases" / "12a-db-credential-watch.sh"


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class DbCredentialWatchTests(unittest.TestCase):
    def run_watch(self, *, backend: str = "rds", running: str | None = "old", live: str = "new") -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ops = root / "scripts" / "ops"
            ops.mkdir(parents=True)
            (root / "scripts" / "phases").symlink_to(REPO_ROOT / "scripts" / "phases")
            shutil.copy(WATCH, ops / WATCH.name)
            _executable(ops / "sync-compose-db-env.sh", "echo SYNCED\n")
            data = root / "data" / ".setup"
            data.mkdir(parents=True)
            (data / "instance-env.sh").write_text(
                f"export MEMORY_BACKEND={backend}\nexport MANAGED_DB_CREDENTIAL_REF=arn:aws:secretsmanager:x:1:secret:rds\n"
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            _executable(bin_dir / "flock", "#!/bin/sh\nexit 0\n")
            container = "" if running is None else "abc123"
            _executable(
                bin_dir / "docker",
                f'#!/bin/bash\ncase "$1" in\n  ps) [[ -z "{container}" ]] || echo {container} ;;\n'
                f'  exec) printf %s "{running or ""}" ;;\nesac\n',
            )
            adapter = (
                "CLOUD_ADAPTER_LOADED=1\n_cloud_aws_bin() { :; }\ncloud_fetch_secret() { :; }\n"
                f"cloud_fetch_managed_db_credentials() {{ printf '{{\"username\":\"u\",\"password\":\"{live}\"}}'; }}\n"
                "export -f _cloud_aws_bin cloud_fetch_secret cloud_fetch_managed_db_credentials\n"
            )
            env = {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "DATA_DIR": str(root / "data"),
                "DB_CREDENTIALS_LOCK": str(root / "watch.lock"),
                "HOME": tmp,
            }
            return subprocess.run(
                ["bash", "-c", f'{adapter}export CLOUD_ADAPTER_LOADED; bash "{ops / WATCH.name}"'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_changed_password_triggers_sync(self):
        result = self.run_watch(running="old", live="new")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("managed DB password changed", result.stdout)
        self.assertIn("SYNCED", result.stdout)
        self.assertNotIn("new", result.stdout.replace("managed DB password changed", ""))

    def test_matching_password_is_a_no_op(self):
        result = self.run_watch(running="same", live="same")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_local_backend_and_stopped_memory_api_skip(self):
        self.assertEqual(self.run_watch(backend="local").stdout, "")
        stopped = self.run_watch(running=None)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn("memory-api is not running", stopped.stdout)
        self.assertNotIn("SYNCED", stopped.stdout)

    def test_phase_registers_cron_only_for_managed_backends(self):
        phase = PHASE.read_text()
        self.assertIn('grep -v "watch-db-credentials"', phase)
        self.assertIn("*/5 * * * *", phase)
        self.assertIn("ADMIN_USER=${ADMIN_USER}", phase)
        self.assertIn("if is_managed_memory_backend; then", phase)


if __name__ == "__main__":
    unittest.main()
