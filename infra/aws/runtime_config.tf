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
    CODING_JOBS_ENABLED                   = tostring(var.coding_jobs_enabled)
    CODING_MAX_RUNNING                    = tostring(var.coding_max_running)
    LLM_PROXY_ENABLED                     = tostring(var.llm_proxy_enabled)
    MULTI_AGENT_ENABLED                   = tostring(var.multi_agent_enabled)
    EMBEDDINGS_PROVIDER                   = var.embeddings_provider
    EMBEDDINGS_MODEL                      = var.embeddings_model
    EMBEDDINGS_DIMENSIONS                 = tostring(var.embeddings_dimensions)
    EMBEDDINGS_FALLBACK                   = var.embeddings_fallback
    IMPLEMENT_WORK_ORDERS_REPO_URL        = var.implement_work_orders_repo_url
    IMPLEMENT_WORK_ORDERS_BASE_BRANCH     = var.implement_work_orders_base_branch
    OPENCODE_VERSION                      = var.implement_work_orders_opencode_version
    GITHUB_APP_ID                         = var.github_app_id
    GITHUB_APP_SLUG                       = var.github_app_slug
    GITHUB_APP_HOST                       = var.github_app_host
    IDENTITY_PROVIDER                     = var.identity_provider
    OIDC_ISSUER                           = var.oidc_issuer
    OIDC_AUDIENCE                         = var.oidc_audience
    OIDC_JWKS_URL                         = var.oidc_jwks_url
    OIDC_CLIENT_ID                        = var.oidc_client_id
    OIDC_AUTHORIZATION_ENDPOINT           = var.oidc_authorization_endpoint
    OIDC_TOKEN_ENDPOINT                   = var.oidc_token_endpoint
    OIDC_PRESET                           = var.oidc_preset
    OIDC_TOKEN_TYPE                       = var.oidc_token_type
    OIDC_GROUPS_CLAIM                     = var.oidc_groups_claim
    OIDC_ROLES_CLAIM                      = var.oidc_roles_claim
    OIDC_ALLOWED_DOMAINS                  = var.oidc_allowed_domains
    OIDC_GROUPS_SOURCE                    = var.oidc_groups_source
    IDENTITY_DIRECTORY                    = var.identity_directory
    DEPLOYMENT_PROFILE                    = var.deployment_profile
    CODING_ECS_CLUSTER                    = try(aws_ecs_cluster.coding[0].name, "")
    CODING_ECS_TASK_DEFINITION            = local.ecs_enabled ? local.coding_worker_family : ""
    CODING_ECS_CONTAINER                  = local.coding_worker_container
    CODING_ECS_SUBNETS                    = join(",", local.ecs_enabled ? local.private_subnet_ids : [])
    CODING_ECS_SECURITY_GROUPS            = join(",", aws_security_group.coding_worker[*].id)
    CODING_EGRESS_PROXY_SERVICE           = try(aws_ecs_service.egress_proxy[0].name, "")
    CODING_EGRESS_DOMAINS_PARAMETER       = try(aws_ssm_parameter.egress_extra_domains[0].name, "")
    CODING_EGRESS_PROXY_URL               = local.egress_proxy_url
    CODING_WORKER_LLM_PROXY_URL           = local.llm_proxy_worker_url
    LLM_PROXY_HOST_PORT                   = local.ecs_enabled && var.llm_proxy_enabled ? tostring(local.llm_proxy_port) : ""
    TS_HARNESS_STATE_BACKEND              = local.state_backend_effective
    TS_HARNESS_OBJECT_BACKEND             = local.object_backend_effective
    TS_HARNESS_SKILLS_BUCKET              = try(aws_s3_bucket.skills[0].id, "")
    TS_HARNESS_WORKSPACE_BUCKET           = try(aws_s3_bucket.workspaces[0].id, "")
  }
}

resource "aws_ssm_parameter" "runtime_config" {
  name        = "/${local.secret_prefix_effective}/runtime-config"
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
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.runtime_config.account_id}:parameter/${local.secret_prefix_effective}/runtime-config"
    ]
  }
}

resource "aws_iam_role_policy" "runtime_config_read" {
  name   = "${var.project_name}-runtime-config-read"
  role   = aws_iam_role.harness.id
  policy = data.aws_iam_policy_document.runtime_config_read.json
}
