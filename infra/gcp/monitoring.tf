# Cloud Monitoring alert policies aligned with AWS CloudWatch metric names:
#   GatewayHealthy, WorkflowRunnerHealthy, WorkflowTasksPending
# under the TSHarness custom metric namespace.

resource "google_monitoring_notification_channel" "email" {
  count = local.create_notify

  display_name = "${var.project_name}-alerts"
  type         = "email"

  labels = {
    email_address = var.notification_email
  }

  depends_on = [google_project_service.required]
}

locals {
  notification_channel_ids = local.create_notify == 1 ? [google_monitoring_notification_channel.email[0].id] : []

  # Custom metric types published by runtime (parity with AWS TSHarness namespace).
  metric_gateway_healthy         = "custom.googleapis.com/${local.monitoring_namespace}/GatewayHealthy"
  metric_workflow_runner_healthy = "custom.googleapis.com/${local.monitoring_namespace}/WorkflowRunnerHealthy"
  metric_workflow_tasks_pending  = "custom.googleapis.com/${local.monitoring_namespace}/WorkflowTasksPending"
}

resource "google_monitoring_alert_policy" "gateway_unhealthy" {
  display_name = "${var.project_name}-gateway-unhealthy"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = "TS Harness gateway health check failed for 3 consecutive checks (GatewayHealthy < 1)."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "GatewayHealthy below 1"
    condition_threshold {
      filter          = "metric.type=\"${local.metric_gateway_healthy}\" AND resource.type=\"gce_instance\""
      duration        = "360s"
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period   = "120s"
        per_series_aligner = "ALIGN_MIN"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "1800s"
  }

  user_labels = {
    name = "${var.project_name}-gateway-unhealthy"
  }

  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "workflow_runner_unhealthy" {
  display_name = "${var.project_name}-workflow-runner-unhealthy"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = "Workflow runner heartbeat missing for 3 consecutive checks (WorkflowRunnerHealthy < 1)."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "WorkflowRunnerHealthy below 1"
    condition_threshold {
      filter          = "metric.type=\"${local.metric_workflow_runner_healthy}\" AND resource.type=\"gce_instance\""
      duration        = "360s"
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period   = "120s"
        per_series_aligner = "ALIGN_MIN"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "1800s"
  }

  user_labels = {
    name = "${var.project_name}-workflow-runner-unhealthy"
  }

  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "workflow_tasks_pending" {
  display_name = "${var.project_name}-workflow-tasks-pending"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = "Workflow tasks pending above threshold for ~30 minutes (WorkflowTasksPending > 50)."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "WorkflowTasksPending above 50"
    condition_threshold {
      filter          = "metric.type=\"${local.metric_workflow_tasks_pending}\" AND resource.type=\"gce_instance\""
      duration        = "1800s"
      comparison      = "COMPARISON_GT"
      threshold_value = 50

      aggregations {
        alignment_period   = "120s"
        per_series_aligner = "ALIGN_MAX"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "1800s"
  }

  user_labels = {
    name = "${var.project_name}-workflow-tasks-pending"
  }

  depends_on = [google_project_service.required]
}
