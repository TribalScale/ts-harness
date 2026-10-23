# IAM Requirement: The terraform IAM user must have sns:TagResource permission
# (in addition to sns:CreateTopic) because default_tags in main.tf triggers tagging.
#
# SNS topic for all operational alerts (budget, health, etc.)
resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts"

  tags = {
    Name = "${var.project_name}-alerts"
  }
}

resource "aws_sns_topic_subscription" "alert_email" {
  count     = var.budget_alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.budget_alert_email
}

# CloudWatch alarm: gateway health check (custom metric published by gateway-health-check.sh cron)
resource "aws_cloudwatch_metric_alarm" "gateway_unhealthy" {
  alarm_name          = "${var.project_name}-gateway-unhealthy"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "GatewayHealthy"
  namespace           = "TSHarness"
  period              = 120
  statistic           = "Minimum"
  threshold           = 1
  alarm_description   = "TS Harness gateway health check failed for 3 consecutive checks (6 minutes)"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "breaching"

  dimensions = {
    InstanceId = aws_instance.harness.id
  }

  tags = {
    Name = "${var.project_name}-gateway-unhealthy"
  }
}

resource "aws_cloudwatch_metric_alarm" "workflow_runner_unhealthy" {
  alarm_name          = "${var.project_name}-workflow-runner-unhealthy"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "WorkflowRunnerHealthy"
  namespace           = "TSHarness"
  period              = 120
  statistic           = "Minimum"
  threshold           = 1
  alarm_description   = "Workflow runner heartbeat missing for 3 consecutive checks (6 minutes)"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "breaching"

  dimensions = {
    InstanceId = aws_instance.harness.id
  }

  tags = {
    Name = "${var.project_name}-workflow-runner-unhealthy"
  }
}

resource "aws_cloudwatch_metric_alarm" "system_jobs_stale" {
  alarm_name          = "${var.project_name}-system-jobs-stale"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "SystemJobsStale"
  namespace           = "TSHarness"
  period              = 120
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Scheduled jobs (e.g. poll-github-merges, escalate-stale-waits) missed 2+ intervals for 3 consecutive checks; see /healthz scheduler_jobs"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "breaching"

  dimensions = {
    InstanceId = aws_instance.harness.id
  }

  tags = {
    Name = "${var.project_name}-system-jobs-stale"
  }
}

resource "aws_cloudwatch_metric_alarm" "workflow_tasks_pending" {
  alarm_name          = "${var.project_name}-workflow-tasks-pending"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 15
  metric_name         = "WorkflowTasksPending"
  namespace           = "TSHarness"
  period              = 120
  statistic           = "Maximum"
  threshold           = 50
  alarm_description   = "Workflow tasks pending for 30 minutes (possible stuck runner)"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.harness.id
  }

  tags = {
    Name = "${var.project_name}-workflow-tasks-pending"
  }
}

# CloudWatch alarm: auto-recover EC2 on system status check failure (e.g. host issue).
resource "aws_cloudwatch_metric_alarm" "ec2_recovery" {
  alarm_name          = "${var.project_name}-ec2-recovery"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "StatusCheckFailed_System"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Auto-recover TS Harness EC2 instance on system status check failure"
  alarm_actions       = ["arn:aws:automate:${var.aws_region}:ec2:recover"]
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.harness.id
  }

  tags = {
    Name = "${var.project_name}-ec2-recovery"
  }
}
