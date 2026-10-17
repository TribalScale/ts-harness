import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC = REPO_ROOT / "scripts" / "ops" / "sync-runtime-config.sh"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "runtime-config.md"
AWS_INSTALL = REPO_ROOT / "docs" / "runbooks" / "aws-install.md"
GCP_INSTALL = REPO_ROOT / "docs" / "runbooks" / "gcp-install.md"


class SyncRuntimeConfigTests(unittest.TestCase):
    def test_sync_script_hydrates_then_restarts_agent_only(self):
        text = SYNC.read_text()
        self.assertIn("lib/common.sh", text)
        self.assertIn("cloud_fetch_runtime_config", text)
        self.assertIn("10a-hydrate-runtime-config.sh", text)
        self.assertIn("11-agent-runtime-setup.sh", text)
        self.assertIn("restart_agent_runtime", text)
        self.assertNotIn("--force-recreate", text)
        self.assertNotIn("compose up", text)

    def test_runbook_covers_apply_sync_and_oidc_check(self):
        text = RUNBOOK.read_text()
        self.assertIn("terraform apply", text)
        self.assertIn("sync-runtime-config.sh", text)
        self.assertIn("/admin/auth/config", text)
        self.assertIn("oidc", text)
        self.assertIn("runtime-config.md", AWS_INSTALL.read_text())
        self.assertIn("runtime-config.md", GCP_INSTALL.read_text())


if __name__ == "__main__":
    unittest.main()
