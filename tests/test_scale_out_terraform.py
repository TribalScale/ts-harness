# flake8: noqa
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VARIABLES = REPO_ROOT / "infra" / "aws" / "variables.tf"
RUNTIME_CONFIG = REPO_ROOT / "infra" / "aws" / "runtime_config.tf"
COMPOSE_UP = REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh"
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"


class DeploymentProfileTerraformTests(unittest.TestCase):
    def test_variable_defaults_to_single_host_with_validation(self):
        variables = VARIABLES.read_text()
        block = variables[variables.index('variable "deployment_profile"') :]
        block = block[: block.index("\n}\n")]
        self.assertIn('default     = "single_host"', block)
        self.assertIn('contains(["single_host", "scale_out"], var.deployment_profile)', block)

    def test_profile_is_published_to_runtime_config(self):
        self.assertIn("DEPLOYMENT_PROFILE                    = var.deployment_profile", RUNTIME_CONFIG.read_text())

    def test_bootstrap_hydrates_profile_from_runtime_config(self):
        self.assertIn("    DEPLOYMENT_PROFILE\n", COMMON.read_text())

    def test_scale_out_overlay_only_for_scale_out(self):
        script = COMPOSE_UP.read_text()
        self.assertIn('if [[ "${DEPLOYMENT_PROFILE:-single_host}" == "scale_out" ]]; then', script)
        self.assertIn("docker-compose.scale-out.yml", script)


if __name__ == "__main__":
    unittest.main()
