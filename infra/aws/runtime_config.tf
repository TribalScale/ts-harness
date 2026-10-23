locals {
  runtime_config_object = {
    AGENT_TIMEZONE                        = var.agent_timezone
    SLACK_STANDUP_CHANNEL                 = var.slack_standup_channel
    SLACK_OPS_CHANNEL                     = var.slack_ops_channel
    SLACK_DEV_CHANNEL                     = var.slack_dev_channel
    SLACK_TEAM_CHANNEL                    = var.slack_team_channel
    SLACK_MARKETING_CHANNEL               = var.slack_marketing_channel
    SLACK_SALES_CHANNEL                   = var.slack_sales_channel
    APPROVED_SLACK_CHANNELS               = join(",", var.approved_slack_channels)
    WORKFLOW_RUNNER_ENABLED               = tostring(var.workflow_runner_enabled)
    WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS = tostring(var.workflow_runner_poll_interval_seconds)
    WORKFLOW_RUNNER_BATCH_SIZE            = tostring(var.workflow_runner_batch_size)
    WORKFLOW_RUNNER_LEASE_SECONDS         = tostring(var.workflow_runner_lease_seconds)
    WORKFLOW_RUNNER_STALE_POLICY          = var.workflow_runner_stale_policy
    MULTI_AGENT_ENABLED                   = tostring(var.multi_agent_enabled)
    EMBEDDINGS_PROVIDER                   = var.embeddings_provider
    EMBEDDINGS_MODEL                      = var.embeddings_model
    EMBEDDINGS_DIMENSIONS                 = tostring(var.embeddings_dimensions)
    EMBEDDINGS_FALLBACK                   = var.embeddings_fallback
    IMPLEMENT_WORK_ORDERS_REPO_URL        = var.implement_work_orders_repo_url
    IMPLEMENT_WORK_ORDERS_BASE_BRANCH     = var.implement_work_orders_base_branch
    OPENCODE_VERSION                      = var.implement_work_orders_opencode_version
    IDENTITY_PROVIDER                     = var.identity_provider
    OIDC_ISSUER                           = var.oidc_issuer
    OIDC_AUDIENCE                         = var.oidc_audience
    OIDC_JWKS_URL                         = var.oidc_jwks_url
    OIDC_CLIENT_ID                        = var.oidc_client_id
    OIDC_AUTHORIZATION_ENDPOINT           = var.oidc_authorization_endpoint
    OIDC_TOKEN_ENDPOINT                   = var.oidc_token_endpoint
  }
}

resource "aws_ssm_parameter" "runtime_config" {
  name        = "/${var.project_name}/runtime-config"
  description = "Mutable non-secret runtime knobs (Slack, OIDC, timezone)"
  type        = "String"
  value       = jsonencode(local.runtime_config_object)

  tags = {
    Name    = "${var.project_name}-runtime-config"
    Purpose = "runtime-config"
  }
}

data "aws_caller_identity" "runtime_config" {}

data "aws_iam_policy_document" "runtime_config_read" {
  statement {
    sid     = "AllowReadRuntimeConfig"
    effect  = "Allow"
    actions = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.runtime_config.account_id}:parameter/${var.project_name}/runtime-config"
    ]
  }
}

resource "aws_iam_role_policy" "runtime_config_read" {
  name   = "${var.project_name}-runtime-config-read"
  role   = aws_iam_role.harness.id
  policy = data.aws_iam_policy_document.runtime_config_read.json
}
