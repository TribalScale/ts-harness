"""Shared bootstrap orchestration contract (provider-neutral phase runner)."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_RUN = REPO_ROOT / "scripts" / "bootstrap" / "run.sh"
BOOTSTRAP_PHASES = REPO_ROOT / "scripts" / "bootstrap" / "phases.sh"
USER_DATA = REPO_ROOT / "scripts" / "user-data.sh"

# Exact ordered phase graph currently executed by AWS user-data.
EXPECTED_PHASES = [
    "00-init.sh",
    "01-ebs.sh",
    "02-admin-user.sh",
    "03-ufw.sh",
    "04-ssh-harden.sh",
    "05-docker.sh",
    "06-tailscale.sh",
    "07-workspace-dirs.sh",
    "09-clone-enterprise.sh",
    "10-fetch-secrets.sh",
    "10b-render-workspace.sh",
    "11-agent-runtime-setup.sh",
    # multi-agent block (conditional)
    "11a-agents-init.sh",
    "11b-agent-auth-init.sh",
    "11c-mcporter-init.sh",
    "11g-google-workspace-init.sh",
    "11h-coding-tools-install.sh",
    "11d-validate-workspace.sh",
    "11e-preflight-embeddings.sh",
    "11f-check-embedding-dim.sh",
    "12-compose-up.sh",
    "13-memory-db-init.sh",
    "14-channels-init.sh",
    "15-automation-cron.sh",
    "16-release-gates.sh",
    "99-finalize.sh",
]


class BootstrapOrchestrationContractTests(unittest.TestCase):
    def test_bootstrap_runner_and_phase_list_exist(self):
        self.assertTrue(BOOTSTRAP_RUN.is_file(), f"missing {BOOTSTRAP_RUN}")
        self.assertTrue(BOOTSTRAP_PHASES.is_file(), f"missing {BOOTSTRAP_PHASES}")
        run_text = BOOTSTRAP_RUN.read_text()
        phases_text = BOOTSTRAP_PHASES.read_text()
        self.assertIn("bootstrap_phase_list", phases_text)
        self.assertIn("phases.sh", run_text)
        self.assertIn("phase-", run_text)
        self.assertIn("$DATA_DIR/.setup", run_text)

    def test_phase_list_preserves_order(self):
        phases_text = BOOTSTRAP_PHASES.read_text()
        positions = []
        for name in EXPECTED_PHASES:
            self.assertIn(name, phases_text, f"phase missing from phases.sh: {name}")
            positions.append(phases_text.index(name))
        self.assertEqual(positions, sorted(positions))

    def test_user_data_delegates_phase_loop_to_bootstrap_runner(self):
        user_data = USER_DATA.read_text()
        self.assertIn("bootstrap/run.sh", user_data)
        # Provider-specific setup stays in user-data.
        self.assertIn("__TF_AWS_REGION__", user_data)
        self.assertIn("awscli", user_data.lower())
        # Inline phase loop should not remain as the primary runner.
        # Allow a thin wrapper that calls the shared runner.
        self.assertNotRegex(
            user_data,
            r"(?m)^run_phase 00-init\.sh$",
        )

    def test_marker_path_contract_unchanged(self):
        # Individual phases still write markers under DATA_DIR/.setup.
        phase_12 = (REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh").read_text()
        self.assertIn('PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"', phase_12)
        run_text = BOOTSTRAP_RUN.read_text()
        self.assertRegex(run_text, r"DATA_DIR.*/\.setup")


if __name__ == "__main__":
    unittest.main()
