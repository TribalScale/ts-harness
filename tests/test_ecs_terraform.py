"""Scale-out AWS infrastructure: VPC mode, ECS coding workers, egress proxy, internal memory API."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import install as install_mod
from install_contract import InstallContractError, validate_install_config

REPO_ROOT = Path(__file__).resolve().parents[1]
AWS_ROOT = REPO_ROOT / "infra" / "aws"
AWS_EXAMPLE = REPO_ROOT / "config" / "install" / "aws.example.yaml"
GCP_EXAMPLE = REPO_ROOT / "config" / "install" / "gcp.example.yaml"
PROVIDER_CACHE = AWS_ROOT / ".terraform" / "providers"


def _tf(name: str) -> str:
    return (AWS_ROOT / name).read_text(encoding="utf-8")


def _resource_block(text: str, kind: str, name: str) -> str:
    match = re.search(rf'resource "{kind}" "{name}" \{{(.*?)\n\}}', text, re.S)
    if match is None:
        raise AssertionError(f"resource {kind}.{name} not found")
    return match.group(1)


class WorkerIsolationTests(unittest.TestCase):
    def test_worker_task_has_no_public_ip_or_ingress(self):
        ecs = _tf("ecs.tf")
        self.assertNotIn("assign_public_ip = true", ecs)
        self.assertNotRegex(ecs, r'aws_vpc_security_group_ingress_rule" "coding_worker')

    def test_worker_egress_is_limited_to_endpoints_proxy_and_memory_api(self):
        ecs = _tf("ecs.tf")
        egress_rules = re.findall(r'resource "aws_vpc_security_group_egress_rule" "(coding_worker_\w+)"', ecs)
        self.assertEqual(
            sorted(egress_rules),
            [
                "coding_worker_to_endpoints",
                "coding_worker_to_memory_api",
                "coding_worker_to_proxy",
                "coding_worker_to_s3",
            ],
        )
        for rule in egress_rules:
            self.assertNotIn("0.0.0.0/0", _resource_block(ecs, "aws_vpc_security_group_egress_rule", rule))

    def test_task_role_only_writes_logs(self):
        ecs = _tf("ecs.tf")
        policy = re.search(r'data "aws_iam_policy_document" "coding_worker_task_logs" \{(.*?)\n\}', ecs, re.S)
        self.assertIsNotNone(policy)
        actions = re.findall(r'"([a-z0-9]+:[A-Za-z*]+)"', policy.group(1))
        self.assertEqual(sorted(actions), ["logs:CreateLogStream", "logs:PutLogEvents"])

    def test_execution_role_reads_only_worker_secrets(self):
        ecs = _tf("ecs.tf")
        self.assertIn('"coding-backend-api-key"', ecs)
        self.assertIn('"github-token"', ecs)
        self.assertNotIn('"memory-api-token"', ecs)
        self.assertNotIn('"gateway-token"', ecs)

    def test_job_identity_is_not_baked_into_the_task_definition(self):
        ecs = _tf("ecs.tf")
        self.assertNotIn('name = "JOB_TOKEN"', ecs)
        self.assertNotIn('name = "JOB_ID"', ecs)


class MemoryApiExposureTests(unittest.TestCase):
    def test_load_balancer_is_internal_and_forwards_worker_routes_only(self):
        lb = _tf("memory_api_lb.tf")
        self.assertIn("internal           = true", lb)
        self.assertIn('type = "fixed-response"', lb)
        self.assertIn('values = ["/health", "/coding-jobs/*"]', lb)

    def test_host_admits_memory_api_from_load_balancer_only(self):
        lb = _tf("memory_api_lb.tf")
        rule = _resource_block(lb, "aws_vpc_security_group_ingress_rule", "host_memory_api_from_lb")
        self.assertIn("aws_security_group.memory_api_lb[0].id", rule)
        self.assertNotIn("cidr_ipv4", rule)

    def test_memory_api_bind_defaults_to_loopback(self):
        gateway = (REPO_ROOT / "scripts" / "memory-gateway.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("MEMORY_API_BIND", "").strip() or "127.0.0.1"', gateway)
        scale_out = yaml.safe_load((REPO_ROOT / "docker" / "docker-compose.scale-out.yml").read_text())
        self.assertEqual(scale_out["services"]["memory-api"]["environment"]["MEMORY_API_BIND"], "0.0.0.0")
        self.assertIn("${MEMORY_API_PUBLISH_PORT:-18792}:18792", scale_out["services"]["agent-runtime"]["ports"])
        base = yaml.safe_load((REPO_ROOT / "docker" / "docker-compose.memory-api.yml").read_text())
        self.assertNotIn("MEMORY_API_BIND", base["services"]["memory-api"]["environment"])


class LauncherPermissionTests(unittest.TestCase):
    def test_host_role_runs_only_the_worker_family_in_the_coding_cluster(self):
        iam = _tf("iam.tf")
        block = re.search(r'data "aws_iam_policy_document" "coding_launcher" \{(.*?)\n\}', iam, re.S).group(1)
        self.assertIn("task-definition/${local.coding_worker_family}:*", block)
        self.assertIn('variable = "ecs:cluster"', block)
        self.assertIn('values   = ["ecs-tasks.amazonaws.com"]', block)
        for action in ("ecs:RunTask", "ecs:StopTask", "ecs:DescribeTasks", "iam:PassRole"):
            self.assertIn(f'"{action}"', block)

    def test_runtime_config_publishes_ecs_identifiers_to_the_host(self):
        runtime_config = _tf("runtime_config.tf")
        common = (REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh").read_text(encoding="utf-8")
        for key in (
            "CODING_ECS_CLUSTER",
            "CODING_ECS_TASK_DEFINITION",
            "CODING_ECS_CONTAINER",
            "CODING_ECS_SUBNETS",
            "CODING_ECS_SECURITY_GROUPS",
            "CODING_EGRESS_PROXY_SERVICE",
            "CODING_EGRESS_DOMAINS_PARAMETER",
        ):
            self.assertIn(key, runtime_config)
            self.assertRegex(common, rf"\n    {key}\n")


class CodingJobSecretTests(unittest.TestCase):
    def test_secret_is_generated_and_reaches_memory_api(self):
        self.assertIn('"coding-job-secret"            = random_password.coding_job_secret.result', _tf("secrets.tf"))
        fetch = (REPO_ROOT / "scripts" / "phases" / "10-fetch-secrets.sh").read_text(encoding="utf-8")
        self.assertIn('fetch_secret "coding-job-secret" true', fetch)
        self.assertIn('write_env_var CODING_JOB_SECRET "$CODING_JOB_SECRET"', fetch)
        common = (REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh").read_text(encoding="utf-8")
        self.assertIn('CODING_JOB_SECRET="${CODING_JOB_SECRET:-}"', common)
        compose_up = (REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh").read_text(encoding="utf-8")
        self.assertIn("export CODING_JOB_SECRET=", compose_up)


class InstallerScaleOutTests(unittest.TestCase):
    def _aws(self) -> dict:
        return yaml.safe_load(AWS_EXAMPLE.read_text())

    def test_single_host_keeps_default_network(self):
        tfvars = install_mod.generate_tfvars(self._aws(), cloud="aws")
        self.assertEqual(tfvars["network_mode"], "default")
        self.assertNotIn("coding_worker_image_tag", tfvars)

    def test_scale_out_defaults_to_vpc_and_maps_worker_settings(self):
        cfg = self._aws()
        cfg["deployment_profile"] = "scale_out"
        cfg["scale_out"] = {
            "coding_worker_image_tag": "abc123",
            "egress_allowed_domains": ["npm.example.com"],
            "memory_api_certificate_arn": "arn:aws:acm:us-east-1:123456789012:certificate/x",
        }
        validate_install_config(cfg)
        tfvars = install_mod.generate_tfvars(cfg, cloud="aws")
        self.assertEqual(tfvars["network_mode"], "vpc")
        self.assertEqual(tfvars["coding_worker_image_tag"], "abc123")
        self.assertEqual(tfvars["egress_allowed_domains"], ["npm.example.com"])

    def test_ci_image_tag_overrides_config(self):
        cfg = self._aws()
        cfg["deployment_profile"] = "scale_out"
        cfg["scale_out"] = {"coding_worker_image_tag": "abc123"}
        with mock.patch.dict(os.environ, {"TS_HARNESS_CODING_WORKER_IMAGE_TAG": "deadbeef"}):
            tfvars = install_mod.generate_tfvars(cfg, cloud="aws")
        self.assertEqual(tfvars["coding_worker_image_tag"], "deadbeef")

    def test_scale_out_rejects_default_network(self):
        cfg = self._aws()
        cfg["deployment_profile"] = "scale_out"
        cfg["scale_out"] = {"network_mode": "default"}
        with self.assertRaises(InstallContractError) as ctx:
            validate_install_config(cfg)
        self.assertIn("network_mode=vpc", str(ctx.exception))

    def test_gcp_rejects_scale_out_settings(self):
        cfg = yaml.safe_load(GCP_EXAMPLE.read_text())
        cfg["scale_out"] = {"network_mode": "vpc"}
        with self.assertRaises(InstallContractError):
            validate_install_config(cfg)


class CiWorkflowTests(unittest.TestCase):
    def test_build_workflow_pushes_sha_tag_and_deploy_accepts_it(self):
        build = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "build-coding-worker.yml").read_text())
        steps = "\n".join(str(step.get("run", "")) for step in build["jobs"]["build"]["steps"])
        self.assertIn("docker/Dockerfile.coding-worker", steps)
        self.assertIn('image="$REGISTRY/$REPOSITORY:$GITHUB_SHA"', steps)
        deploy = (REPO_ROOT / ".github" / "workflows" / "deploy-aws.yml").read_text()
        self.assertIn("TS_HARNESS_CODING_WORKER_IMAGE_TAG: ${{ inputs.coding_worker_image_tag }}", deploy)

    def test_ci_runs_terraform_test(self):
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("terraform test", ci)


@unittest.skipUnless(
    shutil.which("terraform") and PROVIDER_CACHE.is_dir(),
    "terraform and a local provider cache are needed for the offline plan tests",
)
class TerraformPlanTests(unittest.TestCase):
    def test_network_mode_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            shutil.copy2(REPO_ROOT / "scripts" / "user-data.sh", root / "scripts" / "user-data.sh")
            shutil.copytree(
                AWS_ROOT,
                root / "infra" / "aws",
                ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "backend.tf", ".terraform.lock.hcl"),
            )
            workdir = root / "infra" / "aws"
            env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_CLI_ARGS": "-no-color"}
            init = subprocess.run(
                ["terraform", "init", "-backend=false", "-input=false", f"-plugin-dir={PROVIDER_CACHE}"],
                cwd=workdir, env=env, capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
            result = subprocess.run(
                ["terraform", "test"], cwd=workdir, env=env, capture_output=True, text=True, timeout=600,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("6 passed, 0 failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
