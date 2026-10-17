import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HEALTH_SCRIPT = REPO_ROOT / "scripts" / "ops" / "gateway-health-check.sh"
AWS_ADAPTER = REPO_ROOT / "scripts" / "cloud" / "aws" / "adapter.sh"
IAM_TF = REPO_ROOT / "infra" / "aws" / "iam.tf"
MONITORING_TF = REPO_ROOT / "infra" / "aws" / "monitoring.tf"
EC2_TF = REPO_ROOT / "infra" / "aws" / "ec2.tf"


class MonitoringContractTests(unittest.TestCase):
    def test_health_check_uses_imdsv2_for_instance_id(self):
        # Ops scripts call cloud_get_instance_resource_id; IMDSv2 lives in AWS adapter.
        health = HEALTH_SCRIPT.read_text()
        aws = AWS_ADAPTER.read_text()

        self.assertIn("cloud_get_instance_resource_id", health)
        self.assertIn("latest/api/token", aws)
        self.assertIn("X-aws-ec2-metadata-token-ttl-seconds", aws)
        self.assertIn("X-aws-ec2-metadata-token:", aws)
        self.assertIn("latest/meta-data/instance-id", aws)
        self.assertNotIn("IMDSv1", aws)

    def test_health_check_publishes_expected_metric_dimension(self):
        health = HEALTH_SCRIPT.read_text()
        aws = AWS_ADAPTER.read_text()

        self.assertIn("cloud_publish_runtime_metrics", health)
        self.assertIn("TSHarness", health)
        self.assertIn("GatewayHealthy", health)
        self.assertIn("InstanceId", health)
        self.assertIn("cloudwatch", aws)
        self.assertIn("put-metric-data", aws)

    def test_health_check_logs_metric_publish_failures(self):
        health = HEALTH_SCRIPT.read_text()
        aws = AWS_ADAPTER.read_text()

        self.assertIn("CloudWatch metric publish failed", health)
        self.assertIn("CloudWatch metric publish failed", aws)
        self.assertIn("cloud_publish_runtime_metrics", health)
        # Adapter must not swallow put-metric-data failures with || true.
        publish_idx = aws.index("put-metric-data")
        window = aws[publish_idx : publish_idx + 400]
        self.assertNotIn("|| true", window)

    def test_ec2_role_can_publish_cloudwatch_metric(self):
        iam = IAM_TF.read_text()

        self.assertIn("cloudwatch:PutMetricData", iam)
        self.assertIn("TSHarness", iam)
        self.assertIn("aws_iam_role_policy", iam)

    def test_alarm_dimension_matches_health_script_dimension(self):
        monitoring = MONITORING_TF.read_text()

        self.assertIn("namespace           = \"TSHarness\"", monitoring)
        self.assertIn("metric_name         = \"GatewayHealthy\"", monitoring)
        self.assertIn("InstanceId = aws_instance.harness.id", monitoring)

    def test_workflow_runner_alarm_contract(self):
        monitoring = MONITORING_TF.read_text()

        self.assertIn("metric_name         = \"WorkflowRunnerHealthy\"", monitoring)
        self.assertIn("metric_name         = \"WorkflowTasksPending\"", monitoring)

    def test_publish_runtime_metrics_script(self):
        script = REPO_ROOT / "scripts" / "ops" / "publish-runtime-metrics.sh"
        text = script.read_text()
        aws = AWS_ADAPTER.read_text()

        self.assertIn("GatewayHealthy", text)
        self.assertIn("WorkflowRunnerHealthy", text)
        self.assertIn("WorkflowTasksPending", text)
        self.assertIn("WorkflowTasksFailed", text)
        self.assertIn("cloud_publish_runtime_metrics", text)
        self.assertIn("put-metric-data", aws)

    def test_health_cron_includes_usr_local_bin_path(self):
        phase = REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh"
        script = phase.read_text()

        self.assertIn("PATH=/usr/local/bin:/usr/bin:/bin", script)
        self.assertIn("publish-runtime-metrics", script)

    def test_imds_hop_limit_supports_container_role_credentials(self):
        ec2 = EC2_TF.read_text()

        self.assertIn('http_tokens   = "required"', ec2)
        self.assertIn("http_put_response_hop_limit = 2", ec2)
        self.assertIn('embeddings_provider = "bedrock"', ec2)


if __name__ == "__main__":
    unittest.main()
