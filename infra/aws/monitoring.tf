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

# Scale-out metrics: published by publish-runtime-metrics.sh from the memory API
# snapshot, without an InstanceId because they come from the shared database.
resource "aws_cloudwatch_metric_alarm" "coding_job_queue_age" {
  alarm_name          = "${var.project_name}-coding-job-queue-age"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CodingJobOldestQueuedSeconds"
  namespace           = "TSHarness"
  period              = 120
  statistic           = "Maximum"
  threshold           = var.coding_job_queue_alarm_seconds
  alarm_description   = "A coding job has waited for a worker longer than ${var.coding_job_queue_alarm_seconds} seconds (launcher down, ECS quota, or team quotas)"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  tags = {
    Name = "${var.project_name}-coding-job-queue-age"
  }
}

resource "aws_cloudwatch_metric_alarm" "coding_job_lost_rate" {
  alarm_name          = "${var.project_name}-coding-job-lost-rate"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = var.coding_job_lost_rate_threshold
  alarm_description   = "More than ${var.coding_job_lost_rate_threshold * 100}% of coding jobs finished in 15 minutes were lost (worker stopped heartbeating)"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "rate"
    expression  = "IF(finished >= 3, lost / finished, 0)"
    label       = "Lost job rate"
    return_data = true
  }

  metric_query {
    id = "lost"
    metric {
      metric_name = "CodingJobsLost"
      namespace   = "TSHarness"
      period      = 900
      stat        = "Sum"
    }
  }

  metric_query {
    id = "finished"
    metric {
      metric_name = "CodingJobsFinishedTotal"
      namespace   = "TSHarness"
      period      = 900
      stat        = "Sum"
    }
  }

  tags = {
    Name = "${var.project_name}-coding-job-lost-rate"
  }
}

resource "aws_cloudwatch_metric_alarm" "github_rate_limit_low" {
  alarm_name          = "${var.project_name}-github-rate-limit-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "GitHubRateLimitLowestPercent"
  namespace           = "TSHarness"
  period              = 300
  statistic           = "Minimum"
  threshold           = var.github_rate_limit_alarm_percent
  alarm_description   = "A GitHub App installation has less than ${var.github_rate_limit_alarm_percent}% of its REST rate limit left; see the GitHubRateLimitRemainingPercent metric for which one"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  tags = {
    Name = "${var.project_name}-github-rate-limit-low"
  }
}

locals {
  dashboard_search = {
    for key, query in {
      lane_ready  = ["{TSHarness,Lane} MetricName=\"LaneReadyTasks\"", "Maximum"]
      lane_held   = ["{TSHarness,Lane} MetricName=\"LaneHeldTasks\"", "Maximum"]
      lane_oldest = ["{TSHarness,Lane} MetricName=\"LaneOldestReadySeconds\"", "Maximum"]
      team_jobs   = ["{TSHarness,Team} MetricName=\"CodingJobsRunning\"", "Maximum"]
      outcomes    = ["{TSHarness,Outcome} MetricName=\"CodingJobsFinished\"", "Sum"]
      gate_wait   = ["{TSHarness,Gate} MetricName=\"GateOldestWaitSeconds\"", "Maximum"]
      gate_avg    = ["{TSHarness,Gate} MetricName=\"GateAvgWaitSeconds\"", "Average"]
      github      = ["{TSHarness,Installation} MetricName=\"GitHubRateLimitRemainingPercent\"", "Minimum"]
      llm_cost    = ["{TSHarness,Team} MetricName=\"LlmCostUsd\"", "Sum"]
      llm_tokens  = ["{TSHarness,Team} MetricName=\"LlmOutputTokens\"", "Sum"]
      llm_month   = ["{TSHarness,Team} MetricName=\"LlmMonthCostUsd\"", "Maximum"]
    } : key => [[{ expression = "SEARCH('${query[0]}', '${query[1]}', 300)", id = key }]]
  }

  dashboard_widgets = [
    for i, widget in [
      { title = "Ready tasks by lane", metrics = local.dashboard_search.lane_ready },
      { title = "Oldest ready task by lane (s)", metrics = local.dashboard_search.lane_oldest },
      { title = "Tasks held by team budget", metrics = local.dashboard_search.lane_held },
      {
        title = "Coding job queue"
        metrics = [
          ["TSHarness", "CodingJobsQueued", { stat = "Maximum" }],
          ["TSHarness", "CodingJobsRunning", { stat = "Maximum" }],
          ["TSHarness", "CodingJobOldestQueuedSeconds", { stat = "Maximum", yAxis = "right" }],
        ]
      },
      { title = "Running coding jobs by team", metrics = local.dashboard_search.team_jobs },
      { title = "Coding job outcomes", metrics = local.dashboard_search.outcomes },
      {
        title = "Coding job duration (s)"
        metrics = [
          ["TSHarness", "CodingJobAvgSeconds", { stat = "Average" }],
        ]
      },
      { title = "Longest wait at each gate (s)", metrics = local.dashboard_search.gate_wait },
      { title = "Average decided gate wait (s)", metrics = local.dashboard_search.gate_avg },
      { title = "GitHub rate limit left (%)", metrics = local.dashboard_search.github },
      { title = "LLM cost by team (USD per 5 min)", metrics = local.dashboard_search.llm_cost },
      { title = "LLM output tokens by team", metrics = local.dashboard_search.llm_tokens },
      { title = "LLM cost this month by team (USD)", metrics = local.dashboard_search.llm_month },
      ] : {
      type   = "metric"
      x      = (i % 3) * 8
      y      = floor(i / 3) * 6
      width  = 8
      height = 6
      properties = {
        title   = widget.title
        region  = var.aws_region
        view    = "timeSeries"
        stacked = false
        period  = 300
        metrics = widget.metrics
      }
    }
  ]
}

resource "aws_cloudwatch_dashboard" "scale_out" {
  dashboard_name = "${var.project_name}-scale-out"
  dashboard_body = jsonencode({ widgets = local.dashboard_widgets })
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
