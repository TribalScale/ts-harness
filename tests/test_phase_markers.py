import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"
FETCH_SECRETS = REPO_ROOT / "scripts" / "phases" / "10-fetch-secrets.sh"
RENDER_WORKSPACE = REPO_ROOT / "scripts" / "phases" / "10b-render-workspace.sh"
AGENTS_INIT = REPO_ROOT / "scripts" / "phases" / "11a-agents-init.sh"
COMPOSE_UP = REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh"
CHANNELS_INIT = REPO_ROOT / "scripts" / "phases" / "14-channels-init.sh"
AUTOMATION_CRON = REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh"
RELEASE_GATES = REPO_ROOT / "scripts" / "phases" / "16-release-gates.sh"
USER_DATA = REPO_ROOT / "scripts" / "user-data.sh"
FINALIZE = REPO_ROOT / "scripts" / "phases" / "99-finalize.sh"


class PhaseMarkerTests(unittest.TestCase):
    def test_common_has_content_aware_marker_helpers(self):
        common = COMMON.read_text()

        for function_name in (
            "enterprise_assets_commit",
            "calculate_phase_fingerprint",
            "phase_marker_matches",
            "write_phase_marker",
        ):
            self.assertIn(f"{function_name}()", common)
        self.assertIn("phase_fingerprint=", common)
        self.assertIn("enterprise_assets_commit=", common)

    def test_fetch_secrets_does_not_skip_on_version_marker(self):
        phase = FETCH_SECRETS.read_text()

        self.assertNotIn("Already complete for $AGENT_RUNTIME_VERSION. Skipping.", phase)
        self.assertNotIn("INSTALLED_VERSION", phase)

    def test_content_sensitive_phases_use_fingerprints(self):
        for path in (
            RENDER_WORKSPACE,
            AGENTS_INIT,
            COMPOSE_UP,
            CHANNELS_INIT,
            AUTOMATION_CRON,
            RELEASE_GATES,
        ):
            phase = path.read_text()
            with self.subTest(path=path.name):
                self.assertIn("PHASE_FINGERPRINT=", phase)
                self.assertIn("calculate_phase_fingerprint", phase)
                self.assertIn("phase_marker_matches", phase)
                self.assertIn("write_phase_marker", phase)

    def test_standalone_fingerprint_phases_source_common_helpers(self):
        for path in (RENDER_WORKSPACE, AGENTS_INIT, COMPOSE_UP):
            phase = path.read_text()
            with self.subTest(path=path.name):
                self.assertIn('source "${SCRIPT_DIR_LIB}/lib/common.sh"', phase)

    def test_completion_marker_records_enterprise_commit(self):
        finalize = FINALIZE.read_text()

        self.assertNotIn("Already complete for $AGENT_RUNTIME_VERSION. Skipping.", finalize)
        self.assertIn("enterprise_assets_commit=", finalize)
        self.assertIn("ENTERPRISE_ASSETS_COMMIT_CURRENT", finalize)

    def test_user_data_completion_skip_checks_enterprise_commit(self):
        user_data = USER_DATA.read_text()

        self.assertIn("INSTALLED_ENTERPRISE_ASSETS_COMMIT", user_data)
        self.assertIn("CURRENT_ENTERPRISE_ASSETS_COMMIT", user_data)
        self.assertIn(
            '"$INSTALLED_ENTERPRISE_ASSETS_COMMIT" == "$CURRENT_ENTERPRISE_ASSETS_COMMIT"',
            user_data,
        )

    def test_automation_cron_writes_marker_after_runtime_config_restart(self):
        phase = AUTOMATION_CRON.read_text()

        self.assertIn("runtime-cron-jobs.json", phase)
        self.assertIn("recreate_ts_harness_services agent-runtime memory-api", phase)
        self.assertLess(phase.index("recreate_ts_harness_services"), phase.index("write_phase_marker"))

    def test_release_gates_phase_runs_live_gates_before_marker(self):
        phase = RELEASE_GATES.read_text()

        self.assertIn("python /app/scripts/release-gates.py", phase)
        self.assertNotIn("RELEASE_GATES_LOCAL_ONLY", phase)
        self.assertLess(phase.index("release-gates.py"), phase.index("write_phase_marker"))


if __name__ == "__main__":
    unittest.main()
