"""Contract tests for persisted bootstrap Slack channel env."""

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"
PHASE_07 = REPO_ROOT / "scripts" / "phases" / "07-workspace-dirs.sh"
PHASE_12 = REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh"
PHASE_15 = REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh"


class BootstrapEnvContractTests(unittest.TestCase):
    def test_env_defaults_file_exists(self):
        path = REPO_ROOT / "scripts" / "phases" / "lib" / "env-defaults.sh"
        self.assertTrue(path.is_file())
        text = path.read_text()
        self.assertIn("DATA_DIR:=/vol/ts-harness-data", text)
        self.assertIn("TS_HARNESS_WORKSPACE_DIR:=", text)

    def test_phase_12_sources_env_defaults_before_data_dir(self):
        phase = PHASE_12.read_text()
        defaults_idx = phase.index("lib/env-defaults.sh")
        data_dir_idx = phase.index('PHASE_DONE="$DATA_DIR')
        self.assertLess(defaults_idx, data_dir_idx)

    def test_common_persists_and_hydrates_slack_channels(self):
        common = COMMON.read_text()

        for symbol in (
            "instance_env_path",
            "write_instance_env",
            "load_instance_env",
            "hydrate_slack_channel_env",
            "require_slack_channel_env",
            "recover_slack_channels_from_runtime_config",
            "SLACK_CHANNEL_KEYS",
        ):
            self.assertIn(symbol, common)
        self.assertIn("hydrate_slack_channel_env", common)
        self.assertIn("_read_nonempty_export", common)

    def test_phase_07_writes_instance_env(self):
        phase = PHASE_07.read_text()
        self.assertIn("write_instance_env", phase)

    def test_phase_12_hydrates_before_compose_env_write(self):
        phase = PHASE_12.read_text()
        hydrate_idx = phase.index("hydrate_slack_channel_env")
        compose_up_idx = phase.index("run_docker_compose_as_admin up -d")
        compose_write_idx = phase.index('} > "$COMPOSE_ENV"')
        require_idx = phase.index('require_slack_channel_env "$PHASE_NAME"')
        self.assertLess(hydrate_idx, compose_up_idx)
        self.assertLess(compose_up_idx, require_idx)
        self.assertLess(require_idx, compose_write_idx)
        self.assertIn("write_instance_env", phase)
        self.assertIn("TS_HARNESS_TOOLS_ROOT_HOST", phase)
        self.assertIn(
            'echo "export TS_HARNESS_TOOLS_ROOT_HOST=',
            phase,
        )

    def test_phase_15_uses_shared_slack_requirement(self):
        phase = PHASE_15.read_text()
        self.assertIn("require_slack_channel_env", phase)
        self.assertNotIn("require_channel()", phase)


if __name__ == "__main__":
    unittest.main()
