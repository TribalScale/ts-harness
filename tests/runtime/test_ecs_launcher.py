"""EcsLauncher against a stubbed ECS client, and capacity handling in the launcher tick."""

from __future__ import annotations

import unittest

try:
    import boto3
    from botocore.stub import ANY, Stubber
except ImportError:  # pragma: no cover - boto3 ships with the aws extra
    boto3 = None

from ts_harness.coding import launcher as launcher_mod
from ts_harness.coding.launcher import CapacityError, EcsConfig, EcsLauncher, LaunchError, launcher_tick

JOB = {"id": "6f1c1f9e-1111-4222-8333-444455556666", "workflow_id": "wf-1", "attempt": 2}
TASK_ARN = "arn:aws:ecs:us-east-1:123456789012:task/ts-harness-coding/abc"
CONFIG = EcsConfig(
    cluster="ts-harness-coding",
    task_definition="ts-harness-coding-worker",
    subnets=["subnet-a", "subnet-b"],
    security_groups=["sg-worker"],
    region="us-east-1",
)


@unittest.skipIf(boto3 is None, "boto3 not installed")
class EcsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client("ecs", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
        self.stub = Stubber(self.client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.sleeps: list[float] = []
        self.launcher = EcsLauncher(CONFIG, client=self.client, sleep=self.sleeps.append)

    def test_start_runs_one_private_task_with_job_overrides(self):
        expected = {
            "cluster": "ts-harness-coding",
            "taskDefinition": "ts-harness-coding-worker",
            "count": 1,
            "capacityProviderStrategy": [{"capacityProvider": "FARGATE", "weight": 1}],
            "networkConfiguration": {
                "awsvpcConfiguration": {
                    "subnets": ["subnet-a", "subnet-b"],
                    "securityGroups": ["sg-worker"],
                    "assignPublicIp": "DISABLED",
                }
            },
            "overrides": {
                "containerOverrides": [
                    {
                        "name": "coding-worker",
                        "environment": [
                            {"name": "JOB_ID", "value": JOB["id"]},
                            {"name": "JOB_TOKEN", "value": "cj1.token"},
                        ],
                    }
                ]
            },
            "startedBy": ANY,
            "clientToken": f"{JOB['id']}-2",
            "enableECSManagedTags": True,
            "tags": [
                {"key": "ts-harness:coding-job", "value": JOB["id"]},
                {"key": "ts-harness:workflow", "value": "wf-1"},
            ],
        }
        self.stub.add_response("run_task", {"tasks": [{"taskArn": TASK_ARN}], "failures": []}, expected)
        self.assertEqual(self.launcher.start(JOB, "cj1.token"), TASK_ARN)
        self.stub.assert_no_pending_responses()
        self.assertLessEqual(len(self.launcher.run_task_request(JOB, "t")["startedBy"]), 36)

    def test_throttling_backs_off_then_succeeds(self):
        self.stub.add_client_error("run_task", service_error_code="ThrottlingException", service_message="Rate exceeded")
        self.stub.add_client_error("run_task", service_error_code="ThrottlingException", service_message="Rate exceeded")
        self.stub.add_response("run_task", {"tasks": [{"taskArn": TASK_ARN}], "failures": []})
        self.assertEqual(self.launcher.start(JOB, "t"), TASK_ARN)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_persistent_throttling_is_a_capacity_error(self):
        for _ in range(4):
            self.stub.add_client_error(
                "run_task",
                service_error_code="ThrottlingException",
                service_message="You've reached the limit on the number of vCPUs you can run concurrently",
            )
        with self.assertRaises(CapacityError):
            self.launcher.start(JOB, "t")
        self.assertEqual(len(self.sleeps), 3)

    def test_capacity_failures_and_other_failures(self):
        self.stub.add_response(
            "run_task", {"tasks": [], "failures": [{"reason": "Capacity is unavailable at this time"}]}
        )
        with self.assertRaises(CapacityError):
            self.launcher.start(JOB, "t")
        self.stub.add_response(
            "run_task", {"tasks": [], "failures": [{"reason": "MISSING", "detail": "image not found"}]}
        )
        with self.assertRaisesRegex(LaunchError, "image not found") as ctx:
            self.launcher.start(JOB, "t")
        self.assertNotIsInstance(ctx.exception, CapacityError)

    def test_access_denied_is_a_launch_error_without_retry(self):
        self.stub.add_client_error("run_task", service_error_code="AccessDeniedException", service_message="not allowed")
        with self.assertRaisesRegex(LaunchError, "AccessDenied"):
            self.launcher.start(JOB, "t")
        self.assertEqual(self.sleeps, [])

    def test_describe_maps_ecs_status(self):
        self.stub.add_response("describe_tasks", {"tasks": [{"taskArn": TASK_ARN, "lastStatus": "PROVISIONING"}]},
                               {"cluster": "ts-harness-coding", "tasks": [TASK_ARN]})
        self.stub.add_response("describe_tasks", {"tasks": [{"taskArn": TASK_ARN, "lastStatus": "STOPPED"}]})
        self.stub.add_response("describe_tasks", {"tasks": [], "failures": [{"arn": TASK_ARN, "reason": "MISSING"}]})
        self.assertEqual(self.launcher.describe(TASK_ARN), "running")
        self.assertEqual(self.launcher.describe(TASK_ARN), "exited")
        self.assertEqual(self.launcher.describe(TASK_ARN), "missing")

    def test_stop_names_the_cluster_and_reason(self):
        self.stub.add_response(
            "stop_task",
            {"task": {"taskArn": TASK_ARN}},
            {"cluster": "ts-harness-coding", "task": TASK_ARN, "reason": "coding job cancelled or lost"},
        )
        self.launcher.stop(TASK_ARN)
        self.stub.assert_no_pending_responses()


class EcsConfigTests(unittest.TestCase):
    def test_from_env_requires_the_ecs_identifiers(self):
        with self.assertRaisesRegex(ValueError, "CODING_ECS_SUBNETS"):
            EcsConfig.from_env({"CODING_ECS_CLUSTER": "c", "CODING_ECS_TASK_DEFINITION": "t", "CODING_ECS_SECURITY_GROUPS": "sg"})

    def test_from_env_splits_lists(self):
        cfg = EcsConfig.from_env(
            {
                "CODING_ECS_CLUSTER": "c",
                "CODING_ECS_TASK_DEFINITION": "t",
                "CODING_ECS_SUBNETS": "subnet-a, subnet-b",
                "CODING_ECS_SECURITY_GROUPS": "sg-1",
                "CODING_ECS_CAPACITY_PROVIDER": "FARGATE_SPOT",
                "CLOUD_REGION": "us-west-2",
            }
        )
        self.assertEqual((cfg.subnets, cfg.capacity_provider, cfg.region), (["subnet-a", "subnet-b"], "FARGATE_SPOT", "us-west-2"))

    def test_build_launcher_picks_ecs(self):
        launcher = launcher_mod.build_launcher(
            {
                "CODING_LAUNCHER": "ecs",
                "CODING_ECS_CLUSTER": "c",
                "CODING_ECS_TASK_DEFINITION": "t",
                "CODING_ECS_SUBNETS": "s",
                "CODING_ECS_SECURITY_GROUPS": "g",
            }
        )
        self.assertIsInstance(launcher, EcsLauncher)


class _Memory:
    def __init__(self, jobs):
        self.jobs = jobs
        self.launches = []

    def claim_coding_jobs(self, *, launcher, max_running, quotas=None):
        jobs, self.jobs = self.jobs, []
        return {"jobs": jobs}

    def record_coding_job_launch(self, job_id, *, runner_ref="", error="", retryable=False):
        self.launches.append((job_id, runner_ref, error, retryable))

    def list_coding_jobs(self, **kwargs):
        return []


class _FullLauncher:
    name = "ecs"

    def __init__(self):
        self.calls = 0

    def start(self, job, token):
        self.calls += 1
        raise CapacityError("vCPU limit")

    def stop(self, ref):
        pass

    def describe(self, ref):
        return "missing"


class CapacityTickTests(unittest.TestCase):
    def test_capacity_error_requeues_the_batch_without_more_launch_calls(self):
        memory = _Memory([{"id": "a", "token": "t1"}, {"id": "b", "token": "t2"}])
        launcher = _FullLauncher()
        result = launcher_tick(memory, launcher, max_running=5)
        self.assertEqual(launcher.calls, 1)
        self.assertEqual(result["waiting"], ["a", "b"])
        self.assertEqual(result["failed"], [])
        self.assertTrue(all(retryable for *_, retryable in memory.launches))


if __name__ == "__main__":
    unittest.main()
