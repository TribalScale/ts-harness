# Cloud Billing budget (google-beta).
#
# Requires:
#   - var.billing_account_id (Billing Account Administrator / Budgets Manager)
#   - var.monthly_budget_limit > 0
#   - billingbudgets.googleapis.com enabled (see apis.tf)
#
# When billing_account_id is empty, no budget is created even if monthly_budget_limit > 0.

resource "google_billing_budget" "monthly" {
  count    = local.create_budget
  provider = google-beta

  billing_account = var.billing_account_id
  display_name    = "${var.project_name}-monthly"

  budget_filter {
    projects = ["projects/${var.gcp_project_id}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(floor(var.monthly_budget_limit))
    }
  }

  threshold_rules {
    threshold_percent = 0.8
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  dynamic "all_updates_rule" {
    for_each = local.create_notify == 1 ? [1] : []
    content {
      monitoring_notification_channels = [
        google_monitoring_notification_channel.email[0].name,
      ]
      disable_default_iam_recipients = true
    }
  }

  depends_on = [google_project_service.required]
}
